"""Verify the overload lab's IPv4 and IPv6 UDP rules with class packet counters."""
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))

from benchmark_v2 import verify
from research_faults import update_conditions
from service_config import load
from service_lab import ServiceLab


def probe(lab):
    before = packets(lab)
    with ExitStack() as cleanup:
        for family, ip in ((socket.AF_INET, '10.203.0.2'), (socket.AF_INET6, 'fd42:203::2')):
            with lab.enter(1):
                receiver = cleanup.enter_context(socket.socket(family, socket.SOCK_DGRAM))
                receiver.bind((ip, 9002))
                receiver.settimeout(3)
            with lab.enter(0):
                sender = cleanup.enter_context(socket.socket(family, socket.SOCK_DGRAM))
                for _ in range(8):
                    sender.sendto(b'overload-classification', (ip, 9002))
            for _ in range(8):
                if receiver.recv(128) != b'overload-classification':
                    raise AssertionError('unexpected UDP payload')
    return {name: value-before[name] for name, value in packets(lab).items()}


def packets(lab):
    return {name: row['stats']['packets'] for name, row in lab.counters()['traffic_classes'].items()}


def main():
    config = load(ROOT/'config/service.json')
    policy, profile = config['policies']['default'], config['lab']
    with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='overload-verify-') as tmp:
        lab = ServiceLab(Path(tmp))
        try:
            lab.start()
            lab.install_policy(policy, profile)
            legacy = probe(lab)
            state = SimpleNamespace(lab=lab, research_mode=None, profile=profile, controller=None, research_link_up=True)
            update_conditions(state, lab, profile, 'optimizer-overload', 7, True)
            corrected = probe(lab)
            if not (legacy['standard'] >= 16 and legacy['background'] == 0
                    and corrected['background'] >= 16 and corrected['standard'] < legacy['standard']):
                raise AssertionError(f'class counter mismatch: {legacy}, {corrected}')
            ports = {key: row['port'] for key, row in policy['classes'].items()}

            def readback():
                snap = dict(tc=lab.counters(), links=[lab.network(i)['interfaces'] for i in (0, 1)])
                return verify(snap, 'optimizer-overload', profile['bandwidth_kbit'], ports)

            readback()
            lab.assert_owned_sender()
            lab.tc('filter', 'del', 'dev', 'fc0', 'parent', '1:', 'protocol', 'ip', 'pref', 50)
            try:
                readback()
            except RuntimeError as error:
                if 'UDP flower readback' not in str(error):
                    raise
            else:
                raise AssertionError('missing classifier accepted')
        finally:
            lab.close()
    print(json.dumps(dict(legacy=legacy, corrected=corrected, missing_rule_rejected=True)))


if __name__ == '__main__':
    main()
