"""Read Linux link, address and route state through rtnetlink."""
import ipaddress
import os
import socket
import struct
from pathlib import Path

HEADER = struct.Struct('=IHHII')


def messages(data):
    offset = 0
    while offset + HEADER.size <= len(data):
        length, kind, flags, seq, _ = HEADER.unpack_from(data, offset)
        if length < HEADER.size or length > len(data) - offset:
            raise ValueError('invalid rtnetlink message length')
        yield kind, flags, seq, data[offset + HEADER.size:offset + length]
        offset += (length + 3) & ~3


def attrs(data):
    result = {}
    offset = 0
    while offset + 4 <= len(data):
        length, kind = struct.unpack_from('=HH', data, offset)
        if length < 4 or length > len(data) - offset:
            raise ValueError('invalid rtnetlink attribute length')
        result[kind & 0x3fff] = data[offset + 4:offset + length]
        offset += (length + 3) & ~3
    return result


def dump(sock, kind, payload):
    sock.sendto(HEADER.pack(16 + len(payload), kind, 0x301, kind, 0) + payload, (0, 0))
    for _ in range(1024):
        data, _, flags, sender = sock.recvmsg(262144)
        if sender[0] or flags & socket.MSG_TRUNC:
            raise RuntimeError('unexpected or truncated rtnetlink datagram')
        for response, flags, seq, body in messages(data):
            if seq != kind:
                continue
            if flags & 16:
                raise RuntimeError('rtnetlink dump interrupted')
            if response == 3:
                if body and struct.unpack_from('=i', body)[0]:
                    raise RuntimeError('rtnetlink dump failed')
                return
            if response == 2:
                error = -struct.unpack_from('=i', body)[0]
                if error:
                    raise OSError(error, os.strerror(error))
            else:
                yield response, body
    raise RuntimeError('rtnetlink dump exceeded limit')


def integer(data):
    return int.from_bytes(data, byteorder=os.sys.byteorder)


def snapshot(*, sysfs_speed=True):
    links = {}
    routes = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as sock:
        sock.bind((0, 0))
        sock.settimeout(2)
        for _, body in dump(sock, 18, bytes(16)):
            _, _, hardware, index, flags, _ = struct.unpack_from('=BBHiII', body)
            a = attrs(body[16:])
            name = a[3].rstrip(b'\0').decode(errors='replace')
            try:
                speed = int((Path('/sys/class/net') / name / 'speed').read_text()) if sysfs_speed else None
            except (OSError, ValueError):
                speed = None
            kind = attrs(a.get(18,b'')).get(1,b'').rstrip(b'\0').decode(errors='replace')
            if sysfs_speed and ((Path('/sys/class/net')/name/'wireless').exists() or
                                (Path('/sys/class/net')/name/'phy80211').exists()):
                kind = 'wifi'
            elif not kind:
                kind = 'ethernet' if hardware==1 else 'loopback' if hardware==772 else 'other'
            raw = a.get(23,b'')
            counters = dict(zip(('rx_packets','tx_packets','rx_bytes','tx_bytes','rx_errors','tx_errors',
                                 'rx_dropped','tx_dropped'),struct.unpack_from('=8Q',raw))) if len(raw)>=64 else {}
            if len(links)>=256:
                raise RuntimeError('interface snapshot exceeds 256 interfaces')
            links[index] = dict(index=index, name=name, kind=kind, hardware_type=hardware,
                                mac=a.get(1,b'').hex(), counters=counters, up=bool(flags & 1),
                                carrier=bool(flags & 0x10000), mtu=integer(a.get(4, b'')),
                                speed_mbps=speed if speed and speed > 0 else None, addresses=[])
        for _, body in dump(sock, 22, bytes(8)):
            family, prefix, _, scope, index = struct.unpack_from('=BBBBI', body)
            a = attrs(body[8:])
            address = a.get(2, a.get(1))
            if index in links and family in (2, 10) and address:
                links[index]['addresses'].append(dict(address=str(ipaddress.ip_address(address)),
                                                      prefix=prefix, scope=scope))
        for _, body in dump(sock, 26, bytes(12)):
            family, prefix, _, _, table, protocol, scope, route_type, _ = struct.unpack_from('=BBBBBBBBI', body)
            if family not in (2, 10):
                continue
            a = attrs(body[12:])
            destination = a.get(1, bytes(4 if family == 2 else 16))
            if len(routes)>=2048:
                raise RuntimeError('route snapshot exceeds 2048 routes')
            routes.append(dict(destination=f'{ipaddress.ip_address(destination)}/{prefix}',
                               gateway=str(ipaddress.ip_address(a[5])) if 5 in a else None,
                               interface_index=integer(a.get(4, b'')), table=integer(a[15]) if 15 in a else table,
                               protocol=protocol, scope=scope, type=route_type,
                               metric=integer(a[6]) if 6 in a else None))
    return dict(interfaces=list(links.values()), routes=routes)


def subscribe():
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
    try:
        sock.bind((0, 1 | 0x10 | 0x40 | 0x100 | 0x400))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


if __name__ == '__main__':
    import json
    print(json.dumps(snapshot()))
