"""Bounded TCP socket snapshots using Linux INET_DIAG and TCP_INFO."""
import errno
import ipaddress
import socket
import struct
import sys
import time
from pathlib import Path

from network_state import HEADER, attrs, messages

# Linux UAPI tcp_info byte offsets. Fields beyond returned optlen are absent.
INFO = {
    'state_code': (0, 'B'), 'congestion_state_code': (1, 'B'),
    'current_retransmits': (2, 'B'), 'rto_us': (8, 'I'),
    'send_mss_bytes': (16, 'I'), 'unacked_segments': (24, 'I'),
    'lost_segments': (32, 'I'), 'current_retrans_segments': (36, 'I'), 'rtt_us': (68, 'I'), 'rttvar_us': (72, 'I'),
    'cwnd_segments': (80, 'I'), 'rcv_space_bytes': (96, 'I'),
    'total_retrans_segments': (100, 'I'), 'bytes_acked': (120, 'Q'),
    'pacing_rate_bytes_per_second': (104, 'Q'),
    'max_pacing_rate_bytes_per_second': (112, 'Q'),
    'bytes_received': (128, 'Q'), 'segments_out': (136, 'I'), 'segments_in': (140, 'I'),
    'notsent_bytes': (144, 'I'), 'min_rtt_us': (148, 'I'), 'data_segments_in': (152, 'I'), 'data_segments_out': (156, 'I'),
    'delivery_rate_bytes_per_second': (160, 'Q'), 'busy_time_us': (168, 'Q'),
    'receive_window_limited_us': (176, 'Q'), 'send_buffer_limited_us': (184, 'Q'),
    'delivered_segments': (192, 'I'), 'delivered_ce_segments': (196, 'I'), 'bytes_sent': (200, 'Q'),
    'bytes_retrans': (208, 'Q'), 'peer_receive_window_bytes': (228, 'I'),
    'local_receive_window_bytes': (232, 'I'), 'total_rto': (240, 'H'),
    'total_rto_recoveries': (242, 'H'), 'total_rto_time_ms': (244, 'I'),
    'received_ce_segments': (248, 'I'), 'accecn_delivered_ect1_bytes': (252, 'I'),
    'accecn_delivered_ect0_bytes': (256, 'I'), 'accecn_delivered_ce_bytes': (260, 'I'),
    'accecn_received_ect1_bytes': (264, 'I'), 'accecn_received_ect0_bytes': (268, 'I'),
    'accecn_received_ce_bytes': (272, 'I'),
}
STATES = {1: 'established', 2: 'syn_sent', 3: 'syn_recv', 4: 'fin_wait_1',
          5: 'fin_wait_2', 6: 'time_wait', 7: 'closed', 8: 'close_wait',
          9: 'last_ack', 10: 'listen', 11: 'closing', 12: 'new_syn_recv'}


def tcp_info(data):
    fields = {key: struct.unpack_from('=' + fmt, data, offset)[0]
              for key, (offset, fmt) in INFO.items() if len(data) >= offset + struct.calcsize(fmt)}
    if len(data) >= 6:
        fields.update(ecn_negotiated=bool(data[5] & 8), received_ect=bool(data[5] & 16))
    if len(data) >= 8:
        shift = 0 if sys.byteorder == 'little' else 7
        fields['delivery_rate_app_limited'] = bool(data[7] & (1 << shift))
    if len(data) >= 280:
        flags = struct.unpack_from('=I', data, 276)[0]
        shift = 0 if sys.byteorder == 'little' else 30
        fields['ecn_mode'] = ('disabled', 'classic', 'accurate', 'pending')[(flags >> shift) & 3]
        fields['accecn_option_state'] = (flags >> (2 if shift == 0 else 28)) & 3
        fields['accecn_fail_flags'] = (flags >> (4 if shift == 0 else 24)) & 15
    return fields


def decode(body):
    if len(body) < 72 or body[0] not in (socket.AF_INET,socket.AF_INET6):
        raise ValueError('short or unsupported inet_diag message')
    family, state, timer, retrans = body[:4]
    sport, dport = struct.unpack_from('!HH', body, 4)
    iface, low, high = struct.unpack_from('=III', body, 40)
    _, recvq, sendq, uid, inode = struct.unpack_from('=IIIII', body, 52)
    attributes = attrs(body[72:])
    size = 4 if family == socket.AF_INET else 16
    cookie = None if (low, high) == (0xffffffff, 0xffffffff) else f'{high:08x}{low:08x}'
    memory = attributes.get(7, b'')
    return dict(cookie=cookie, family=family, state=STATES.get(state, str(state)),
                local_address=str(ipaddress.ip_address(body[8:8+size])),
                remote_address=str(ipaddress.ip_address(body[24:24+size])),
                local_port=sport, remote_port=dport, bound_interface_index=iface or None,
                uid=uid, inode=inode, cgroup_id=struct.unpack_from('=Q', attributes[21])[0] if len(attributes.get(21,b''))>=8 else None,
                retransmit_timer=timer, retransmit_backoff_count=retrans,
                receive_queue=recvq, send_queue=sendq,
                memory={key: struct.unpack_from('=I', memory, i * 4)[0]
                        for i, key in enumerate(('receive_alloc_bytes', 'receive_buffer_bytes',
                            'send_alloc_bytes', 'send_buffer_bytes', 'forward_alloc_bytes',
                            'send_queued_bytes', 'option_bytes', 'backlog_bytes', 'socket_drops'))
                        if len(memory) >= (i + 1) * 4},
                tcp_info_bytes=len(attributes.get(2,b'')), tcp_info=tcp_info(attributes.get(2, b'')), source='inet_diag/tcp_info')


def connections(limit, protocol=socket.IPPROTO_TCP):
    if type(limit) is not int or not 1 <= limit <= 4096:
        raise ValueError('socket limit must be 1..4096')
    if type(protocol) is not int or protocol not in (socket.IPPROTO_TCP,socket.IPPROTO_UDP):
        raise ValueError('only TCP/UDP INET_DIAG supported')
    rows = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as sock:
        sock.bind((0, 0))
        deadline = time.monotonic() + .7
        for family in (socket.AF_INET, socket.AF_INET6):
            request = struct.pack('=BBBBI', family, protocol, 0x42 if protocol==socket.IPPROTO_TCP else 0x40, 0, 0x1ffe) + bytes(40) + b'\xff'*8
            sock.sendto(HEADER.pack(16 + len(request), 20, 0x301, family, 0) + request, (0, 0))
            done = False
            for _ in range(128):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('inet_diag snapshot deadline exceeded')
                sock.settimeout(min(.35, remaining))
                data, _, flags, sender = sock.recvmsg(262144)
                if sender[0] or flags & socket.MSG_TRUNC:
                    raise RuntimeError('invalid inet_diag response')
                for kind, flags, seq, body in messages(data):
                    if seq != family:
                        continue
                    if flags & 16:
                        raise RuntimeError('interrupted inet_diag dump')
                    if kind in (2, 3):
                        error = struct.unpack_from('=i', body)[0] if body else 0
                        if error:
                            raise OSError(-error, errno.errorcode.get(-error))
                        done = True
                        break
                    if kind == 20:
                        if len(rows) == limit:
                            return rows, True
                        row=decode(body)
                        row['protocol']=protocol
                        if protocol==socket.IPPROTO_UDP:
                            row.pop('tcp_info',None)
                            row.pop('tcp_info_bytes',None)
                            row.pop('retransmit_timer',None)
                            row.pop('retransmit_backoff_count',None)
                            row['source']='inet_diag/udp'
                            row['state']='connected' if row['state']=='established' else 'unconnected'
                        rows.append(row)
                if done:
                    break
            if not done:
                return rows, True
    return rows, False



def socket_snapshot(sock):
    """Only accepts a socket already held by this process."""
    import os
    data = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 512)
    pid = os.getpid()
    started = int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
    return dict(source='getsockopt(TCP_INFO)', time_ns=time.monotonic_ns(),
                local=sock.getsockname(), remote=sock.getpeername(),
                tcp_info_bytes=len(data), tcp_info=tcp_info(data),
                send_buffer_bytes=sock.getsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF),
                receive_buffer_bytes=sock.getsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF),
                process=dict(pid=pid,start_ticks=started),
                process_source='calling process owns descriptor; socket may be shared',
                cgroup_membership=Path('/proc/self/cgroup').read_text().strip())
