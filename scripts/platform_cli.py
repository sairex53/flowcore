"""Operations, profiling and experimental lab commands."""
from pathlib import Path
from cli import request
from platform_policy import load_json


def add_commands(commands):
    group = commands.add_parser('platform',help='interfaces, profiles and optional networking labs')
    sub = group.add_subparsers(dest='action',required=True)
    for name in ('interfaces','policies','mptcp','multipath-lab','container-lab','container-status'):
        sub.add_parser(name)
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
    if action in ('mptcp','multipath-lab'):
        from multipath import capabilities, experiment
        return capabilities() if action=='mptcp' else experiment()
    from containers import experiment, inspect_container, inventory
    if action=='containers':
        return inventory(args.store)
    if action=='container-inspect':
        return inspect_container(args.name,args.store)
    return experiment()
