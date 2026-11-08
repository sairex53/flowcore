#!/usr/bin/env python3
"""Disposable namespace lab for FLOWID recovery and stop-and-wait throughput."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

from benchmark import ENV, GID, Lab, UID, run
from flowid_lab_support import Reader, client_args, endpoint_file, pki, settings, stop
from paths import BINDIR, RESULTS, ROOT

PRIMARY = '198.18.20.2'
SECONDARY = '198.18.21.2'
REPLACEMENT = '198.18.21.3'


def require(condition, failure):
    if not condition:
        raise RuntimeError(failure)


def process_usage(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    status = Path(f'/proc/{pid}/status').read_text().splitlines()
    return {'cpu_ticks': int(fields[11])+int(fields[12]),
            'peak_rss_kib': int(next(line for line in status if line.startswith('VmHWM:')).split()[1])}


class Experiment:
    def __init__(self, lab, private, binary):
        self.lab, self.private, self.binary = lab, private, binary
        self.pin = pki(private)
        self.state = private/'state'
        for entry in private.iterdir():
            os.chown(entry, UID, GID)
        os.chown(private, UID, GID)

    def network(self):
        self.lab.start_holders(3)
        for index, subnet in ((1, 20), (2, 21)):
            client, server = f'fc{index-1}', f'fs{index}'
            self.lab.command(0, ['ip', 'link', 'add', client, 'type', 'veth', 'peer', 'name', server,
                                 'netns', self.lab.namespace_path(index)])
            for ns, interface, tail in ((0, client, 1), (index, server, 2)):
                self.lab.command(ns, ['ip', 'link', 'set', 'lo', 'up'])
                self.lab.command(ns, ['ip', 'addr', 'add', f'198.18.{subnet}.{tail}/29', 'dev', interface])
                self.lab.command(ns, ['ip', 'link', 'set', interface, 'up'])
        return [json.loads(self.lab.command(index, ['ip', '-j', 'addr'])) for index in range(3)]

    def spawn(self, index, argv, name):
        with (self.lab.output/f'{name}.stderr').open('w') as error:
            proc = subprocess.Popen(self.lab.ns(index, ['setpriv', f'--reuid={UID}', f'--regid={GID}',
                                    '--clear-groups', '--', *argv]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=error, start_new_session=True, env=ENV)
        self.lab.processes.append(proc)
        self.lab.files.extend((proc.stdin, proc.stdout))
        return Reader(proc)

    def server(self, index, address, label, *, transport='tcp', port=0):
        config = self.private/f'{label}.json'
        cfg = settings(self.private, self.pin, address=address, transport=transport, ttl=300)
        cfg['listen']['port'] = port
        config.write_text(json.dumps(cfg))
        proc = self.spawn(index, [sys.executable, '-B', ROOT/'scripts/flowid_server.py', config,
                                  '--state', self.state], label)
        ready = proc.get()
        require(ready['event'] == 'flowid_ready', 'server did not start')
        return proc, ready['port']

    def client(self, port, address, label, *, transport='tcp', extra=()):
        endpoint_file(self.private/'endpoints', port, address, transport)
        reader = self.spawn(0, [*client_args(self.binary, self.private, transport=transport), *extra], label)
        return reader, reader.get()

    def phase(self, result, name, operation):
        began = time.monotonic_ns()
        value = operation()
        result[name] = {'offset_ns': began-result['started_ns'], 'elapsed_us': (time.monotonic_ns()-began)//1000,
                        'observations': value}
        return value

    def recovery(self):
        result = {'started_ns': time.monotonic_ns()}
        server, port = self.server(1, PRIMARY, 'server-original')
        runtime = self.private/'runtime'
        daemon = self.spawn(0, ['/usr/bin/env', f'FLOWCORE_RUNTIME={runtime}', f'FLOWCORE_STATE={runtime}',
                                BINDIR/'flowcored'], 'flowcored')
        for _ in range(100):
            if (runtime/'control.sock').is_socket():
                break
            require(daemon.proc.poll() is None, 'flowcored exited')
            time.sleep(.02)
        else:
            raise RuntimeError('flowcored did not expose its local control socket')
        client, opened = self.client(port, PRIMARY, 'client-recovery',
                                    extra=('--flowcore-socket', str(runtime/'control.sock'), '--deadline-us', '50000'))
        require(opened['event'] == 'opened', 'TCP session failed to open')
        result['open'] = opened

        def command(name, line, sequence, pending=0):
            rows = self.phase(result, name, lambda: client.command(line))
            final = rows[-1]
            require(final.get('acknowledged') == sequence and final.get('pending') == pending,
                    f'{name}: unexpected cursor or outbox size: {final}')
            return rows

        def link(value):
            self.lab.command(0, ['ip', 'link', 'set', 'fc0', value])
            state = json.loads(self.lab.command(0, ['ip', '-j', 'link', 'show', 'dev', 'fc0']))[0]
            require(('UP' in state['flags']) == (value == 'up'), 'link change not confirmed')
            return state['flags']

        command('initial_delivery', 'send initial', 1)
        self.phase(result, 'link_down_confirmed', lambda: link('down'))
        command('partition_send', 'send partition', 1, 1)
        command('partition_still_pending', 'flush', 1, 1)
        self.phase(result, 'link_restored', lambda: link('up'))
        command('link_recovery', 'flush', 2)
        self.phase(result, 'server_sigkill', lambda: stop(server.proc, force=True))
        command('server_down_send', 'send restart', 2, 1)
        began = time.monotonic_ns()
        server, _ = self.server(1, PRIMARY, 'server-restarted', port=port)
        result['restart_listen_us'] = (time.monotonic_ns()-began)//1000
        command('restart_recovery', 'flush', 3)

        self.phase(result, 'old_endpoint_stopped', lambda: stop(server.proc, force=True))
        command('migration_send', 'send migration', 3, 1)
        self.lab.command(2, ['ip', 'addr', 'del', SECONDARY+'/29', 'dev', 'fs2'])
        self.lab.command(2, ['ip', 'addr', 'add', REPLACEMENT+'/29', 'dev', 'fs2'])
        result['replacement_addresses'] = json.loads(self.lab.command(2, ['ip', '-j', 'addr', 'show', 'dev', 'fs2']))
        replacement, replacement_port = self.server(2, REPLACEMENT, 'server-replacement')
        endpoint_file(self.private/'endpoints', replacement_port, REPLACEMENT, generation=2)
        rows = command('endpoint_recovery', 'flush', 4)
        require(any(row.get('address') == REPLACEMENT for row in rows), 'client did not resume to replacement')
        command('post_migration_delivery', 'send after', 5)
        result['close'] = client.command('close')[-1]
        require(result['close']['status'] == 'acknowledged', 'session did not close')
        result['flowcore'] = client.get()
        require(result['flowcore']['event'] == 'flowcore' and result['flowcore']['accepted'] >= 5,
                'FLOWCORE intent telemetry did not accept the demo request samples')
        with sqlite3.connect(self.state/'flowid-v1.sqlite') as store:
            result['durable_state'] = store.execute(
                'SELECT committed,association,app_messages,app_bytes,closed FROM sessions').fetchall()
        rows = result['durable_state']
        require(len(rows) == 1 and rows[0][0] == rows[0][2] == 5 and rows[0][3:] == (37, 1),
                f'durable commit count mismatch: {rows}')
        stop(replacement.proc)
        stop(daemon.proc)
        return result

    def throughput(self, transport, repeats, messages):
        index, address = 1, PRIMARY
        server, port = self.server(index, address, 'server-'+transport, transport=transport)
        result = []
        for i in range(repeats):
            client, opened = self.client(port, address, f'client-{transport}-{i}', transport=transport)
            if opened.get('status') == 'unsupported_transport' and transport == 'mptcp':
                stop(server.proc)
                return {'status': 'unsupported_transport', 'runs': result}
            require(opened['event'] == 'opened', f'{transport} client did not open: {opened}')
            before = process_usage(server.proc.pid)
            rows = client.command(f'bench {messages} 1024', timeout=30)
            after = process_usage(server.proc.pid)
            sample = rows[-1]
            require(sample['status'] == 'acknowledged' and sample['messages'] == messages, 'incomplete throughput run')
            sample.update(open=opened, server_cpu_seconds=(after['cpu_ticks']-before['cpu_ticks'])/os.sysconf('SC_CLK_TCK'),
                          server_process_peak_rss_kib=after['peak_rss_kib'],
                          payload_mbit_per_second=sample['payload_bytes']*8/sample['elapsed_us'])
            result.append(sample)
            require(client.command('close')[-1]['status'] == 'acknowledged', 'throughput session did not close')
            client.proc.wait(timeout=4)
        stop(server.proc)
        return {'status': 'completed', 'runs': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new directory within FLOWCORE_RESULTS')
    parser.add_argument('--client', type=Path, default=BINDIR/'flowid-client')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--messages', type=int, default=250)
    parser.add_argument('--mptcp', action='store_true', help='explicitly test negotiated MPTCP')
    args = parser.parse_args()
    output = args.output.resolve()
    binary = args.client.resolve()
    if os.geteuid() != 0 or UID == 0 or GID == 0:
        parser.error('run with sudo from an unprivileged lab account')
    if output == RESULTS.resolve() or not output.is_relative_to(RESULTS.resolve()):
        parser.error('output must be a new directory within FLOWCORE_RESULTS')
    if not 1 <= args.repeats <= 6 or not 1 <= args.messages <= 500 or not binary.is_file():
        parser.error('require built native client, 1–6 repetitions and 1–500 messages')
    output.mkdir()
    host = json.loads(run(['ip', '-j', 'link', 'show']))
    lab = Lab(output)
    with binary.open('rb') as executable:
        build_digest = hashlib.file_digest(executable, 'sha256').hexdigest()
    summary = {'schema': 'flowid-experiment/1', 'system': list(os.uname()),
               'repeats': args.repeats, 'messages': args.messages, 'mptcp_requested': args.mptcp,
               'client_sha256': build_digest}
    def interrupted(*_):
        raise KeyboardInterrupt('experiment interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix='fi-', dir=output) as directory:
            experiment = Experiment(lab, Path(directory), binary)
            try:
                summary['topology'] = experiment.network()
                summary['recovery'] = experiment.recovery()
                summary['tcp'] = experiment.throughput('tcp', args.repeats, args.messages)
                if args.mptcp:
                    summary['mptcp_runtime'] = experiment.lab.command(1, ['sysctl', '-n', 'net.mptcp.enabled']).strip()
                    if summary['mptcp_runtime'] == '1':
                        summary['mptcp'] = experiment.throughput('mptcp', args.repeats, args.messages)
                    else:
                        summary['mptcp'] = {'status': 'disabled', 'runs': []}
            finally:
                lab.close()
        summary['status'] = 'completed'
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        summary['status'] = 'failed'
        summary['error'] = str(error)
        raise
    finally:
        summary['host_link_inventory_unchanged'] = host == json.loads(run(['ip', '-j', 'link', 'show']))
        summary['namespace_holders_exited'] = all(proc.poll() is not None for proc in lab.holders)
        (output/'report.json').write_text(json.dumps(summary, indent=2)+'\n')
    require(summary['host_link_inventory_unchanged'], 'host link inventory changed during experiment')
    require(summary['namespace_holders_exited'], 'namespace holders still running')


if __name__ == '__main__':
    main()
