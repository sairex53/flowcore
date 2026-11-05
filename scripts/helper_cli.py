"""Direct operator client. This socket is independent of SDK and daemon IPC."""
import json
import socket
import struct

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
    for action in ('status', 'create', 'remove', 'queue-set', 'queue-clear'):
        command = operations.add_parser(action)
        if action != 'status':
            command.add_argument('--epoch', required=True)
            command.add_argument('--revision', type=int, required=True)
        if action == 'queue-set':
            command.add_argument('limit_packets', type=int)


def dispatch(args):
    op = {'status': 'status', 'create': 'lab.create', 'remove': 'lab.remove',
          'queue-set': 'queue.set', 'queue-clear': 'queue.clear'}[args.action]
    fields = {} if op == 'status' else dict(epoch=args.epoch, revision=args.revision)
    if op == 'queue.set':
        fields['limit_packets'] = args.limit_packets
    return call(args.socket, op, **fields)
