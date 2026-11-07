"""Traffic control for disposable FLOWCORE-owned namespace interfaces."""
from paths import BINDIR, RESULTS
import json
from benchmark import Lab, TC, run
from network_state import snapshot, subscribe
from service_config import CLASSES


class ServiceLab(Lab):
    def start_flow_engine(self,capacity=4096,observe=False):
        from flow_engine import FlowEngine
        if getattr(self,'flow_engine',None):
            self.flow_engine.close()
        self.flow_engine=FlowEngine()
        return self.flow_engine.start(self,capacity,observe)

    def flow_intelligence(self,limit=4096):
        from flow_snapshot import snapshot as flow_snapshot
        from service_config import number
        number(limit,1,4096,True)
        with self.enter(0):
            sockets=flow_snapshot(limit)
        engine=getattr(self,'flow_engine',None)
        if not engine or not engine.ready:
            return sockets
        observations=engine.dump()
        by_cookie={int(row['cookie'],16):row for row in sockets['flows'] if row.get('cookie') and int(row['cookie'],16)!=0}
        for row in observations:
            match=by_cookie.get(int(row['cookie'])) if int(row['cookie']) else None
            row['socket_snapshot']=match if match and match['protocol']==row['protocol'] and match['netns']==row['netns'] else None
            row['socket_association']='exact_cookie' if row['socket_snapshot'] else 'unavailable'
            row['tuple_relation']='not_comparable'
            if row['socket_snapshot'] and match['remote_port'] and match['local_address'] not in ('0.0.0.0','::'):
                row['tuple_relation']='same' if (
                    row['source'],row['sport'],row['destination'],row['dport'])==(
                    match['local_address'],match['local_port'],match['remote_address'],match['remote_port']) else 'different'
        return dict(source='tcx+inet_diag',flows=observations,stats=engine.stats,
                    socket_snapshot_truncated=sockets['truncated'],snapshot=engine.last_snapshot)

    def close(self):
        if getattr(self,'flow_engine',None):
            self.flow_engine.close()
        super().close()

    def command(self, index, argv):
        return run(self.ns(index, argv))

    def network(self, index):
        with self.enter(index):
            return snapshot(sysfs_speed=False)

    def watcher(self, index):
        with self.enter(index):
            return subscribe()

    def validate(self):
        for index, (name, expected) in enumerate((('fc0', '10.203.0.1'), ('fc1', '10.203.0.2'))):
            links = {link['name']: link for link in self.network(index)['interfaces']}
            link = links.get(name, {})
            if set(links) != {'lo', name} or not link.get('up') or link.get('mtu') != 1500:
                raise RuntimeError('lab link validation failed')
            if expected not in {address['address'] for address in link['addresses']}:
                raise RuntimeError('lab address validation failed')

    def install_policy(self, policy, profile):
        capacity = profile['bandwidth_kbit']
        rate = f'{capacity}kbit'
        self.command(1, ['ip', '-6', 'addr', 'add', 'fd42:203::2/64', 'dev', 'fc1', 'nodad'])
        self.command(0, ['ip', '-6', 'addr', 'add', 'fd42:203::1/64', 'dev', 'fc0', 'nodad'])
        if policy['scheduler'] == 'cake':
            self.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'cake',
                    'bandwidth', rate, 'besteffort', 'flows')
        else:
            self.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'htb', 'default', '12')
            self.tc('class', 'add', 'dev', 'fc0', 'parent', '1:', 'classid', '1:1',
                    'htb', 'rate', rate, 'ceil', rate)
            for name, cls in policy['classes'].items():
                minor = CLASSES[name]
                self.tc('class', 'add', 'dev', 'fc0', 'parent', '1:1', 'classid', f'1:{minor}',
                        'htb', 'rate', f"{capacity * cls['min_pct'] // 100}kbit",
                        'ceil', f"{capacity * cls['max_pct'] // 100}kbit",
                        'prio', cls['priority'], 'quantum', '1514')
                self.tc('qdisc', 'add', 'dev', 'fc0', 'parent', f'1:{minor}', 'fq_codel')
                for protocol in ('ip', 'ipv6'):
                    self.tc('filter', 'add', 'dev', 'fc0', 'parent', '1:', 'protocol', protocol,
                            'pref', 2 * minor + (protocol == 'ipv6'), 'flower', 'ip_proto', 'tcp', 'dst_port', cls['port'],
                            'classid', f'1:{minor}')
        # Reverse path impairment is separate from the sender's egress shaper.
        args = [TC, 'qdisc', 'add', 'dev', 'fc1', 'root', 'netem', 'limit', '1000',
                'delay', f"{profile['delay_ms']}ms", f"{profile['jitter_ms']}ms",
                'loss', f"{profile['loss_pct']}%"]
        if profile['reorder_pct']:
            args += ['reorder', f"{profile['reorder_pct']}%"]
        self.command(1, args)
        self.validate()

    def counters(self):
        stats = {}
        for kind in ('qdisc', 'class', 'filter'):
            output = self.tc('-s', '-j', kind, 'show', 'dev', 'fc0')
            try:
                stats[kind] = json.loads(output)
            except ValueError as error:
                raise RuntimeError(f'tc {kind} readback is not JSON: {output[:160]!r}') from error
        stats['receiver_qdisc'] = json.loads(self.command(1, [TC, '-s', '-j', 'qdisc', 'show', 'dev', 'fc1']))
        by_handle = {row['handle']: row for row in stats['class']}
        stats['traffic_classes'] = {name: by_handle[f'1:{minor}'] for name, minor in CLASSES.items()
                                    if f'1:{minor}' in by_handle}
        stats['link'] = json.loads(self.command(0, [BINDIR/'flowcore', '--link', 'fc0']))
        return stats
