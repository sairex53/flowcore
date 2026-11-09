"""Operator controls for the strategy supervisor and reproducible evidence."""
from pathlib import Path

from strategy_registry import CLASS, STRATEGIES, inventory, profile, recommend


def add_commands(commands):
    strategy = commands.add_parser('strategy', help='inspect and evaluate network control strategies')
    actions = strategy.add_subparsers(dest='action', required=True)
    for name in ('list','inspect','rollback'):
        actions.add_parser(name)
    shadow = actions.add_parser('shadow', help='replace class shadow subscriptions; omit names to clear')
    shadow.add_argument('names', nargs='*', metavar='STRATEGY')
    shadow.add_argument('--seconds', type=int, default=30)
    compare = actions.add_parser('compare')
    compare.add_argument('summary', type=Path)
    compare.add_argument('--baseline')
    compare.add_argument('--candidate')
    select = actions.add_parser('recommend')
    select.add_argument('summary', type=Path)
    select.add_argument('--context', type=Path, required=True)
    select.add_argument('--eligible', choices=tuple(STRATEGIES), nargs='+', default=list(STRATEGIES))
    select.add_argument('--guards', type=Path, required=True)
    select.add_argument('--switch-cost', type=float, default=.05)
    active = actions.add_parser('activate', help='authorize one expiring owned-lab class canary')
    active.add_argument('name', choices=(*CLASS[1:], 'auto'))
    active.add_argument('--scope', choices=['owned-fc0'], required=True)
    active.add_argument('--seconds', type=int, required=True)
    active.add_argument('--guards', type=Path, required=True)
    active.add_argument('--confirm', action='store_true', required=True)
    active.add_argument('--summary', type=Path, help='paired Research Lab summary required by auto')
    active.add_argument('--context', type=Path, help='declared workload manifest required by auto')
    active.add_argument('--switch-cost', type=float, default=.05)


def dispatch(args):
    from cli import request
    from durable_state import read_json
    from research_manifest import load_manifest, net_profile
    from research_stats import compare, load_summary
    op = 'strategy_'+args.action
    if args.action=='list':
        try:
            return request(op)
        except (ConnectionError, FileNotFoundError, PermissionError):
            return inventory()
    if args.action in ('inspect','rollback'):
        return request(op)
    if args.action=='shadow':
        return request(op,names=args.names,seconds=args.seconds)
    if args.action=='activate':
        limits = read_json(args.guards, 2048)
        name = args.name
        selection = None
        if name == 'auto':
            if args.summary is None or args.context is None:
                raise ValueError('auto activation requires --summary and --context')
            context = load_manifest(args.context)
            available = request('strategy_list')
            eligible = [key for key in CLASS[1:] if available[key]['canary']]
            selection = recommend(load_summary(args.summary), context, eligible, limits,
                                  args.switch_cost, available)
            name = selection['selected']
            if name == 'static':
                return dict(armed=False, selection=selection)
            if request('status')['profile'] != net_profile(context['network']):
                raise ValueError('live lab network profile differs from the evidence context')
        elif args.summary is not None or args.context is not None:
            raise ValueError('--summary and --context are supported only with auto activation')
        result = request(op, name=name, scope=args.scope, seconds=args.seconds, guards=limits, confirm=args.confirm)
        return result if selection is None else dict(armed=True, selection=selection, canary=result)
    summary = load_summary(args.summary)
    if args.action=='recommend':
        return recommend(summary,load_manifest(args.context),args.eligible,read_json(args.guards,2048),
                         args.switch_cost,inventory())
    if bool(args.baseline) != bool(args.candidate):
        raise ValueError('both modes are required for a paired comparison')
    if not args.baseline:
        return profile(summary)
    return compare(summary,summary,args.baseline,args.candidate)
