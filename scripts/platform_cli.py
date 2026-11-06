"""Operations, profiling and experimental lab commands."""
from pathlib import Path
from cli import request
from platform_policy import load_json


def add_commands(commands):
    group = commands.add_parser('platform',help='interfaces, profiles and optional networking labs')
    sub = group.add_subparsers(dest='action',required=True)
    for name in ('interfaces','policies','mptcp','container-lab','container-status'):
        sub.add_parser(name)
    from multipath_engine import STRATEGIES
    multipath=sub.add_parser('multipath-lab',help='run an owned multi-path MPTCP experiment')
    multipath.add_argument('--strategy',choices=(*STRATEGIES,'single-path-tcp'),default='linux-default')
    multipath.add_argument('--scenario',choices=('stable','asymmetric','lossy','primary-failover','multiple-failures'),default='primary-failover')
    multipath.add_argument('--paths',type=int,default=2)
    multipath.add_argument('--duration',type=float,default=10)
    multipath.add_argument('--target-latency-ms',type=float)
    multipath.add_argument('--service')
    multipath.add_argument('--repetitions',type=int)
    multipath.add_argument('--seed',type=int,default=20261107)
    multipath.add_argument('--compare',action='store_true',help='run the TCP, Linux-default, static and FLOWCORE policy matrix')
    commit = sub.add_parser('commit')
    commit.add_argument('file',type=Path)
    commit.add_argument('--revision',type=int,required=True)
    for name in ('resolve','apply-intent'):
        cmd = sub.add_parser(name)
        selectors = ('service','container') if name=='apply-intent' else ('device','interface','service','container')
        for selector in selectors:
            cmd.add_argument('--'+selector,required=name=='apply-intent' and selector=='service')
    for name in ('containers','container-inspect'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--store',type=Path)
        if name=='container-inspect':
            cmd.add_argument('name')


def dispatch(args):
    action = args.action
    if action in ('interfaces','policies','container-status'):
        return request('platform_'+action.replace('-','_'))
    if action=='commit':
        return request('platform_commit',policy=load_json(args.file),revision=args.revision)
    if action in ('resolve','apply-intent'):
        fields = {key:getattr(args,key) for key in ('device','interface','service','container')
                  if getattr(args,key,None) is not None}
        return request('platform_'+action.replace('-','_'),**fields)
    if action=='mptcp':
        from multipath import capabilities
        return capabilities()
    if action=='multipath-lab':
        from multipath import experiment
        service_intent=None
        target=args.target_latency_ms
        if args.service:
            service_intent=request('platform_resolve',service=args.service)
            if target is None:
                target=service_intent.get('fields',{}).get('target_p99_ms')
        repetitions=args.repetitions if args.repetitions is not None else (3 if args.compare else 1)
        return experiment(strategy=args.strategy,scenario=args.scenario,path_count=args.paths,
            duration=args.duration,target_latency_ms=target,service_name=args.service,
            repetitions=repetitions,compare=args.compare,seed=args.seed,service_intent=service_intent)
    from containers import experiment, inspect_container, inventory
    if action=='containers':
        return inventory(args.store)
    if action=='container-inspect':
        return inspect_container(args.name,args.store)
    return experiment()
