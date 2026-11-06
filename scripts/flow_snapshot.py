"""Read-only TCP/UDP flow fallback in the current network namespace."""
import os
import time
from tcp_diag import connections


def snapshot(limit=128):
    if type(limit) is not int or not 1<=limit<=4096:
        raise ValueError('flow limit must be 1..4096')
    namespace=os.stat('/proc/self/ns/net').st_ino
    rows=[]
    truncated=False
    for protocol in (6,17):
        if len(rows)==limit:
            truncated=True
            break
        current,full=connections(limit-len(rows),protocol)
        for row in current:
            row['netns']=namespace
            row['observed_ns']=time.monotonic_ns()
            row['interface_association']='bound_socket_only'
        rows.extend(current)
        truncated|=full
    return dict(source='inet_diag',degraded=True,flows=rows,truncated=truncated,
        limitations=['snapshots miss short flows','no UDP per-flow byte/packet totals',
                     'no classification installed','bound interface is not observed egress'])
