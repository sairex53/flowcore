"""Opt-in isolated transport comparison with explicit netem impairments."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from benchmark import owned_directory, TC
from paths import BINDIR, RESULTS
from research_runner import exclusive
from service_lab import ServiceLab
from observer_perf import stop
from lab_tls import certificate

ROOT=Path(__file__).resolve().parents[1]


def main():
    if os.geteuid()!=0:raise PermissionError('isolated namespace runner requires root')
    with exclusive():
        output=RESULTS/('transport-'+str(time.time_ns()));owned_directory(output)
        # Test-only private key lives under build and is removed on all exits.
        import tempfile
        with tempfile.TemporaryDirectory(dir=output,prefix='quic-cert-') as secret:
            certpath,keypath=certificate(secret)
            runtime=output/'runtime'
            env=dict(os.environ,FLOWCORE_RUNTIME=str(runtime),FLOWCORE_STATE=str(output/'state'))
            log=(output/'daemon.log').open('w')
            daemon=subprocess.Popen([BINDIR/'flowcored'],env=env,stdout=log,stderr=log)
            summaries=[]
            try:
                deadline=time.monotonic()+5
                while not (runtime/'control.sock').exists():
                    if daemon.poll() is not None or time.monotonic()>deadline:raise RuntimeError('daemon not ready')
                    time.sleep(.03)
                for scenario,options in (
                    ('stable',[]),('loss',['loss','random','0.5%']),
                    ('reorder',['delay','10ms','reorder','25%','50%']),
                    ('bandwidth',['rate','1000kbit']),
                    ('interface',['delay','2ms'])):
                    path=output/scenario;owned_directory(path)
                    lab=ServiceLab(path);server=client=None
                    with (path/'server.log').open('w') as sl,(path/'client.log').open('w') as cl:
                        try:
                            lab.start()
                            if options:
                                lab.command(0,[TC,'qdisc','add','dev','fc0','root','netem','limit','1000',*options,'seed','20261107'])
                            ready=path/'ready'
                            server=subprocess.Popen(lab.ns(1,[sys.executable,'-B',ROOT/'scripts/transport_echo.py','server',
                                '--cert',certpath,'--key',keypath,'--ready',ready]),stdout=sl,stderr=sl)
                            deadline=time.monotonic()+5
                            while not ready.exists():
                                if server.poll() is not None or time.monotonic()>deadline:raise RuntimeError('server startup failed')
                                time.sleep(.03)
                            before=json.loads(lab.command(0,[TC,'-j','-s','qdisc','show','dev','fc0']))
                            gate_args=['--active',path/'active','--go',path/'go'] if scenario=='interface' else []
                            client=subprocess.Popen(lab.ns(0,[sys.executable,'-B',ROOT/'scripts/transport_echo.py','client',
                                '--cert',certpath,'--host','10.203.0.2','--socket',runtime/'control.sock',
                                '--output',path/'requests.json','--requests','20',*gate_args]),env=env,stdout=cl,stderr=cl)
                            events=[]
                            if scenario=='interface':
                                deadline=time.monotonic()+15
                                while not (path/'active').exists():
                                    if client.poll() is not None or time.monotonic()>deadline:raise RuntimeError('fault synchronization failed')
                                    time.sleep(.01)
                                for up in (False,True):
                                    start=time.monotonic_ns()
                                    lab.command(0,['ip','link','set','fc0','up' if up else 'down'])
                                    events.append(dict(up=up,applied_ns=start,confirmed_ns=time.monotonic_ns(),
                                        state=next(row for row in lab.network(0)['interfaces'] if row['name']=='fc0')))
                                    if not up:
                                        (path/'go').touch()
                                        time.sleep(.3)
                            code=client.wait(timeout=90)
                            after=json.loads(lab.command(0,[TC,'-j','-s','qdisc','show','dev','fc0']))
                            result=dict(scenario=scenario,exit_code=code,configured=options,qdisc_before=before,qdisc_after=after,events=events)
                            if (path/'requests.json').exists():result['workload']=json.loads((path/'requests.json').read_text())
                            (path/'summary.json').write_text(json.dumps(result,indent=2))
                            summaries.append(result)
                            if code:raise RuntimeError('client failed: '+str(path/'client.log'))
                        finally:
                            stop(client);stop(server);lab.close()
                (output/'summary.json').write_text(json.dumps(dict(scenarios=summaries,comparison='INCONCLUSIVE',repetitions=1,seed=20261107,kernel=os.uname().release,architecture=os.uname().machine,python=sys.version,aioquic=__import__('aioquic').__version__),indent=2))
                print(output)
            finally:
                stop(daemon);log.close()

if __name__=='__main__':main()
