"""Bounded trace lifecycle records. Snapshot absence never implies TCP close."""
from collections import OrderedDict

STATES = {1:'established',2:'syn_sent',3:'syn_recv',4:'fin_wait_1',5:'fin_wait_2',
          6:'time_wait',7:'closed',8:'close_wait',9:'last_ack',10:'listen',11:'closing',12:'new_syn_recv'}


class Lifecycles:
    def __init__(self, maximum=2048):
        self.maximum = maximum
        self.rows = OrderedDict()
        self.cookies = {}
        self.evicted = 0
        self.out_of_order = 0
        self.unknown_identity = 0
        self.identity_conflicts = 0

    def ingest(self, event):
        if not event['flow_id']:
            self.unknown_identity += 1
            return
        key = event['netns'], event['flow_id']
        old = self.rows.get(key)
        if old and event['time_ns'] < old['last_event_ns']:
            self.out_of_order += 1
            return
        if old is None:
            if len(self.rows) >= self.maximum:
                removed_key, removed = self.rows.popitem(last=False)
                cookie_key = removed['netns'], removed['cookie']
                if self.cookies.get(cookie_key) == removed_key:
                    del self.cookies[cookie_key]
                self.evicted += 1
            old = dict(netns=event['netns'],flow_id=event['flow_id'],cookie=event['cookie'],
                       local_port=event['sport'],remote_port=event['dport'],
                       family=event['family'],first_event_ns=event['time_ns'],
                       local_address=event.get('local_address'),remote_address=event.get('remote_address'),
                       state=None, observed_lifetime_ns=None, retransmit_events=0,received_resets=0,
                       pid=None,cgroup_id=None,
                       identity_source='collector flow_id scoped to generation and network namespace',
                       lifetime_source='observed SYN or ESTABLISHED to TIME_WAIT/CLOSE')
            self.rows[key] = old
            if event['cookie'] != '0000000000000000':
                self.cookies[(event['netns'],event['cookie'])] = key
        if (old['cookie'] != '0000000000000000' and event['cookie'] != '0000000000000000'
                and old['cookie'] != event['cookie']):
            self.identity_conflicts += 1
            return
        if old['cookie'] == '0000000000000000' and event['cookie'] != '0000000000000000':
            old['cookie'] = event['cookie']
            self.cookies[(event['netns'],event['cookie'])] = key
        if event['sport']:
            old['local_port'] = event['sport']
        if event['dport']:
            old['remote_port'] = event['dport']
        for field in ('local_address','remote_address'):
            if event.get(field) not in (None,'0.0.0.0','::'):
                old[field] = event[field]
        old['last_event_ns'] = event['time_ns']
        if event['kind'] == 1:
            old['state'] = STATES.get(event['new'],str(event['new']))
            old['origin_state'] = STATES.get(event['origin_state'],str(event['origin_state']))
            old['handshake_close_candidate'] = event['old'] in (2,3) and event['new']==7
            old['observed_lifetime_ns'] = event['lifetime_ns'] or None
        elif event['kind'] == 2:
            old['retransmit_events'] += 1
        elif event['kind'] == 3:
            old['received_resets'] += 1
        self.rows.move_to_end(key)

    def correlate(self, socket_row):
        cookie = socket_row.get('cookie')
        if not cookie or cookie == '0000000000000000':
            return None
        # No tuple-only join. Cookies are joined only inside the same netns.
        key = self.cookies.get((socket_row.get('netns'),cookie))
        row = self.rows.get(key)
        if row is None:
            return None
        row['cgroup_id'] = socket_row.get('cgroup_id')
        return dict(flow_id=row['flow_id'],last_event_ns=row['last_event_ns'],
                    state=row['state'],source='netns and socket cookie')

    def status(self):
        return dict(retained=len(self.rows), maximum=self.maximum, evicted=self.evicted,
                    out_of_order=self.out_of_order,unknown_identity=self.unknown_identity,
                    identity_conflicts=self.identity_conflicts)
