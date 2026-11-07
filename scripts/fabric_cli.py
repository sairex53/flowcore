"""Operator CLI for Fabric. Credentials are separate from enrolled agent identities."""
from pathlib import Path

from durable_state import read_json
from fabric_protocol import endpoint, fields, remote, tls


def add_commands(commands):
    parser = commands.add_parser('fabric', help='query and reconcile a mutually authenticated Fabric')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('status', 'agents', 'services', 'policies', 'diagnostics', 'reconcile'):
        command = sub.add_parser(action)
        command.add_argument('--config', required=True, type=Path, help='operator TLS configuration')
        command.add_argument('--json', action='store_true', help='compact JSON output')
        if action == 'reconcile':
            command.add_argument('--apply', action='store_true', help='commit the next verified rollout phase')


def dispatch(args):
    cfg = fields(read_json(args.config), ('coordinator', 'tls'))
    command = {'op': args.action}
    if args.action == 'reconcile':
        command['apply'] = args.apply
    return remote(tls(cfg['tls']), endpoint(cfg['coordinator']), command)
