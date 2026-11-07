"""Optional receive-side shaper for a newly admitted, private veth pair."""
import json
import os
from pathlib import Path

from benchmark import TC
from bottleneck import shaper_rate
from service_config import number


class Ingress:
    device = 'fcifb0'

    def __init__(self, capacity_kbit):
        number(capacity_kbit, 100, 1_000_000, integer=True)
        self.capacity = capacity_kbit

    def install(self, lab):
        host = os.stat('/proc/self/ns/net')
        identities = {(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in lab.namespace_fds}
        if len(identities) != 2 or (host.st_dev, host.st_ino) in identities:
            raise PermissionError('IFB requires a private two-endpoint laboratory')
        for index, device in enumerate(('fc0', 'fc1')):
            links = {row['name']: row for row in lab.network(index)['interfaces']}
            if set(links) != {'lo', device} or links[device]['kind'] != 'veth' or not links[device]['up']:
                raise ValueError('IFB requires the original owned veth pair')
        if not Path('/sys/module/ifb').is_dir():
            raise RuntimeError('IFB driver must already be available without auto-creating host devices')
        existing = self.read(lab, 'qdisc', 'show', 'dev', 'fc0')
        if any(row['kind'] in ('clsact', 'ingress') for row in existing):
            raise ValueError('refusing to replace an existing ingress hook')

        # The candidate namespace is discarded on any failed stage. Install the
        # redirect last so an incomplete IFB tree cannot see the ingress path.
        lab.command(0, ['ip', 'link', 'add', self.device, 'type', 'ifb'])
        lab.command(0, ['ip', 'link', 'set', self.device, 'up'])
        rate = f'{self.capacity}kbit'
        lab.tc('qdisc', 'add', 'dev', self.device, 'root', 'handle', '1:', 'htb', 'default', '20')
        lab.tc('class', 'add', 'dev', self.device, 'parent', '1:', 'classid', '1:1',
               'htb', 'rate', rate, 'ceil', rate)
        critical = self.capacity * 30 // 100
        for minor, share, priority in ((10, critical, 0), (20, self.capacity-critical, 1)):
            lab.tc('class', 'add', 'dev', self.device, 'parent', '1:1', 'classid', f'1:{minor}',
                   'htb', 'rate', f'{share}kbit', 'ceil', rate, 'prio', priority, 'quantum', '1514')
            lab.tc('qdisc', 'add', 'dev', self.device, 'parent', f'1:{minor}', 'fq_codel')
        for preference, protocol in enumerate(('ip', 'ipv6'), 10):
            lab.tc('filter', 'add', 'dev', self.device, 'parent', '1:', 'protocol', protocol,
                   'pref', preference, 'flower', 'ip_proto', 'tcp', 'src_port', 9000, 'classid', '1:10')
        self.readback(lab, redirect=False)
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'handle', 'ffff:', 'ingress')
        lab.tc('filter', 'add', 'dev', 'fc0', 'ingress', 'protocol', 'all', 'pref', 10,
               'matchall', 'action', 'mirred', 'egress', 'redirect', 'dev', self.device)
        self.readback(lab)

    @staticmethod
    def read(lab, *args):
        rows = json.loads(lab.command(0, [TC, '-s', '-j', *args]))
        if not isinstance(rows, list):
            raise ValueError('unexpected TC JSON readback')
        return rows

    def readback(self, lab, redirect=True):
        queues = self.read(lab, 'qdisc', 'show', 'dev', self.device)
        root = next((row for row in queues if row.get('root')), {})
        classes = self.read(lab, 'class', 'show', 'dev', self.device)
        by_handle = {row['handle']: row for row in classes if row.get('class') == 'htb'}
        expected = {'1:1': self.capacity, '1:10': self.capacity*30//100}
        expected['1:20'] = self.capacity-expected['1:10']
        leaves = {row.get('parent'): row['kind'] for row in queues if row.get('parent')}
        if (shaper_rate(root, classes) != self.capacity or root.get('handle') != '1:' or
                root.get('options', {}).get('default') != '0x20' or
                set(by_handle) != set(expected) or leaves != {'1:10': 'fq_codel', '1:20': 'fq_codel'} or
                any(row.get('rate') != expected[key]*125 or row.get('ceil') != self.capacity*125
                    for key, row in by_handle.items())):
            raise RuntimeError('IFB policy readback mismatch')
        filters = self.read(lab, 'filter', 'show', 'dev', self.device, 'parent', '1:')
        rules = [(row['protocol'], row.get('kind'), row['options'].get('classid'),
                  row['options'].get('keys', {}).get('ip_proto'),
                  row['options'].get('keys', {}).get('src_port'))
                 for row in filters if 'options' in row]
        if len(rules) != 2 or set(rules) != {('ip', 'flower', '1:10', 'tcp', 9000),
                                            ('ipv6', 'flower', '1:10', 'tcp', 9000)}:
            raise RuntimeError('IFB classifier readback mismatch')
        actions = []
        if redirect:
            rows = self.read(lab, 'filter', 'show', 'dev', 'fc0', 'ingress')
            rules = [row for row in rows if 'options' in row]
            if len(rules) != 1 or rules[0]['kind'] != 'matchall' or rules[0]['protocol'] != 'all':
                raise RuntimeError('unexpected ingress filter')
            actions = rules[0]['options'].get('actions', [])
            if len(actions) != 1 or any(actions[0].get(key) != value for key, value in
                                       dict(kind='mirred', mirred_action='redirect', direction='egress',
                                            to_dev=self.device).items()):
                raise RuntimeError('IFB redirect readback mismatch')
        return dict(verified=redirect, device=self.device, capacity_kbit=self.capacity,
                    root=root, qdiscs=queues, classes=dict(critical=by_handle['1:10'], background=by_handle['1:20']),
                    redirect=actions)
