"""Direct operator client. This socket is independent of SDK and daemon IPC."""
import json
import socket
import struct
import time

SOCKET_PATH = '/run/flowcore-helper/control.sock'


def call(path, op, **fields):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as peer:
        peer.settimeout(5)
        peer.connect(path)
        _, uid, _ = struct.unpack('=3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0:
            raise PermissionError('network helper listener must be owned by root')
        peer.sendall(json.dumps(dict(version=1, op=op, **fields)).encode())
        payload, _, flags, _ = peer.recvmsg(4096)
    if not payload or flags & socket.MSG_TRUNC:
        raise RuntimeError('invalid helper reply')
    reply = json.loads(payload)
    if type(reply.get('version')) is not int or reply['version'] != 1:
        raise RuntimeError('invalid helper reply version')
    if reply.get('ok') is not True:
        raise ValueError(f"network helper rejected request ({reply.get('code', 'unknown')})")
    return reply['result']


def add_commands(commands):
    parser = commands.add_parser('network', help='operate the isolated privileged helper')
    parser.add_argument('--socket', default=SOCKET_PATH)
    operations = parser.add_subparsers(dest='action', required=True)
    for action in ('publish', 'exercise'):
        command = operations.add_parser(action)
        command.add_argument('--directory', required=True)
        command.add_argument('--name', required=True)
        if action == 'exercise':
            command.add_argument('--limit-packets', type=int, default=128)
    for action in ('status', 'create', 'remove', 'queue-set', 'queue-clear'):
        command = operations.add_parser(action)
        if action != 'status':
            command.add_argument('--epoch', required=True)
            command.add_argument('--revision', type=int, required=True)
        if action == 'queue-set':
            command.add_argument('limit_packets', type=int)


def exercise(path, limit):
    if not 1 <= limit <= 4096:
        raise ValueError('expected queue limit in [1, 4096]')
    before = call(path, 'status')
    if not before['verified'] or before['lab_up']:
        raise ValueError('exercise requires an empty verified helper namespace')
    current = call(path, 'lab.create', epoch=before['epoch'], revision=before['revision'])
    readings = [before, current]
    try:
        for operation, fields in (('queue.set', dict(limit_packets=limit)), ('queue.clear', {})):
            time.sleep(.06)
            current = call(path, operation, epoch=current['epoch'], revision=current['revision'], **fields)
            readings.append(current)
    finally:
        time.sleep(.06)
        # A lost acknowledgement or another writer can change the generation.
        # The helper refuses cleanup based on a stale epoch/revision.
        readings.append(call(path, 'lab.remove', epoch=current['epoch'], revision=current['revision']))
    return dict(schema=1, exercise='owned_veth_fifo', observations=readings)


def dispatch(args):
    if args.action in ('publish', 'exercise'):
        from result_handoff import destination, publish
        with destination(args.directory, args.name) as directory:
            result = (exercise(args.socket, args.limit_packets) if args.action == 'exercise' else
                      dict(schema=1, observation=call(args.socket, 'status')))
            publish(directory, args.name, result)
        return dict(published=args.name, **result)
    op = {'status': 'status', 'create': 'lab.create', 'remove': 'lab.remove',
          'queue-set': 'queue.set', 'queue-clear': 'queue.clear'}[args.action]
    fields = {} if op == 'status' else dict(epoch=args.epoch, revision=args.revision)
    if op == 'queue.set':
        fields['limit_packets'] = args.limit_packets
    return call(args.socket, op, **fields)
