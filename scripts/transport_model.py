"""Transport-neutral reports: application claims never become kernel attestations."""
import re
import time
from service_config import keys, number

KINDS=('tcp','mptcp','udp','quic','logical')
ERRORS=('none','handshake','timeout','reset','path','cancelled','other')


def declare(lease, value):
    keys(value,('session','kind','multiplexed','mobility_preference','cookie'))
    if not isinstance(value['session'],str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',value['session']):
        raise ValueError('invalid logical session identifier')
    if value['kind'] not in KINDS or type(value['multiplexed']) is not bool or type(value['mobility_preference']) is not bool:
        raise ValueError('unsupported transport declaration')
    cookie=value['cookie']
    if cookie is not None:
        number(cookie,1,2**64-1,True)
        if cookie not in lease['cookies']:
            raise ValueError('socket cookie is not associated with this lease')
        observed=lease.get('socket_capabilities',{}).get(cookie,{})
        expected=17 if value['kind'] in ('udp','quic') else None
        if expected and observed.get('protocol')!=expected:
            raise ValueError('UDP/QUIC declaration requires an associated UDP socket')
        if value['kind']=='tcp' and (observed.get('protocol') not in (6,262) or observed.get('is_mptcp') is True):
            raise ValueError('TCP declaration conflicts with socket protocol')
        if value['kind']=='mptcp' and observed.get('is_mptcp') is not True:
            raise ValueError('MPTCP declaration requires positively observed MPTCP')
    sessions=lease.setdefault('transport_sessions',{})
    if value['session'] not in sessions and len(sessions)>=16:
        raise ValueError('transport session quota exhausted')
    sessions[value['session']]=dict(**value,source='application_reported',
        kernel_packet_stream_priority=False,mobility_enforced=False,
        reliability_preference_source='service_intent_only',
        degraded=False,error='none',updated_ns=time.monotonic_ns())
    return dict(version=2,accepted=True,capabilities=sessions[value['session']])


def report(lease,session,degraded,error):
    if type(degraded) is not bool or error not in ERRORS:
        raise ValueError('invalid transport state')
    row=lease.get('transport_sessions',{}).get(session) if isinstance(session,str) else None
    if row is None:
        raise ValueError('unknown logical session')
    now=time.monotonic_ns()
    # State reporting is setup/diagnostic work, independently rate limited.
    if now-row['updated_ns']<10_000_000:
        raise ValueError('transport state rate limited')
    row.update(degraded=degraded,error=error,updated_ns=now)
    return dict(version=2,accepted=True)
