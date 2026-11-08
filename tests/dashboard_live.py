"""Run the dashboard against a disposable privileged lab and optional Chromium."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from benchmark import BINDIR, GID, UID, Lab, owned_directory
from fabric_lab import stop, until

MEASURE = r"""
from concurrent.futures import ThreadPoolExecutor
import http.client,json,socket,sys,time

control,start=sys.argv[1],int(sys.argv[2])
def worker(index):
    conn=http.client.HTTPConnection('10.203.0.2',9000,timeout=.7)
    reports=[]
    for sequence in range(index,720,4):
        time.sleep(max(0,(start+sequence*25_000_000-time.monotonic_ns())/1e9))
        before=time.monotonic_ns()
        try:
            conn.request('GET','/api/critical?work_ms=1')
            response=conn.getresponse()
            response.read()
            outcome='ok' if response.status==200 else 'failure'
        except (OSError,http.client.HTTPException) as error:
            outcome='timeout' if isinstance(error,TimeoutError) else 'failure'
            conn.close()
        completed=time.monotonic_ns()
        latency_us=(completed-before)/1000
        report=dict(op='request_sample',service='demo',outcome=outcome,duration_us=latency_us,
                    deadline_ms=15,completed_ns=completed)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as ipc:
            ipc.settimeout(2)
            ipc.connect(control)
            ipc.sendall(json.dumps(report).encode()+b'\n')
            with ipc.makefile('rb') as stream:
                reply=json.load(stream)
                assert reply['ok'], reply
        reports.append(dict(sequence=sequence,completed_ns=completed,outcome=outcome,latency_us=latency_us))
    conn.close()
    return reports
with ThreadPoolExecutor(max_workers=4) as pool:
    reports=sorted((item for batch in pool.map(worker,range(4)) for item in batch),key=lambda row:row['sequence'])
print(json.dumps(reports))
"""

HTTP = r"""
import http.client,json,sys
from pathlib import Path
conn=http.client.HTTPConnection('127.0.0.1',int(sys.argv[1]),timeout=3)
conn.request('GET','/api/v1/'+sys.argv[3],headers={'Authorization':'Bearer '+Path(sys.argv[2]).read_text().strip()})
response=conn.getresponse()
print(json.dumps(dict(status=response.status,body=json.loads(response.read()))))
conn.close()
"""

DELEGATE = r"""
import json,socket,sys
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
    s.settimeout(2)
    s.connect('\0'+sys.argv[1])
    try:
        s.sendall(b'{"op":"lab_down"}\n')
        with s.makefile('rb') as stream:
            frame=stream.readline()
    except ConnectionResetError:
        frame=b''
    if sys.argv[2]=='wrong-uid':
        assert not frame
    else:
        assert json.loads(frame)['code']=='forbidden'
"""


MPTCP = r"""
import socket,time
try:
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM,262) as server:
        server.bind(('127.0.0.1',0))
        server.listen(1)
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM,262) as client:
            client.connect(server.getsockname())
            conn,_=server.accept()
            with conn:
                print('mptcp-ready',flush=True)
                time.sleep(90)
except OSError:
    print('mptcp-unavailable',flush=True)
"""


def rpc(path, op, **values):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(path))
        client.sendall(json.dumps(dict(op=op, **values)).encode()+b'\n')
        with client.makefile('rb') as stream:
            result = json.load(stream)
    assert result['ok'], result
    return result['result']


def run(lab, work, args):
    lab.start_holders(1)
    lab.command(0, ['ip', 'link', 'set', 'lo', 'up'])
    config = json.loads((ROOT/'config/service.json').read_text())
    config['observability']['bpf'] = 'off'
    config['telemetry_seconds'] = .5
    path = work/'service.json'
    path.write_text(json.dumps(config))
    runtime = work/'runtime'
    control = runtime/'control.sock'
    monitor = f'flowcore-dashboard-test-{os.getpid()}'
    daemon = lab.spawn(0, ['env', f'FLOWCORE_RUNTIME={runtime}', f'FLOWCORE_STATE={runtime}',
        BINDIR/'flowcored', '--config', path, '--research', '--monitor-socket', monitor,
        '--monitor-user', UID], 'daemon', privileged=True)
    until(control.is_socket, 'flowcored control socket')
    rpc(control, 'lab_up')
    net = dict(bandwidth_kbit=20000, base_rtt_ms=2, jitter_ms=0, loss_pct=0, reorder_pct=0)
    rpc(control, 'research_conditions', network=net, mode='hybrid', seed=1471, link_up=True)
    rpc(control, 'controller_shadow', strategy='hybrid')
    sender, receiver = rpc(control, 'inspect')['namespaces']
    server = lab.spawn(0, ['nsenter', '-t', receiver, '-n', '--', 'setpriv',
        f'--reuid={UID}', f'--regid={GID}', '--clear-groups', sys.executable, '-B',
        ROOT/'examples/real_apps/http_service.py', '--bind', '10.203.0.2'],
        'http-service', privileged=True)
    until(lambda: '"ready": true' in (lab.output/'http-service.stdout').read_text(), 'HTTP receiver')
    assert server.poll() is None
    lab.command(0, [sys.executable, '-B', '-c', DELEGATE, monitor, 'wrong-uid'])
    lab.command(0, ['setpriv', f'--reuid={UID}', f'--regid={GID}', '--clear-groups', sys.executable,
                    '-B', '-c', DELEGATE, monitor, 'allowed-uid'])
    assert rpc(control, 'status')['lab_up'] is True

    credential = work/'dashboard.token'
    credential.write_text(secrets.token_urlsafe(32)+'\n')
    os.chown(credential, UID, GID)
    credential.chmod(0o600)
    state = work/'dashboard-state'
    state.mkdir(mode=0o700)
    os.chown(state, UID, GID)
    argv = [sys.executable, '-B', ROOT/'scripts/dashboard.py', '--socket', '@'+monitor, '--daemon-user', 0,
            '--state', state, '--token-file', credential, '--port', 0, '--interval', 1, '--window', 10]
    for artifact in args.artifact:
        identifier, file = artifact.split('=', 1)
        argv.extend(['--artifact', identifier+'='+str(Path(file).resolve())])
    lab.spawn(0, [sys.executable, '-B', '-c', MPTCP], 'mptcp-socket')
    mptcp = until(lambda: (lab.output/'mptcp-socket.stdout').read_text().strip(), 'MPTCP capability fixture')
    dashboard = lab.spawn(0, argv, 'dashboard')
    port = until(lambda: next((json.loads(line)['port'] for line in
                  (lab.output/'dashboard.stdout').read_text().splitlines() if 'dashboard_ready' in line), None),
                 'dashboard loopback listener')

    def get(route):
        response = json.loads(lab.command(0, [sys.executable, '-B', '-c', HTTP, port, credential, route]))
        if response['status'] != 200:
            raise RuntimeError(f"monitor route {route} returned {response['status']}")
        return response['body']

    until(lambda: get('health')['status'] == 'ok', 'first monitoring sample')
    live = get('overview')['data']
    if mptcp == 'mptcp-ready':
        until(lambda: any(row['state']=='ESTAB' for row in
            ((get('multipath')['data'] or {}).get('connections') or [])), 'live MPTCP inventory')
    assert any(row['id']=='lab:fc0' for row in live['interfaces'])
    start = ((time.monotonic_ns()+1_000_000_000)//4_000_000_000+1)*4_000_000_000
    measured = lab.spawn(0, ['nsenter', '-t', sender, '-n', '--', sys.executable, '-B', '-c',
        MEASURE, control, start], 'measured-http', privileged=True)
    time.sleep(max(0, (start+4_000_000_000-time.monotonic_ns())/1e9))
    fault = rpc(control, 'research_conditions', network=dict(net, base_rtt_ms=45),
                mode='hybrid', seed=1471, link_up=True)

    def incident():
        rows = get('incidents')['data']['items']
        return next((row for row in rows if row['service']=='demo'), None)

    row = until(incident, 'latency incident', 20)
    bundle = get('incident?id='+row['id']+'&kind=fault')['data']
    assert bundle['items'] and bundle['items'][0]['kind'] == 'fault'
    assert 'SLO_P99_EXCEEDED' in bundle['report']['signals']
    assert get('policy-history')['data']['total'] > 0
    history = get('history?kind=service&id=demo&seconds=60&points=80')['data']
    assert any(point['values'] and point['values']['p99_ms'] is not None for point in history['points'])
    live = get('overview')['data']
    assert live['classes'] and any(row['p99_ms'] is not None for row in live['services'])

    if args.browser_root:
        browser = args.browser_root.resolve()
        screenshots = lab.output/'screenshots'
        owned_directory(screenshots)
        env = [f'NODE_PATH={browser}/node_modules', f'PLAYWRIGHT_BROWSERS_PATH={browser}/browsers']
        if (browser/'libs/usr/lib64').is_dir():
            env.append(f'LD_LIBRARY_PATH={browser}/libs/usr/lib64')
        identifiers = {row.split('=',1)[0] for row in args.artifact}
        browser_test = lab.spawn(0, ['env', *env, 'node', ROOT/'tests/dashboard_browser.cjs',
            f'http://127.0.0.1:{port}', credential, screenshots,
            'app' if 'app' in identifiers else '', 'mptcp' if 'mptcp' in identifiers else ''], 'browser')
        if browser_test.wait(timeout=90):
            raise RuntimeError('browser smoke failed; see browser.stderr')
    assert measured.wait(timeout=30)==0
    report = json.loads((lab.output/'measured-http.stdout').read_text())
    assert len(report)==720
    stop(daemon)
    until(lambda: get('health')['status']=='stale', 'source failure surfaced')
    result = dict(measured_requests=len(report), successful_requests=sum(row['outcome']=='ok' for row in report),
                  incident=row['id'], signals=bundle['report']['signals'], fault_readback=fault,
                  browser_checked=args.browser_root is not None, live_mptcp=mptcp, source_stale=True)
    (lab.output/'validation.json').write_text(json.dumps(result, indent=2)+'\n')
    assert dashboard.poll() is None
    print('PASS: isolated daemon, authenticated projections, measured incident and stale source')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser-root', type=Path)
    parser.add_argument('--artifact', action='append', default=[])
    args = parser.parse_args()
    if os.geteuid()!=0 or UID==0:
        parser.error('run through sudo from a nonroot lab account')
    args.output = args.output.resolve()
    owned_directory(args.output)
    lab = Lab(args.output)
    with tempfile.TemporaryDirectory(prefix='dash-', dir=ROOT/'results') as name:
        work = Path(name)
        work.chmod(0o755)
        try:
            run(lab, work, args)
        finally:
            lab.close()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    main()
