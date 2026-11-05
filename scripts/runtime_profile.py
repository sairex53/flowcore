"""Reproducible bounded histogram profile and ordinary-user daemon IPC probe."""
import argparse
import cProfile
import json
import os
import pstats
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from telemetry_windows import Windows

ROOT=Path(__file__).resolve().parents[1]


def aggregation():
    windows=Windows(61)
    for second in range(60):
        bucket=windows.at(second*10**9)
        for service in range(8):
            for value in (1000,2000,4000,8000,16000):
                bucket.observe(f'app.{service}',value)
    begin=time.process_time_ns()
    for _ in range(300):
        for width in (1,10,60):
            result=windows.summarize(59*10**9,width)
            for histogram in result.histograms.values():
                histogram.summary()
    return dict(cpu_ns=time.process_time_ns()-begin,
                fixture_seconds=60,services=8,summaries=900)


def daemon_probe():
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='profile-') as directory:
        directory=Path(directory)
        env=dict(os.environ,FLOWCORE_RUNTIME=str(directory/'runtime'),FLOWCORE_STATE=str(directory/'state'))
        with (directory/'daemon.log').open('w') as log:
            proc=subprocess.Popen([ROOT/'build/flowcored','--monitor-only'],env=env,stdout=log,stderr=log)
            try:
                path=directory/'runtime/control.sock'
                for _ in range(100):
                    if path.exists(): break
                    if proc.poll() is not None: raise RuntimeError('daemon startup failed')
                    time.sleep(.05)
                def stat():
                    fields=Path(f'/proc/{proc.pid}/stat').read_text().rsplit(')',1)[1].split()
                    status=Path(f'/proc/{proc.pid}/status').read_text()
                    values={line.split(':')[0]:line.split(':')[1].strip() for line in status.splitlines() if ':' in line}
                    return dict(cpu_ticks=int(fields[11])+int(fields[12]),
                                rss_bytes=int(values['VmRSS'].split()[0])*1024,
                                voluntary=int(values['voluntary_ctxt_switches']),
                                involuntary=int(values['nonvoluntary_ctxt_switches']))
                before=stat()
                time.sleep(3)
                idle=stat()
                durations=[]
                for _ in range(100):
                    begin=time.monotonic_ns()
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                        peer.settimeout(3)
                        peer.connect(str(path))
                        peer.sendall(b'{"op":"observe"}\n')
                        with peer.makefile('rb') as stream: reply=json.loads(stream.readline(1024*1024))
                        assert reply['ok']
                    durations.append(time.monotonic_ns()-begin)
                after=stat()
                return dict(idle_before=before,idle_after=idle,poll_after=after,
                            ipc_median_ns=sorted(durations)[50],ipc_p99_ns=sorted(durations)[98])
            finally:
                proc.terminate()
                proc.wait(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--max-regression',type=float,default=.25)
    args=parser.parse_args()
    if not 0<=args.max_regression<=10:
        parser.error('max regression must be between zero and ten')
    profile=cProfile.Profile()
    profile.enable()
    result=aggregation()
    profile.disable()
    profile.dump_stats(str(args.output)+'.prof')
    result['daemon']=daemon_probe()
    result['kernel']=os.uname().release
    result['clock_ticks']=os.sysconf('SC_CLK_TCK')
    if args.baseline:
        baseline=json.loads(args.baseline.read_text())
        pairs={'aggregation_cpu':(baseline['cpu_ns'],result['cpu_ns']),
               'idle_rss':(baseline['daemon']['idle_after']['rss_bytes'],result['daemon']['idle_after']['rss_bytes']),
               'ipc_p99':(baseline['daemon']['ipc_p99_ns'],result['daemon']['ipc_p99_ns'])}
        result['regression_gate']={name:dict(ratio=after/before,
             outcome='REGRESSION' if after>before*(1+args.max_regression) else 'PASS')
             for name,(before,after) in pairs.items()}
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    pstats.Stats(profile).sort_stats('cumulative').print_stats(8)
    if any(item['outcome']=='REGRESSION' for item in result.get('regression_gate',{}).values()):
        raise SystemExit(1)
