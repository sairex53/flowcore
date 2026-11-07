"""FLOWCORE client and local interface discovery."""
import argparse
import json
import socket
import sys
import time

from daemon import SOCKET
from network_state import snapshot
from service_config import LAB_FIELDS
from observability import FAILURE_REASONS
from telemetry_export import connection_csv, prometheus


def request(op, **fields):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(120)
        sock.connect(str(SOCKET))
        sock.sendall(json.dumps(dict(op=op, **fields)).encode() + b'\n')
        with sock.makefile('rb') as stream:
            reply = stream.readline(1024 * 1024 + 1)
        if not reply.endswith(b'\n') or len(reply) > 1024 * 1024:
            raise RuntimeError('invalid daemon reply')
        result = json.loads(reply)
        if not result['ok']:
            raise ValueError(result['error'])
        return result['result']


def main():
    parser = argparse.ArgumentParser(prog='flowcore', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'interfaces', 'metrics', 'observe', 'connections', 'congestion', 'events', 'health', 'lifecycles'):
        command = commands.add_parser(name, help=f'query {name}')
        command.add_argument('--json', action='store_true', help='compact JSON output')
        if name == 'interfaces':
            command.add_argument('--local', action='store_true', help='query current namespace directly')
        if name in ('observe', 'connections', 'congestion', 'events', 'health', 'metrics', 'lifecycles'):
            command.add_argument('--watch', type=float, metavar='SECONDS', help='emit successive JSON lines')
        if name == 'observe':
            command.add_argument('--interface', help='controlled sender interface')
        if name in ('connections','lifecycles'):
            command.add_argument('--limit', type=int, default=128)
            command.add_argument('--offset', type=int, default=0)
            command.add_argument('--csv', action='store_true')
        if name == 'events':
            command.add_argument('--jsonl', action='store_true')
        if name == 'metrics':
            command.add_argument('--prometheus', action='store_true')
    diagnose = commands.add_parser('diagnose', help='explain retained SLO and network evidence')
    diagnose.add_argument('--service')
    diagnose.add_argument('--lab-intervention', choices=('disable','freeze','restore'),
                          help='explicit, bounded operation on a privileged research daemon')
    incident = commands.add_parser('incident', help='inspect or replay bounded evidence').add_subparsers(dest='action', required=True)
    incident.add_parser('list')
    for action in ('inspect', 'export'):
        incident.add_parser(action).add_argument('id')
    incident.add_parser('replay').add_argument('file')
    operational=commands.add_parser('ops',help='deployment health and conservative control')
    operational.add_argument('action',choices=('status','validate','reload','emergency','diagnostics'))
    flows=commands.add_parser('flows',help='read-only current-namespace TCP/UDP fallback; never attaches BPF')
    flows.add_argument('--limit',type=int,default=128)
    flows.add_argument('--json',action='store_true')
    report = commands.add_parser('report', help='submit explicit application timings')
    reports = report.add_subparsers(dest='action', required=True)
    for action in ('sample', 'start', 'complete'):
        sub = reports.add_parser(action)
        sub.add_argument('service')
        if action != 'start':
            sub.add_argument('--outcome', choices=('ok', 'timeout', 'failure'), required=True)
            sub.add_argument('--failure-reason', choices=FAILURE_REASONS)
        if action == 'sample':
            sub.add_argument('--duration-us', type=float, required=True)
        if action == 'complete':
            sub.add_argument('--token', required=True)
        else:
            sub.add_argument('--deadline-ms', type=float)
    groups = dict(policy=('list', 'apply', 'inspect', 'evaluate', 'explain', 'history'), daemon=('status',),
                  intent=('register', 'list', 'inspect', 'remove', 'revoke'),
                  controller=('start', 'stop', 'status', 'shadow', 'observe', 'fallback'),
                  lab=('up', 'down', 'status', 'configure', 'inspect'))
    groups.update(bottleneck=('inspect', 'watch'), capacity=('estimate',),
                  qos=('direction',), path=('diagnostics',))
    for name, operations in groups.items():
        sub = commands.add_parser(name, help=f'{name} commands').add_subparsers(dest='action', required=True)
        for action in operations:
            command = sub.add_parser(action, help=f'{action} {name}')
            command.add_argument('--json', action='store_true', help='compact JSON output')
            if (name, action) == ('intent', 'register'):
                command.add_argument('file', help='JSON intent file')
            if (name == 'intent' and action in ('inspect', 'remove', 'revoke')):
                command.add_argument('name')
            if (name == 'controller' and action in ('start', 'shadow', 'observe')):
                command.add_argument('--strategy', choices=('static', 'threshold', 'slo', 'queue-aware', 'slo-aware', 'hybrid', 'predictive'), default='slo')
            if (name, action) == ('policy', 'evaluate'):
                command.add_argument('--dry-run', action='store_true', help='evaluation never writes tc')
            if (name, action) == ('policy', 'apply'):
                command.add_argument('name')
            if name in ('bottleneck', 'capacity', 'qos', 'path'):
                command.add_argument('--interface')
            if (name, action) == ('bottleneck', 'watch'):
                command.add_argument('--watch', type=float, default=1, metavar='SECONDS')
                command.add_argument('--count', type=int, default=0, help='number of JSON lines, zero means continuous')
            if (name, action) == ('lab', 'up'):
                command.add_argument('--ingress-kbit', type=int, help='opt in to IFB in a fresh owned lab')
            if (name, action) == ('lab', 'configure'):
                for field in LAB_FIELDS:
                    command.add_argument('--' + field.replace('_', '-'), required=True,
                                         type=int if field == 'bandwidth_kbit' else float)
    from research_cli import add_commands
    add_commands(commands)
    from platform_cli import add_commands as add_platform
    add_platform(commands)
    from helper_cli import add_commands as add_network
    add_network(commands)
    args = parser.parse_args()
    watch = getattr(args, 'watch', None)
    if watch is not None and not .1 <= watch <= 60:
        parser.error('--watch must be between .1 and 60 seconds')
    if watch and (getattr(args, 'csv', False) or getattr(args, 'prometheus', False)):
        parser.error('--watch supports JSON and JSONL only')
    try:
        if args.command in ('diagnose','incident'):
            if args.command == 'diagnose':
                if args.lab_intervention:
                    if args.service is not None:
                        raise ValueError('--service cannot be combined with --lab-intervention')
                    result = request('diagnostic_intervention', action=args.lab_intervention)
                else:
                    result = request('diagnose', **({'service':args.service} if args.service is not None else {}))
            elif args.action == 'replay':
                from incident_runtime import replay_file
                result = replay_file(args.file)
            else:
                result = request('incident_'+args.action, **({'id':args.id} if hasattr(args,'id') else {}))
            print(json.dumps(result, indent=None if getattr(args,'action',None) == 'export' else 2))
            return 0
        if args.command in ('bottleneck', 'capacity', 'qos', 'path'):
            fields = {'interface': args.interface} if args.interface is not None else {}
            operation = 'bottleneck_inspect' if args.command == 'bottleneck' else args.command+'_'+args.action
            count = getattr(args, 'count', 1)
            if not 0 <= count <= 10000:
                raise ValueError('--count must be in [0, 10000]')
            emitted = 0
            while not count or emitted < count:
                result = request(operation, **fields)
                print(json.dumps(result, indent=None if args.json or watch else 2), flush=True)
                emitted += 1
                if watch and (not count or emitted < count):
                    time.sleep(watch)
            return 0
        if args.command=='flows':
            from flow_snapshot import snapshot as flow_snapshot
            print(json.dumps(flow_snapshot(args.limit),indent=None if args.json else 2))
            return 0
        if args.command == 'network':
            from helper_cli import dispatch
            print(json.dumps(dispatch(args), indent=2))
            return 0
        if args.command=='platform':
            from platform_cli import dispatch
            print(json.dumps(dispatch(args),indent=2))
            return 0
        if args.command in ('bench','chaos'):
            from research_cli import dispatch
            result,code = dispatch(args)
            print(json.dumps(result,indent=2))
            return code
        if args.command in ('observe', 'connections', 'congestion', 'events', 'health', 'metrics', 'lifecycles'):
            sequence = 0
            while True:
                fields = {}
                if args.command == 'observe' and args.interface:
                    fields['interface'] = args.interface
                if args.command in ('connections','lifecycles'):
                    fields.update(limit=args.limit, offset=args.offset)
                if args.command == 'events':
                    fields['after'] = sequence
                result = request(args.command, **fields)
                if getattr(args, 'prometheus', False):
                    print('\n'.join(prometheus(result['observability'],result)))
                elif getattr(args, 'csv', False):
                    connection_csv(result['connections'], sys.stdout)
                elif args.command == 'events' and (args.jsonl or watch):
                    if result['last_sequence'] < sequence:
                        sequence = 0
                    if result['missed']:
                        print(json.dumps({'kind': 'stream_gap', 'missed': result['missed'],
                                          'first_sequence': result['first_sequence']}), flush=True)
                    for event in result['events']:
                        if event['sequence'] > sequence:
                            print(json.dumps(event), flush=True)
                    sequence = result['last_sequence']
                else:
                    print(json.dumps(result, indent=None if args.json or watch else 2), flush=True)
                if watch is None:
                    return 0
                time.sleep(watch)
        if args.command=='ops':
            print(json.dumps(request('ops_'+args.action),indent=2))
            return 0
        if args.command == 'report':
            fields = {key: getattr(args, key) for key in ('service', 'outcome', 'duration_us', 'deadline_ms', 'token', 'failure_reason')
                      if hasattr(args, key) and getattr(args, key) is not None}
            operation = {'start': 'request_start', 'complete': 'request_complete', 'sample': 'request_sample'}
            print(json.dumps(request(operation[args.action], **fields)))
            return 0
        if args.command == 'interfaces' and args.local:
            result = snapshot()
        elif args.command == 'intent':
            fields = {}
            if args.action == 'register':
                with open(args.file) as stream:
                    fields['intent'] = json.loads(stream.read(65536))
            if args.action in ('inspect', 'remove', 'revoke'):
                fields['name'] = args.name
            result = request('intent_v2_revoke' if args.action=='revoke' else 'intent_' + args.action, **fields)
        elif args.command == 'controller':
            fields = {'strategy': args.strategy} if hasattr(args, 'strategy') else {}
            result = request('controller_' + args.action, **fields)
        elif args.command == 'policy':
            if args.action == 'list':
                result = request('policies')
            elif args.action == 'apply':
                result = request('policy_apply', name=args.name)
            elif args.action in ('evaluate', 'explain', 'history'):
                result = request('policy_' + args.action)
            else:
                result = request('inspect')
        elif args.command == 'lab':
            if args.action == 'configure':
                result = request('lab_configure', profile={name: getattr(args, name) for name in LAB_FIELDS})
            elif args.action == 'up' and args.ingress_kbit is not None:
                result = request('lab_up', ingress_kbit=args.ingress_kbit)
            else:
                result = request('inspect' if args.action == 'inspect' else 'lab_' + args.action)
        else:
            result = request('status' if args.command == 'daemon' else args.command)
        print(json.dumps(result, indent=None if args.json else 2))
        return 0
    except KeyboardInterrupt:
        return 130 if args.command in ('bench','chaos') else 0
    except OSError as error:
        print(f'flowcore: daemon connection or Linux query failed: {error}', file=sys.stderr)
        return 3
    except (ValueError, RuntimeError) as error:
        print(f'flowcore: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
