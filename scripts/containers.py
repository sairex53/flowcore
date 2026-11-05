"""Podman inventory and a cgroup-v2 classifier for explicitly owned lab workloads."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from benchmark import owned_directory, run
from cli import request
from observer_perf import await_service, command, stop
from paths import BINDIR, RESULTS, ROOT, RUNTIME
from research_runner import child, exclusive, save
from service_config import CLASSES, load

CGROUP = Path('/sys/fs/cgroup')
SAFE_CGROUP = re.compile(r'/(?:[a-zA-Z0-9_.@:-]+/)*[a-zA-Z0-9_.@:-]+')
CONTAINER = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}')


def cgroup(pid):
    if type(pid) is not int or pid<=0:
        raise ValueError('container is not running')
    with open(f'/proc/{pid}/cgroup') as stream:
        path = next((line.rstrip().split(':',2)[2] for line in stream if line.startswith('0::')),None)
    if not path or len(path)>4096 or not SAFE_CGROUP.fullmatch(path) or any(part in ('.','..') for part in path.split('/')):
        raise ValueError('no supported non-root cgroup v2 path')
    directory = (CGROUP/path.lstrip('/')).resolve()
    if not directory.is_relative_to(CGROUP):
        raise ValueError('cgroup path escaped mount')
    return dict(path=path,id=directory.stat().st_ino,network_namespace=os.stat(f'/proc/{pid}/ns/net').st_ino)


def podman(store=None):
    args = ['podman']
    if store:
        store = Path(store)
        args += ['--root',store/'storage','--runroot',store/'run','--tmpdir',store/'tmp',
                 '--storage-driver=vfs','--events-backend=file','--cgroup-manager=cgroupfs']
    return args


def inspect_container(name, store=None):
    if not isinstance(name,str) or not CONTAINER.fullmatch(name):
        raise ValueError('invalid Podman container selector')
    row = json.loads(run([*podman(store),'container','inspect',name]))[0]
    state = row['State']
    result = dict(id=row['Id'],name=row['Name'],status=state['Status'],pid=state['Pid'])
    if state['Running']:
        try:
            result['cgroup'] = cgroup(state['Pid'])
        except (OSError,ValueError) as error:
            result['cgroup_error'] = str(error)
    return result


def inventory(store=None):
    rows = json.loads(run([*podman(store),'ps','--all','--format','json','--last','16']))
    return [dict(id=row.get('Id'),names=row.get('Names'),state=row.get('State'),status=row.get('Status'))
            for row in rows[:16]]


def attach(service, pid, traffic_class):
    if not service.research or not service.lab or traffic_class not in tuple(CLASSES):
        raise ValueError('cgroup classification requires the explicitly owned research lab')
    lab = service.lab
    identity = cgroup(pid)
    owned = os.fstat(lab.research_fds[0]).st_ino
    if identity['network_namespace']!=owned:
        raise ValueError('container does not share the owned sender namespace')
    policy = service.config['policies'][service.active]
    if policy['scheduler']!='htb':
        raise ValueError('cgroup classification requires HTB')
    path = identity['path'].strip('/')
    level = len(path.split('/'))
    rules = lab.output/'container.nft'
    if rules.exists():
        raise ValueError('one cgroup classifier per dedicated lab')
    rule = (f'add table inet flowcore_container\n'
            'add chain inet flowcore_container output { type filter hook output priority -150; policy accept; }\n'
            f'add rule inet flowcore_container output socket cgroupv2 level {level} {json.dumps(path)} '
            f'counter meta priority set 1:{CLASSES[traffic_class]}\n')
    tables = json.loads(lab.command(0,['nft','-j','list','tables']))
    if any(row.get('table',{}).get('name')=='flowcore_container' for row in tables['nftables']):
        raise ValueError('existing table is not owned by this classifier')
    rules.write_text(rule)
    committed = False
    try:
        lab.command(0,['nft','-f',rules])
        committed = True
        installed = json.loads(lab.command(0,['nft','-j','list','table','inet','flowcore_container']))
        if not any('rule' in row for row in installed['nftables']):
            raise RuntimeError('cgroup classifier readback missing')
        if cgroup(pid)!=identity:
            raise RuntimeError('container identity changed during classifier setup')
    except BaseException:
        try:
            if committed:
                lab.command(0,['nft','delete','table','inet','flowcore_container'])
        except subprocess.SubprocessError:
            pass
        rules.unlink(missing_ok=True)
        raise
    result = dict(pid=pid,cgroup=identity,traffic_class=traffic_class,active=True,
                ceiling_kbit=service.profile['bandwidth_kbit']*policy['classes'][traffic_class]['max_pct']//100)
    service.container_attachment = result
    return result


def attachment_sample(service):
    state = service.container_attachment
    if not state or not state['active']:
        return
    lab = service.lab
    try:
        actual = cgroup(state['pid'])
    except (OSError,ValueError):
        actual = None
    nft = json.loads(lab.command(0,['nft','-j','list','table','inet','flowcore_container']))
    counts = [expr['counter'] for row in nft['nftables'] for expr in row.get('rule',{}).get('expr',[])
              if 'counter' in expr]
    state['matched_egress'] = counts[0] if counts else None
    if actual!=state['cgroup']:
        lab.command(0,['nft','delete','table','inet','flowcore_container'])
        (lab.output/'container.nft').unlink(missing_ok=True)
        state['active'] = False
        state['event'] = 'container_exit_or_identity_change'
        service.log('container_classifier_removed',pid=state['pid'])
        return
    path = CGROUP/actual['path'].lstrip('/')
    try:
        state['memory_bytes'] = int((path/'memory.current').read_text())
    except (OSError,ValueError):
        state['memory_bytes'] = None


def rootfs(path):
    binary = BINDIR/'flowcore-container-request'
    target = path/'client'
    shutil.copy2(binary,target)
    libs = re.findall(r'(?:=>\s*)?(/[^\s()]+)\s*\(0x',run(['ldd',binary]))
    if not libs:
        raise RuntimeError('could not resolve container workload shared libraries')
    for name in libs:
        library = path/name.lstrip('/')
        library.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(name,library)


def experiment():
    with exclusive():
        path = RESULTS/f'container-{time.time_ns()}'
        owned_directory(path)
        store = path/'podman'
        gate = path/'gate'
        image = path/'rootfs'
        for directory in (store,gate,image):
            directory.mkdir()
        rootfs(image)
        archive = path/'image.tar'
        run(['tar','-cf',archive,'-C',image,'.'])
        args = podman(store)
        cfg = load(ROOT/'config/service.json')
        cfg['observability']['bpf'] = 'off'
        cfg['telemetry_seconds'] = 1
        cfg['policies']['default']['classes']['critical']['max_pct'] = 35
        save(path/'config.json',cfg)
        daemon = server = None
        name = 'fc-workload'
        with (path/'daemon.log').open('w') as dl,(path/'server.log').open('w') as sl:
            try:
                daemon = child([BINDIR/'flowcored','--research','--config',path/'config.json'],stdout=dl,stderr=dl)
                await_service(daemon)
                request('lab_up')
                sender,receiver = request('inspect')['namespaces']
                request('intent_register',intent={'name':'container_demo','priority':'critical'})
                server = child(command(receiver,'server',path/'rx.jsonl',['--request-port','9003']),stdout=sl,stderr=sl)
                from adaptive_experiment import await_ready
                await_ready(server,path/'server.log')
                run([*args,'import',archive,'localhost/flowcore-workload:local'])
                run([*args,'create','--name',name,'--network',f'ns:/proc/{sender}/ns/net','--cap-drop=all',
                     '--security-opt=no-new-privileges','--security-opt=label=disable',
                     '--volume',f'{gate}:/gate:ro','--volume',f'{RUNTIME}:/control:ro',
                     'localhost/flowcore-workload:local','/client'])
                run([*args,'start',name])
                active = inspect_container(name,store)
                save(path/'container-running.json',active)
                rule = request('platform_container_attach',pid=active['pid'],traffic_class='critical')
                save(path/'classification.json',rule)
                (gate/'start').touch()
                status = run([*args,'wait',name]).strip()
                save(path/'container-exited.json',inspect_container(name,store))
                workload = json.loads(run([*args,'logs',name]).strip().splitlines()[-1])
                view = request('inspect')
                obs = request('observe')
                deadline = time.monotonic()+4
                while time.monotonic()<deadline:
                    attachment = request('platform_container_status')
                    if not attachment['active']:
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('stale container classifier was not removed')
                stats = view['tc']['traffic_classes']
                result = dict(path=str(path),container_exit=int(status),workload=workload,
                    critical_bytes=stats['critical']['stats']['bytes'],critical_ceil_kbit=stats['critical']['ceil']//125,
                    classification=rule,container_telemetry=attachment,application_slo=obs['windows']['60']['application']['container_demo']['application'])
                save(path/'summary.json',result)
                if int(status) or result['critical_bytes']<4096*40:
                    raise RuntimeError(f'cgroup classified workload failed: {path}')
                return result
            finally:
                try:
                    run([*args,'rm','-f',name])
                except (OSError,subprocess.SubprocessError):
                    pass
                stop(server)
                stop(daemon)
