"""Staged relocation, unprivileged operation, SDK consumer and clean removal."""
import json
import ctypes
import os
import socket
import sys
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(argv, **kw):
    return subprocess.run(argv,check=True,text=True,capture_output=True,timeout=25,**kw).stdout


def main():
    if os.geteuid()==0:
        raise SystemExit('run the installed monitor test unprivileged')
    build = Path(os.environ.get('FLOWCORE_TEST_BUILD', ROOT/'build')).resolve()
    with tempfile.TemporaryDirectory(prefix='deploy-',dir=ROOT/'build') as work:
        work = Path(work)
        stage = work/'stage'
        env = dict(os.environ,DESTDIR=str(stage),TMPDIR=str(work))
        command(['cmake','--install',build,'--prefix','/usr'],env=env)
        prefix = stage/'usr'
        manifest = [stage/line.lstrip('/') for line in (build/'install_manifest.txt').read_text().splitlines()]
        assert all(path.is_relative_to(stage) and path.exists() for path in manifest)
        cli,daemon = (prefix/'bin'/name for name in ('flowcore','flowcored'))
        assert command([cli,'--version']).strip()=='FLOWCORE 0.2.0'
        assert ctypes.CDLL(str(prefix/'bin/libflowcore_intelligence.so')).flowcore_allocate
        command([sys.executable,'-I','-B','-c',
                 "import sys;sys.path.insert(0,sys.argv[1]);from predictive import Native;"
                 "rows=[dict(time_ns=(i+1)*10**9,backlog_bytes=0,departure_bps=1,capacity_bps=1000) for i in range(8)];"
                 "assert Native().forecast(rows,'hybrid',1514,.1)['risk']==0",
                 prefix/'share/flowcore/scripts'])
        assert command([daemon,'--version']).strip()=='0.2.0'
        fallback=json.loads(command([cli,'flows','--limit','32','--json']))
        assert fallback['source']=='inet_diag' and fallback['degraded']
        libraries=list(prefix.rglob('libflowcore_intent.so'))
        assert len(libraries)==1 and ctypes.CDLL(str(libraries[0])).fc_v2_open
        runtime = work/'runtime'
        private_state = work/'private-state'
        private_state.mkdir(mode=0o700)
        public_state = work/'public-state'
        public_state.symlink_to(private_state, target_is_directory=True)
        state = public_state/'state'
        notify = work/'notify'
        env = dict(os.environ,FLOWCORE_RUNTIME=str(runtime),FLOWCORE_STATE=str(state),NOTIFY_SOCKET=str(notify))
        env.pop('DESTDIR',None)
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as notifications, (work/'daemon.log').open('w') as log:
            notifications.bind(str(notify))
            notifications.settimeout(8)
            proc = subprocess.Popen([daemon,'--monitor-only'],env=env,stdout=log,stderr=log)
            try:
                assert notifications.recv(256)==b'READY=1'
                assert (private_state/'state/state.lock').is_file()
                assert b'/share/flowcore/scripts/daemon.py' in Path(f'/proc/{proc.pid}/cmdline').read_bytes()
                assert any(line.split()[1]=='0000000000000000' for line in
                           Path(f'/proc/{proc.pid}/status').read_text().splitlines() if line.startswith('CapEff:'))
                result = json.loads(command([cli,'platform','interfaces'],env=env))
                assert result['error'] is None and len(result['interfaces'])>=1
                denied = subprocess.run([cli,'lab','up'],env=env,capture_output=True,text=True,timeout=5)
                assert denied.returncode==1 and 'forbidden' in denied.stderr
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                    peer.settimeout(2)
                    peer.connect(str(runtime/'control.sock'))
                    peer.sendall(b'{"op":"status","op":"lab_up"}\n')
                    assert json.loads(peer.recv(4096))['code']=='invalid_request'
                assert notifications.recv(256)==b'WATCHDOG=1'
                metrics = command([cli,'metrics','--prometheus'],env=env)
                assert 'flowcore_interface_available' in metrics and 'flowcore_daemon_healthy 1' in metrics
                view = json.loads(command([cli,'metrics','--json'],env=env))
                overhead = view['observability']['overhead']
                print(json.dumps(dict(installed_monitor_cpu_seconds=overhead['daemon_cpu_seconds'],
                    installed_monitor_rss_bytes=overhead['daemon_rss_bytes'],
                    uptime_seconds=view['state']['uptime_seconds'],interfaces=len(result['interfaces']))))
            finally:
                proc.terminate()
                proc.wait(timeout=8)
            assert not (runtime/'control.sock').exists()
        verify = work/'flowcored.service'
        verify.write_text((ROOT/'packaging/flowcored.service').read_text().replace('/usr/bin/flowcored',str(daemon)))
        units = stage/'usr/lib/systemd/system'
        service = units/'flowcore-network.service'
        assert any((base/'etc/flowcore/helper.json').is_file() for base in (stage, prefix))
        assert (stage/'usr/lib/sysusers.d/flowcore-operator.conf').is_file()
        assert 'network' in command([cli,'--help'])
        command(['systemd-analyze','verify',verify,service,units/'flowcore-network.socket'])
        sdk = work/'consumer'
        sdk.mkdir()
        (sdk/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\nproject(consumer LANGUAGES CXX)\n'
            'find_package(flowcore CONFIG REQUIRED)\nadd_executable(consumer main.cpp)\n'
            'target_link_libraries(consumer PRIVATE flowcore::sdk)\n')
        (sdk/'main.cpp').write_text(
            '#include <flowcore/client.hpp>\n#include <flowcore/intent_v2.hpp>\n'
            'int main(int argc,char** argv){flowcore::Client c("/nonexistent");'
            'flowcore::v2::Intent i;i.service="installed";'
            'flowcore::v2::Service s(argc==2?argv[1]:"/nonexistent",i);'
            'if(argc==2){if(!s.wait_ready(std::chrono::seconds(3)))return 2;'
            'for(int n=0;n<16;++n){auto r=s.begin_request();(void)r.complete();'
            'if(!s.flush(std::chrono::seconds(3)))return 4;'
            'if(s.statistics().accepted>=2){auto t=s.transport_statistics();'
            'return t.session_connections==1 && !t.legacy_requests?0:5;}}return 3;} }\n')
        command(['cmake','-S',sdk,'-B',sdk/'build','-G','Ninja',f'-DCMAKE_PREFIX_PATH={prefix}'])
        command(['cmake','--build',sdk/'build'])
        command([sdk/'build/consumer'])
        with (work/'installed-intent.log').open('w') as log:
            proc=subprocess.Popen([daemon],env=env,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+3
                while not (runtime/'control.sock').exists():
                    assert proc.poll() is None and time.monotonic()<deadline
                    time.sleep(.01)
                command([sdk/'build/consumer',runtime/'control.sock'])
            finally:
                proc.terminate()
                proc.wait(timeout=5)
        for path in manifest:
            path.unlink()
        assert not any(path.is_file() for path in stage.rglob('*'))
    print('PASS: installed runtime, zero capabilities, parser rejection, watchdog, Prometheus, systemd unit, SDK consumer and staged uninstall')


if __name__=='__main__':
    main()
