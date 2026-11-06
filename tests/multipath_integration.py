"""Opt-in privileged validation using disposable namespaces only."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from multipath import experiment

if os.geteuid() != 0:
    raise SystemExit('Run as root: creates disposable network namespaces')
result = experiment(strategy='latency-preferred', scenario='multiple-failures',
                    path_count=3, duration=8, repetitions=1)
trial = result['runs'][0]
assert result['host_mptcp_state_unchanged']
assert trial['valid'] and trial['mptcp_confirmed']
assert not trial['fallback_observed']
assert trial['observed_mptcp_subflows'] >= 2
assert trial['connection_survived']
assert len(trial['transitions']) == 4
for event in trial['transitions']:
    expected = event['event'] == 'path-up'
    assert event['kernel_state']['link_up'] is expected
    assert event['kernel_state']['peer_link_up'] is expected
    assert event['policy_decision']['status'] == 'applied'
assert trial['namespace_disable_prevented_mptcp_socket']
cleanup = json.loads((Path(result['experiment']) / 'r00-latency-preferred' / 'cleanup.json').read_text())
assert not cleanup['errors'] and not cleanup['remaining_owned_endpoints']
print(json.dumps(dict(experiment=result['experiment'],status='PASS',
                     p99_ms=trial['p99_ms'],failed=trial['failed'])))
