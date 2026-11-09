"""Bounded path evidence and a conservative, deterministic MPTCP policy."""
from collections import deque
from dataclasses import dataclass
import re
from statistics import median


@dataclass(frozen=True)
class PathIntent:
    preference: str = 'latency'
    deadline_ms: float = 100
    payload_bytes: int = 32768
    multipath: bool = True

    def __post_init__(self):
        if (self.preference not in ('latency', 'reliability', 'throughput')
                or type(self.deadline_ms) not in (int, float) or not 1 <= self.deadline_ms <= 60000
                or type(self.payload_bytes) is not int
                or not 1 <= self.payload_bytes <= 1048576 or type(self.multipath) is not bool):
            raise ValueError('invalid path intent')


def service_label(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',value):
        raise ValueError('invalid service identity')
    return value


def load_intent(path, trusted_owners=()):
    from durable_state import read_json
    from service_config import keys
    row=read_json(path,4096,trusted_owners=trusted_owners)
    keys(row,('version','service','preference','deadline_ms','multipath'))
    if type(row['version']) is not int or row['version']!=1:
        raise ValueError('unsupported path intent version')
    return service_label(row['service']),PathIntent(row['preference'],row['deadline_ms'],multipath=row['multipath'])


def advisory(view, *, fq_socket=False, authorized_datagram=False):
    """Local advice only. Capabilities alone never confer actuation authority."""
    if view.get('state') != 'CONGESTED' or view.get('confidence') != 'observed_local':
        return dict(action='NO_INTERVENTION', reason='no confirmed actionable local pressure')
    classification = view.get('classification')
    if classification == 'LOCAL_INGRESS_PRESSURE':
        if view.get('directions', {}).get('ingress', {}).get('controllable'):
            return dict(action='STATIC_QOS', reason='retain verified IFB policy and review ingress admission')
    if classification == 'LOCAL_EGRESS_CONGESTION':
        path = view.get('directions', {}).get('egress', {})
        if path.get('controllable'):
            if authorized_datagram:
                return dict(action='PER_MESSAGE_PRIORITIZATION', reason='cooperating datagrams on owned egress')
            if fq_socket:
                return dict(action='SOCKET_PACING', reason='eligible competing socket and verified pacing qdisc')
            if path.get('qdisc') == 'htb':
                return dict(action='HTB_ADJUSTMENT', reason='review existing controller allocation constraints')
            return dict(action='STATIC_QOS', reason='retain verified local shaper')
    return dict(action='NO_INTERVENTION', reason='no owned actuator for the observed location')


class PathEvidence:
    def __init__(self):
        self.previous = None
        self.rates = deque(maxlen=5)
        self.probes = deque(maxlen=5)
        self.competing = deque(maxlen=4)
        self.probe_floor = None
        self.downward = 0
        self.epoch = 0
        self.last_change = None

    def observe(self, frame):
        old = self.previous
        self.previous = frame
        elapsed = (frame['sample_ns']-old['sample_ns'])/1e9 if old else None
        continuous = (old is not None and 0 < elapsed <= 2
                      and frame['identity']==old['identity']
                      and frame['departed_bytes']>=old['departed_bytes'])
        if not continuous:
            self.rates.clear()
            self.probes.clear()
            self.competing.clear()
            self.probe_floor = None
            self.downward = 0
            self.epoch += 1
            self.last_change = None
        capacity = frame['capacity_kbit']
        prior_cap = old['capacity_kbit'] if continuous else None
        departure = (frame['departed_bytes']-old['departed_bytes'])/(125*elapsed) if continuous else None
        backlog = frame['backlog_bytes']
        probe = frame['probe_ms']
        available = frame['available']
        growth = continuous and backlog-old['backlog_bytes'] >= 1500
        util = departure/capacity if departure is not None and capacity else None
        busy = available and backlog >= 1500 and util is not None and .7 <= util <= 1.25
        if busy and capacity == prior_cap:
            self.rates.append(departure)
        else:
            self.rates.clear()
        if not available:
            self.probes.clear()
        self.probes.append(probe)
        if probe is not None and backlog < 1500:
            self.probe_floor = probe if self.probe_floor is None else min(self.probe_floor,probe)
        drops = max(0,frame['queue_drops']-old['queue_drops']) if continuous else None
        pressure = bool(available and (growth or backlog >= 1500 and (busy or drops)))
        current_bulk = frame.get('competing_bytes')
        previous_bulk = old.get('competing_bytes') if continuous else None
        competing_rate = ((current_bulk-previous_bulk)/(125*elapsed)
                          if current_bulk is not None and previous_bulk is not None
                          and current_bulk>=previous_bulk else None)
        burst = (competing_rate is not None and len(self.competing)>=2 and
                 competing_rate > 1.25*median(self.competing)+500 and pressure)
        if competing_rate is not None:
            self.competing.append(competing_rate)
        event = 'BASELINE' if not continuous else 'UNCHANGED'
        if not available:
            event = 'INTERFACE_FAILURE'
        elif old and not old['available']:
            event = 'LINK_RECOVERY'
        elif continuous and prior_cap and capacity and prior_cap != capacity:
            self.downward = self.downward+1 if capacity<prior_cap else 0
            if capacity < .85*prior_cap:
                self.downward = 0
                event = 'SUDDEN_REDUCTION'
            elif self.downward >= 2:
                event = 'GRADUAL_DEGRADATION'
            elif capacity > prior_cap:
                event = 'CAPACITY_RECOVERY'
            else:
                event = 'CAPACITY_CHANGE'
        elif burst:
            event = 'COMPETING_DEMAND'
        elif pressure and growth:
            event = 'QUEUE_GROWTH'
        if event not in ('UNCHANGED', 'BASELINE','QUEUE_GROWTH','INTERFACE_FAILURE','COMPETING_DEMAND'):
            self.last_change=(event,frame['sample_ns'])
        probes = [value for value in self.probes if value is not None]
        change_recent = self.last_change and frame['sample_ns']-self.last_change[1] <= 4_000_000_000
        # Recent losses/timeouts are observations from probes, not packet loss estimates.
        return dict(frame, epoch=self.epoch, event=event, departure_kbit=departure,
                    saturated_rate_kbit=median(self.rates) if len(self.rates)>=3 else None,
                    capacity_source='managed_qdisc_readback' if capacity else 'unknown',
                    confidence='verified_configuration' if capacity else 'insufficient',
                    queue_growth=bool(growth), pressure=pressure, utilization=util,
                    queue_drop_delta=drops, competing_departure_kbit=competing_rate,
                    probe_p50_ms=median(probes) if probes else None,
                    probe_samples=len(probes), probe_failures=len(self.probes)-len(probes),
                    probe_floor_ms=self.probe_floor,
                    recent_change=self.last_change[0] if change_recent else None,
                    location=frame['location'] if pressure else 'UNKNOWN')


class PathController:
    """One bounded policy session. The caller must commit verified changes."""
    def __init__(self, intent, primary=1):
        self.intent = intent
        self.primary = primary
        self.initial = primary
        self.pending = None
        self.streak = 0
        self.last_change = 0
        self.changes = 0
        self.frozen = False
        self.last_frame = None

    def evaluate(self, now_ns, paths, application):
        current = paths.get(self.primary)
        result = dict(action='NO_INTERVENTION', primary=self.primary, application=application,
                      preference=self.intent.preference, reason='healthy or insufficient evidence')
        if (self.frozen or not self.intent.multipath or current is None or self.changes>=4
                or any(not 0<=now_ns-path['sample_ns']<=1_500_000_000 for path in paths.values())):
            self.pending,self.streak=None,0
            return dict(result, reason='frozen, stale, unavailable or exhausted policy authority')
        frame = tuple(sorted((ident,path['sample_ns']) for ident,path in paths.items()))
        if self.last_frame and frame<=self.last_frame:
            return dict(result, reason='duplicate or out-of-order path evidence')
        self.last_frame=frame
        usable = [row for row in paths.values() if row['available'] and row['route_valid']
                  and row.get('subflow_observed') and row['probe_ms'] is not None
                  and row['probe_samples']>=2 and row['capacity_kbit']]
        if not usable:
            self.pending,self.streak=None,0
            return dict(result, reason='no freshly measured routable alternative')

        def delay_proxy(row):
            return row['probe_p50_ms'] + 8*self.intent.payload_bytes/row['capacity_kbit']

        def ranking(row):
            proxy = delay_proxy(row)
            if self.intent.preference=='throughput':
                return (-row['capacity_kbit'],row['probe_failures'],proxy,row['path_id'])
            if self.intent.preference=='reliability':
                return (row['probe_failures'],proxy,row['path_id'])
            return (proxy,row['probe_failures'],row['path_id'])

        chosen = min(usable,key=ranking)
        delay_risk = (current['capacity_kbit'] and current['probe_p50_ms'] is not None
                      and delay_proxy(current)>=self.intent.deadline_ms)
        app_risk = application.get('samples',0)>=3 and application.get('deadline_misses',0)>0
        capacity_risk = current['recent_change'] in ('SUDDEN_REDUCTION','GRADUAL_DEGRADATION')
        degraded = (not current['available'] or not current['route_valid'] or
                    (current['pressure'] or capacity_risk) and
                    (self.intent.preference!='latency' or delay_risk or app_risk))
        returning = (self.primary!=self.initial and chosen['path_id']==self.initial and
                     chosen['recent_change'] in ('CAPACITY_RECOVERY','LINK_RECOVERY') and not chosen['pressure'])
        current_usable = current in usable
        if (chosen['path_id']==self.primary or not (degraded or returning)
                or (current_usable and ranking(chosen)>=ranking(current))):
            self.pending,self.streak=None,0
            return result
        if current_usable and self.intent.preference=='latency' and ranking(chosen)[0] > .85*ranking(current)[0]:
            self.pending,self.streak=None,0
            return dict(result, reason='estimated latency advantage inside deadband')
        key=(chosen['path_id'],chosen['epoch'],current['epoch'])
        self.streak = min(2,self.streak+1) if self.pending==key else 1
        self.pending=key
        if (self.streak<2 and current['available']) or (current['available'] and now_ns-self.last_change<1_200_000_000):
            return dict(result, reason='awaiting corroboration or dwell interval')
        return dict(result, action='MPTCP_BACKUP_PREFERENCE', chosen_path_id=chosen['path_id'],
                    candidate_epoch=chosen['epoch'],
                    reason='owned path unavailable' if not current['available'] else
                           'verified path recovery' if returning else 'degradation with a measured alternate',
                    candidate_score=list(ranking(chosen)), uncertainty='echo probes and configured rate bound',
                    deadline_ms=self.intent.deadline_ms, observed_deadline_risk=bool(app_risk),
                    capacity_delay_proxy_risk=bool(delay_risk))

    def commit(self, path, now_ns):
        self.primary,self.last_change=path,now_ns
        self.changes += 1
        self.pending,self.streak=None,0
