"""Native rtnetlink operations limited to a fresh helper-owned veth pair."""
import os
import socket
import struct

from network_state import HEADER, attrs, dump, messages

NEW_LINK, DEL_LINK, GET_LINK = 16, 17, 18
NEW_QDISC, DEL_QDISC, GET_QDISC = 36, 37, 38
CREATE_EXCLUSIVE = 0x600
ROOT = 0xffffffff
QUEUE_HANDLE = 0xf1000000
LINK = struct.Struct('=BBHiII')
QDISC = struct.Struct('=BBHiIII')
U32 = struct.Struct('=I')
NAMES = (b'fch0', b'fch1')


def attr(kind, value):
    size = 4 + len(value)
    return struct.pack('=HH', size, kind) + value + bytes(-size % 4)


class Network:
    def __init__(self, sock, tag):
        self.sock = sock
        self.tag = tag.encode()
        self.indices = None

    def ack(self, kind, payload, flags=0):
        seq = 1
        self.sock.sendto(HEADER.pack(16 + len(payload), kind, 5 | flags, seq, 0) + payload, (0, 0))
        data, _, flags, peer = self.sock.recvmsg(65536)
        if peer[0] or flags & socket.MSG_TRUNC:
            raise RuntimeError('invalid netlink ACK sender or length')
        for response, _, number, body in messages(data):
            if response == 2 and number == seq and len(body) >= 4:
                error, = struct.unpack_from('=i', body)
                if error:
                    raise OSError(-error, os.strerror(-error))
                return
        raise RuntimeError('missing netlink ACK')

    def links(self):
        result = {}
        for kind, body in dump(self.sock, GET_LINK, bytes(LINK.size), max_datagrams=8):
            if kind != NEW_LINK or len(result) >= 8:
                raise RuntimeError('unexpected namespace link inventory')
            _, _, _, index, _, _ = LINK.unpack_from(body)
            fields = attrs(body[LINK.size:])
            name = fields[3].rstrip(b'\0')
            result[name] = (index, attrs(fields.get(18, b'')).get(1, b'').rstrip(b'\0'),
                            fields.get(20, b'').rstrip(b'\0'),
                            U32.unpack(fields[5])[0] if 5 in fields else None)
        return result

    def owned(self, expected=None, *, newly_created=False):
        links = self.links()
        if set(links) != {b'lo', *NAMES}:
            raise RuntimeError('unexpected namespace links')
        first, second = (links[name] for name in NAMES)
        indices = first[0], second[0]
        aliases = (b'', self.tag) if newly_created else (self.tag,)
        if (expected is not None and indices != expected) or any(
                row[1] != b'veth' or row[2] not in aliases or row[3] != peer
                for row, peer in ((first, second[0]), (second, first[0]))):
            raise RuntimeError('veth ownership mismatch')
        return indices

    def create(self):
        if set(self.links()) != {b'lo'}:
            raise RuntimeError('namespace must contain only loopback before creation')
        def link_attrs(name):
            return attr(3, name + b'\0') + attr(20, self.tag + b'\0')
        peer = bytes(LINK.size) + link_attrs(NAMES[1])
        info = attr(1, b'veth\0') + attr(2, attr(1, peer))
        self.ack(NEW_LINK, bytes(LINK.size) + link_attrs(NAMES[0]) + attr(18, info), CREATE_EXCLUSIVE)
        indices = self.owned(newly_created=True)
        for index in indices:
            self.ack(NEW_LINK, LINK.pack(0, 0, 0, index, 1, 1) + attr(20, self.tag + b'\0'))
        self.indices = indices
        self.verify_queue(None)

    def verify_queue(self, expected):
        first, second = self.owned(self.indices)
        found = None
        for kind, body in dump(self.sock, GET_QDISC, bytes(QDISC.size), max_datagrams=8):
            if kind != NEW_QDISC:
                raise RuntimeError('unexpected qdisc message')
            _, _, _, index, handle, parent, _ = QDISC.unpack_from(body)
            if index not in (first, second):
                continue
            fields = attrs(body[QDISC.size:])
            scheduler = fields[1].rstrip(b'\0')
            if parent == ROOT and handle == 0 and scheduler == b'noqueue':
                continue
            if (index != first or parent != ROOT or handle != QUEUE_HANDLE
                    or scheduler != b'pfifo' or found is not None):
                raise RuntimeError('foreign qdisc on owned link')
            found, = U32.unpack(fields[2])
        if found != expected:
            raise RuntimeError('qdisc readback or ownership mismatch')

    def set_queue(self, old, limit):
        self.verify_queue(old)
        index = self.indices[0]
        payload = QDISC.pack(0, 0, 0, index, QUEUE_HANDLE, ROOT, 0)
        if limit is None:
            self.ack(DEL_QDISC, payload)
        else:
            payload += attr(1, b'pfifo\0') + attr(2, U32.pack(limit))
            self.ack(NEW_QDISC, payload, CREATE_EXCLUSIVE if old is None else 0)
        self.verify_queue(limit)

    def remove(self, limit):
        self.verify_queue(limit)
        self.ack(DEL_LINK, LINK.pack(0, 0, 0, self.indices[0], 0, 0))
        if set(self.links()) != {b'lo'}:
            raise RuntimeError('veth removal readback failed')
        self.indices = None
