"""Fleet operator commands using the existing Fabric TLS credentials."""
from pathlib import Path

from durable_state import read_json
from fabric_protocol import endpoint, fields, remote, tls
from fleet_policy import MODES


def add_commands(commands):
    root = commands.add_parser('fleet', help='validate, stage and audit a Git policy rollout')
    sub = root.add_subparsers(dest='action', required=True)
    for action in ('plan', 'validate', 'rollout', 'status', 'rollback', 'history'):
        parser = sub.add_parser(action)
        parser.add_argument('--config', type=Path, required=True, help='Fabric operator TLS configuration')
        parser.add_argument('--json', action='store_true', help='compact JSON output')
        if action in ('plan', 'validate', 'rollout'):
            parser.add_argument('--repository', required=True, help='coordinator repository alias')
            parser.add_argument('--commit', required=True, help='full Git commit object ID')
            parser.add_argument('--mode', choices=MODES, default='full')
            parser.add_argument('--percent', type=int)
        if action == 'rollout':
            parser.add_argument('--approve', help='approval digest from the reviewed plan')
        if action == 'rollback':
            parser.add_argument('--generation', required=True, type=int)
            parser.add_argument('--approval', required=True, help='active approval digest from fleet status')
        if action == 'history':
            parser.add_argument('--limit', type=int, default=8)
            parser.add_argument('--offset', type=int, default=0)


def dispatch(args):
    cfg = fields(read_json(args.config), ('coordinator', 'tls'))
    message = {'op': 'fleet_'+args.action}
    for key in ('repository', 'commit', 'mode', 'percent', 'approve', 'generation', 'approval', 'limit', 'offset'):
        value = getattr(args, key, None)
        if value is not None:
            message[key] = value
    return remote(tls(cfg['tls']), endpoint(cfg['coordinator']), message)
