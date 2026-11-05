"""Read-only multi-interface health and observed path quality."""
import socket
import statistics
import struct
import time
from collections import deque
from network_state import attrs, dump, snapshot
from platform_policy import merged
from tcp_diag import connections


def queues():
    result = {}
    with socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,socket.NETLINK_ROUTE) as sock:
        sock.bind((0,0))
        sock.settimeout(1)
        for _,body in dump(sock,38,bytes(20)):
            _,index,_,parent,_ = struct.unpack_from('=B3xIIII',body)
            if parent!=0xffffffff:
                continue
            values = attrs(body[20:])
            stats = attrs(values.get(7,b''))
            q = stats.get(3,b'')
            if len(q)>=20:
                qlen,backlog,drops,requeues,overlimits = struct.unpack_from('=5I',q)
                result[index] = dict(kind=values.get(1,b'').rstrip(b'\0').decode(errors='replace'), qlen=qlen,backlog_bytes=backlog,drops=drops,
                                     requeues=requeues,overlimits=overlimits)
                basic = stats.get(1,b'')
                if len(basic)>=12:
                    sent, packets = struct.unpack_from('=QI',basic)
                    result[index].update(bytes=sent,packets=packets)
    return result


class Inventory:
    def __init__(self, store, max_sockets=128):
        self.store = store
        self.max_sockets = max_sockets
        self.tcp_rows = []
        self.tcp_truncated = False
        self.tcp_error = None
        self.last = {}
        self.routes = set()
        self.view = {}
        self.events = deque(maxlen=128)
        self.error = None
        self.sampled = None

    def refresh(self):
        now = time.monotonic()
        try:
            state = snapshot()
            try:
                queue = queues()
                queue_error = None
            except (OSError,ValueError,RuntimeError,struct.error) as error:
                queue = {}
                queue_error = str(error)
            rtt = {}
            try:
                tcp, self.tcp_truncated = connections(self.max_sockets)
                self.tcp_rows = tcp
                self.tcp_error = None
                for item in tcp:
                    index = item.get('bound_interface_index')
                    value = item['tcp_info'].get('rtt_us')
                    if index and value and item['state']=='established':
                        rtt.setdefault(index,[]).append(value)
            except (OSError,ValueError,RuntimeError,struct.error) as error:
                self.tcp_rows = []
                self.tcp_error = str(error)
            current = {}
            for row in state['interfaces']:
                key = (row['index'],row['name'],row['kind'],row['mac'])
                old = self.last.get(key)
                current[key] = row
                available = row['up'] and row['carrier']
                row['available'] = available
                if old and old['available']!=available:
                    self.events.append(dict(time_ns=time.monotonic_ns(),interface=row['name'],
                                            event='recovered' if available else 'link_unavailable'))
                elif not old and self.sampled is not None:
                    self.events.append(dict(time_ns=time.monotonic_ns(),interface=row['name'],event='discovered'))
                row['profile'] = merged(self.store.state['policy'],row['kind'],row['name'])
                seconds = now-self.sampled if self.sampled is not None else 0
                def rate(counter):
                    before = old['counters'].get(counter) if old else None
                    after = row['counters'].get(counter)
                    return (after-before)/seconds if seconds>0 and before is not None and after is not None and after>=before else None
                tx,rx = rate('tx_bytes'),rate('rx_bytes')
                medium = row['speed_mbps'] if row['kind']=='ethernet' else None
                observations = rtt.get(row['index'],[])
                row['path_quality'] = dict(tx_mbps=tx*8/1e6 if tx is not None else None,
                    rx_mbps=rx*8/1e6 if rx is not None else None,
                    tx_utilization=tx*8/(medium*1e6) if tx is not None and medium else None,
                    local_tx_drops_per_second=rate('tx_dropped'),local_rx_errors_per_second=rate('rx_errors'),
                    rtt_ms=statistics.median(observations)/1000 if observations else None,
                    rtt_bound_socket_samples=len(observations),end_to_end_loss_ratio=None)
                row['queue'] = queue.get(row['index'])
                row['queue_pressure'] = row['queue']['backlog_bytes']>0 if row['queue'] is not None else None
            for key in self.last.keys()-current.keys():
                self.events.append(dict(time_ns=time.monotonic_ns(),interface=key[1],event='removed'))
            routes = {tuple(sorted(row.items())) for row in state['routes']}
            if self.sampled is not None and routes!=self.routes:
                self.events.append(dict(time_ns=time.monotonic_ns(),event='routes_changed',
                                        added=len(routes-self.routes),removed=len(self.routes-routes)))
            self.last,self.routes,self.sampled = current,routes,now
            self.view = dict(state,queue_error=queue_error)
            self.error = None
        except (OSError,ValueError,RuntimeError,struct.error) as error:
            self.error = str(error)

    def status(self):
        return dict(**self.view,events=list(self.events),error=self.error,
                    sample_age_seconds=time.monotonic()-self.sampled if self.sampled is not None else None,
                    scope='current daemon network namespace, read-only')
