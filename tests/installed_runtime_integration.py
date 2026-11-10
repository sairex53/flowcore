"""Run against a real installed RPM in a disposable, booted Fedora guest."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

CLI = '/usr/bin/flowcore'
CONTROL = '/run/flowcore-sdk/intent.sock'
UNITS = ('flowcore-monitor.service', 'flowcore-sdk.socket', 'flowcored.service',
         'flowcore-network.socket', 'flowcore-network.service')
APP, READER, OUTSIDER = ('flowcore-test-app', 'flowcore-test-reader', 'flowcore-test-other')
CLIENT = r'''
import json, socket, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as peer:
    peer.settimeout(5)
    peer.connect(sys.argv[1])
    for message in json.loads(sys.argv[2]):
        if message.get('lease') == 'previous':
            message['lease'] = reply['result']['lease']
        peer.sendall(json.dumps(message).encode())
        reply = json.loads(peer.recv(65536))
        print(json.dumps(reply), flush=True)
'''


def run(*args, env=None, ok=True, timeout=25):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    if (result.returncode == 0) != ok:
        raise AssertionError(f'{args[:4]} returned {result.returncode}: {result.stderr[:400]}')
    return result.stdout


def control(*args):
    return run('/usr/bin/systemctl', *args)


def user(name, *args, **kwargs):
    return run('/usr/bin/runuser', '-u', name, '--', *args, **kwargs)


def response(name, *messages, ok=True):
    data = user(name, '/usr/bin/python3', '-I', '-B', '-c', CLIENT,
                CONTROL, json.dumps(messages), ok=ok)
    return [json.loads(row) for row in data.splitlines()]


def register(name):
    return dict(op='intent_v2', version=2, action='register', isolation='shared',
                intent=dict(name=name, priority='standard'))


def pid(unit):
    return int(control('show', '--property=MainPID', '--value', unit))


def wait(check, seconds=15):
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('service transition timed out')


def proc(unit, expected_capability):
    process = pid(unit)
    fields = dict(line.split(':', 1) for line in Path(f'/proc/{process}/status').read_text().splitlines()
                  if ':' in line)
    assert int(fields['Uid'].split()[1]) > 0 and int(fields['NoNewPrivs']) == 1, fields['Uid']
    for key in ('CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb'):
        assert int(fields[key], 16) == expected_capability, (unit, key, fields[key])
    context = Path(f'/proc/{process}/attr/current').read_text().strip('\x00\n')
    print(json.dumps(dict(unit=unit, pid=process, context=context, capability=expected_capability)), flush=True)
    return process


def network_state():
    return [json.loads(run('/usr/bin/ip', *arguments)) for arguments in
            (('-j','link','show'), ('-j','address','show'), ('-j','route','show','table','all'),
             ('-6','-j','route','show','table','all'))] + [json.loads(run('/usr/bin/tc','-j','qdisc','show'))]


def network(name, *args, ok=True):
    data = user(name, CLI, 'network', *args, ok=ok)
    return json.loads(data) if ok else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.parse_args()
    if os.geteuid() or run('/usr/bin/getenforce').strip() != 'Enforcing':
        raise SystemExit('requires root and SELinux enforcing in an explicitly disposable guest')
    if Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise SystemExit('requires systemd as PID 1')
    run('/usr/bin/rpm', '-qf', CLI, '/usr/share/flowcore/scripts/network_helper.py')
    for unit in UNITS:
        if control('show', '--property=ActiveState', '--value', unit).strip() != 'inactive':
            raise SystemExit('requires a fresh guest with inactive FLOWCORE units')
    for name in (APP, READER, OUTSIDER):
        if subprocess.run(['/usr/bin/getent','passwd',name],capture_output=True).returncode == 0:
            raise SystemExit('fixture accounts already exist')
    config = Path('/etc/flowcore/service.json')
    helper = Path('/etc/flowcore/helper.json')
    old_config, old_helper = config.read_bytes(), helper.read_bytes()
    baseline = network_state()
    created = []
    try:
        for name, groups in ((APP,'flowcore-app,flowcore-operator'),
                             (READER,'flowcore-app,flowcore-operator'), (OUTSIDER,'flowcore-app')):
            run('/usr/bin/useradd', '-M', '-G', groups, name)
            created.append(name)
        app, reader = (int(run('/usr/bin/id','-u',name)) for name in (APP,READER))
        options = json.loads(old_config)
        options.setdefault('intent_api', {'priorities':['standard']})['applications'] = {'installed-app':app, 'installed-reader':reader}
        config.write_text(json.dumps(options)+'\n')
        helper.write_text(json.dumps(dict(version=1, operators=[app], readers=[reader]))+'\n')
        control('start', 'flowcore-monitor.service', 'flowcore-sdk.socket', 'flowcore-network.socket')
        control('start', 'flowcored.service')
        monitor = dict(os.environ, FLOWCORE_RUNTIME='/run/flowcore-monitor')
        assert json.loads(run(CLI,'status',env=monitor))['monitor_only']
        assert json.loads(run(CLI,'status'))['control_only']
        proc('flowcore-monitor.service', 0)
        daemon = proc('flowcored.service', 0)
        for file in (CLI, config, helper, CONTROL, '/run/flowcore-helper/control.sock',
                     '/var/lib/flowcore', '/var/lib/flowcore-monitor'):
            print(run('/usr/bin/ls','-Zd',file).strip(), flush=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(response, APP, register('installed-app'))
            b = pool.submit(response, READER, register('installed-reader'))
            assert all(result.result()[0]['ok'] for result in (a,b))
        assert response(APP, register('installed-reader'))[0]['code'] == 'forbidden'
        assert response(APP, dict(op='lab_up'))[0]['code'] == 'forbidden'
        response(OUTSIDER, register('installed-app'), ok=False)
        user(APP, CLI, 'status', ok=False)

        first = network(APP, 'status')
        helper_pid = proc('flowcore-network.service', 1 << 12)
        assert not os.path.samestat(os.stat('/proc/1/ns/net'), os.stat(f'/proc/{helper_pid}/ns/net'))
        network(READER, 'status')
        network(READER, 'create', '--epoch', first['epoch'], '--revision', str(first['revision']), ok=False)
        network(OUTSIDER, 'status', ok=False)
        with tempfile.TemporaryDirectory(prefix='flowcore-publish-', dir='/var/tmp') as base:
            base = Path(base)
            base.chmod(0o755)
            output = base/'out'
            output.mkdir(mode=0o700)
            os.chown(output,app,app)
            (base/'alias').symlink_to(output)
            network(APP, 'publish', '--directory', str(base/'alias'), '--name', 'bad.json', ok=False)
            network(APP, 'publish', '--directory', str(output), '--name', '../escape.json', ok=False)
            result = network(APP,'exercise','--directory',str(output),'--name','verified.json')
            assert [row['limit_packets'] for row in result['observations']] == [None,None,128,None,None]
            assert not result['observations'][-1]['lab_up']
            published = output/'verified.json'
            content = published.read_bytes()
            assert published.stat().st_uid == app and published.stat().st_mode & 0o777 == 0o600
            network(APP,'publish','--directory',str(output),'--name','verified.json',ok=False)
            assert published.read_bytes() == content
        current = network(APP,'status')
        network(APP,'create','--epoch',first['epoch'],'--revision',str(first['revision']),ok=False)
        network(APP,'create','--epoch',current['epoch'],'--revision',str(current['revision']))
        control('kill','--kill-whom=main','--signal=SIGKILL','flowcore-network.service')
        wait(lambda: pid('flowcore-network.service') == 0)
        recovered = network(APP,'status')
        assert recovered['epoch'] != current['epoch'] and not recovered['lab_up']

        options['log_level'] = 'error'
        config.write_text(json.dumps(options)+'\n')
        control('reload','flowcored.service')
        wait(lambda: json.loads(run(CLI,'ops','status'))['checkpoint']['generation'] >= 1)
        control('kill','--kill-whom=main','--signal=SIGKILL','flowcored.service')
        wait(lambda: pid('flowcored.service') not in (0,daemon))
        wait(lambda: Path('/run/flowcore/control.sock').exists())
        assert json.loads(run(CLI,'status'))['previous_unclean']
        control('restart', 'flowcore-monitor.service')
        assert json.loads(run(CLI,'status',env=monitor))['monitor_only']
    finally:
        control('stop', *UNITS)
        config.write_bytes(old_config)
        helper.write_bytes(old_helper)
        for name in reversed(created):
            run('/usr/bin/userdel', name)
    assert all(not Path(path).exists() for path in (CONTROL, '/run/flowcore-helper/control.sock',
               '/run/flowcore/control.sock', '/run/flowcore-monitor/control.sock'))
    assert network_state() == baseline
    assert run('/usr/bin/getenforce').strip() == 'Enforcing'
    print('PASS installed RPM systemd lifecycle, capabilities, separate identities, result handoff and cleanup')


if __name__ == '__main__':
    main()
