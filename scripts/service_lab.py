"""Traffic control for disposable FLOWCORE-owned namespace interfaces."""
from paths import BINDIR, RESULTS
import contextlib
import json
import os
import subprocess
import sys
from benchmark import ENV, Lab, ROOT, TC, run
from network_state import snapshot, subscribe
from service_config import CLASSES


@contextlib.contextmanager
def network_namespace(pid):
    with open('/proc/self/ns/net', 'rb') as previous, open(f'/proc/{pid}/ns/net', 'rb') as target:
        os.setns(target.fileno(), os.CLONE_NEWNET)
        try:
            yield
        finally:
            os.setns(previous.fileno(), os.CLONE_NEWNET)


class ServiceLab(Lab):
    def holder_command(self):
        return [sys.executable, ROOT / 'scripts/namespace_holder.py', str(os.getpid())]

    def pin_research_namespaces(self):
        self.research_fds = []
        host = os.stat('/proc/self/ns/net')
        try:
            for holder in self.holders:
                if holder.poll() is not None:
                    raise RuntimeError('research namespace holder exited')
                fd = os.open(f'/proc/{holder.pid}/ns/net', os.O_RDONLY | os.O_CLOEXEC)
                self.research_fds.append(fd)
                ns = os.fstat(fd)
                if (holder.poll() is not None or (ns.st_dev,ns.st_ino)==(host.st_dev,host.st_ino)
                        or any(os.fstat(previous).st_ino==ns.st_ino for previous in self.research_fds[:-1])):
                    raise RuntimeError('unsafe research namespace identity')
        except BaseException:
            for fd in self.research_fds:
                os.close(fd)
            self.research_fds.clear()
            raise

    def close(self):
        for fd in getattr(self, 'research_fds', []):
            os.close(fd)
        self.research_fds = []
        super().close()

    def command(self, index, argv):
        if not getattr(self, 'research_fds', None):
            return run(self.ns(index, argv))
        if self.holders[index].poll() is not None:
            raise RuntimeError('research namespace holder exited')
        fd = self.research_fds[index]
        return subprocess.run(['nsenter', f'--net=/proc/self/fd/{fd}', '--', *map(str,argv)],
                              pass_fds=(fd,), check=True, capture_output=True, text=True,
                              env=ENV, timeout=10).stdout

    def network(self, index):
        with network_namespace(self.holders[index].pid):
            return snapshot(sysfs_speed=False)

    def watcher(self, index):
        with network_namespace(self.holders[index].pid):
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
        stats = {kind: json.loads(self.tc('-s', '-j', kind, 'show', 'dev', 'fc0'))
                 for kind in ('qdisc', 'class', 'filter')}
        stats['receiver_qdisc'] = json.loads(self.command(1, [TC, '-s', '-j', 'qdisc', 'show', 'dev', 'fc1']))
        by_handle = {row['handle']: row for row in stats['class']}
        stats['traffic_classes'] = {name: by_handle[f'1:{minor}'] for name, minor in CLASSES.items()
                                    if f'1:{minor}' in by_handle}
        stats['link'] = json.loads(self.command(0, [BINDIR/'flowcore', '--link', 'fc0']))
        return stats
