"""Resource-bounded held TCP connection scale; not a 10k-service or BPF-capacity claim."""
import asyncio
import json
import os
import resource
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from benchmark import owned_directory
from service_lab import ServiceLab
from tcp_diag import connections as diagnostic_snapshot

def available():
    return int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                    if line.startswith('MemAvailable:')))*1024

async def trial(output,target):
    path=output/str(target);owned_directory(path)
    lab=ServiceLab(path);clients=[];writers=set();active=peak=0;server=None
    started=time.monotonic()
    before=len(list(Path('/proc/self/fd').iterdir()))
    samples=[];failures=0
    payload=b'flowcore-scale-1'
    async def serve(reader,writer):
        nonlocal active,peak
        writers.add(writer);active+=1;peak=max(peak,active)
        try:
            while True:
                data=await reader.readexactly(len(payload))
                writer.write(data);await writer.drain()
        except (asyncio.IncompleteReadError,ConnectionError,OSError):
            pass
        finally:
            active-=1;writers.discard(writer)
            writer.close()
            try:await writer.wait_closed()
            except (ConnectionError,OSError):pass
    result=dict(target=target,status='UNVERIFIED')
    try:
        lab.start()
        with lab.enter(1):
            server=await asyncio.start_server(serve,'10.203.0.2',19771,backlog=4096)
        with lab.enter(0):
            while len(clients)<target:
                if available()<768*1024**2:raise MemoryError('MemAvailable guard below 768 MiB')
                batch=min(128,target-len(clients))
                new=await asyncio.gather(*(asyncio.wait_for(
                    asyncio.open_connection('10.203.0.2',19771),5) for _ in range(batch)))
                clients.extend(new)
            await asyncio.sleep(.05)
            assert active==target,(active,target)
            diag,truncated=diagnostic_snapshot(128)
            async def exchange(reader,writer):
                nonlocal failures
                start=time.monotonic_ns()
                try:
                    writer.write(payload);await writer.drain()
                    data=await asyncio.wait_for(reader.readexactly(len(payload)),3)
                    if data!=payload:raise ValueError('payload mismatch')
                    samples.append((time.monotonic_ns()-start)/1e6)
                except (OSError,TimeoutError,ValueError,asyncio.IncompleteReadError):
                    failures+=1
            for offset in range(0,len(clients),128):
                await asyncio.gather(*(exchange(*pair) for pair in clients[offset:offset+128]))
            ordered=sorted(samples)
            result.update(status='PASS' if not failures else 'FAIL',established=len(clients),
                peak_server_connections=peak,completed=len(samples),failed=failures,
                successful_p99_ms=ordered[max(0,__import__('math').ceil(len(ordered)*.99)-1)] if ordered else None,
                snapshot_entries=len(diag),snapshot_limit=128,snapshot_truncated=truncated,
                max_concurrent_requests=128,available_memory_bytes=available(),
                peak_process_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                held_session_seconds=time.monotonic()-started)
    except (OSError,MemoryError,RuntimeError,TimeoutError) as error:
        result.update(status='RESOURCE_LIMIT',established=len(clients),reason=str(error))
    finally:
        for offset in range(0,len(clients),128):
            batch=clients[offset:offset+128]
            for reader,writer in batch:writer.close()
            await asyncio.gather(*(writer.wait_closed() for reader,writer in batch),return_exceptions=True)
        if server:server.close();await server.wait_closed()
        for writer in list(writers):writer.close()
        deadline=time.monotonic()+3
        while active and time.monotonic()<deadline:await asyncio.sleep(.01)
        result['remaining_server_connections']=active
        lab.close()
        result['fd_before']=before
        result['fd_after']=len(list(Path('/proc/self/fd').iterdir()))
        if active or result['fd_after']>before+2:result['status']='FAIL_CLEANUP'
    (path/'summary.json').write_text(json.dumps(result,indent=2))
    return result

async def main():
    if os.geteuid()!=0:raise PermissionError('isolated namespace runner requires root')
    soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
    output=ROOT/'results'/('release-scale-'+str(time.time_ns()));owned_directory(output)
    namespace=Path('/proc/self/ns/net').stat().st_ino
    rows=[]
    try:
        for target in (100,1000,10000):
            need=target*2+256
            if hard<need:
                rows.append(dict(target=target,status='RESOURCE_LIMIT',reason='hard FD limit'))
                break
            resource.setrlimit(resource.RLIMIT_NOFILE,(max(soft,need),hard))
            rows.append(await trial(output,target))
            if rows[-1]['status']!='PASS':break
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE,(soft,hard))
    assert Path('/proc/self/ns/net').stat().st_ino==namespace
    result=dict(kernel=os.uname().release,architecture=os.uname().machine,runs=rows,
                maximum_passed=max((r['target'] for r in rows if r['status']=='PASS'),default=0),
                scope='held TCP connections; 128 concurrent requests; bounded passive INET_DIAG only')
    (output/'summary.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(dict(output=str(output),**result)),flush=True)
if __name__=='__main__':asyncio.run(main())
