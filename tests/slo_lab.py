"""Opt-in owned namespace verification for the read-only SLO shadow path."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from hybrid_lab import ROOT, daemon_at


def check():
    from cli import request
    from kernel_api_experiment import host_identity

    host = host_identity()
    missing = request('slo_shadow', deadline=None)['allocation_preview']
    assert not missing['feasible'] and missing['optimizer']['conflicts'][0]['code']=='OWNED_HTB_REQUIRED'
    request('lab_up')
    network = dict(bandwidth_kbit=12000, base_rtt_ms=6, jitter_ms=0, loss_pct=0, reorder_pct=0)
    request('research_conditions', network=network, mode='hybrid-v3', seed=123, link_up=True)
    request('intent_register', intent=dict(name='shadow-demo', priority='critical',
                                           target_p99_ms=25, min_kbit=500, ttl_seconds=30))

    def rates():
        return {row['handle']: (row['rate'], row['ceil']) for row in request('research_snapshot')['tc']['class']
                if row['class']=='htb'}

    before = rates()
    profile = Path(os.environ['FLOWCORE_RUNTIME']).parent/'objective-profile.json'
    profile.write_bytes((ROOT/'config/slo-optimization.json').read_bytes())
    profile.chmod(0o600)
    result = subprocess.run([ROOT/'build/flowcore', 'slo', 'shadow', '--profile',
                             profile, '--budget-ms', '1', '--payload-bytes', '4096', '--processing-us', '2000'],
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    shadow = json.loads(result.stdout)
    assert shadow['disposition']=='SHADOW_HOLD'
    assert shadow['allocation_preview']['actual_verified']
    assert shadow['allocation_preview']['hybrid_authority_revision'] >= 1
    assert shadow['allocation_preview']['optimizer']['admitted_service_count'] == 1
    assert shadow['deadline']['decision']=='REJECT_INFEASIBLE'
    assert shadow['deadline']['reason']=='budget_exhausted'
    assert 'hold' in shadow['candidates'] and 'static' in shadow['candidates']
    assert rates()==before
    request('lab_down')
    assert host_identity()==host
    print('SLO shadow: allocation readback and cooperative deadline impossibility verified without tc changes')


if __name__ == '__main__':
    if os.geteuid():
        raise SystemExit('requires root for disposable namespace setup')
    with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='slo-lab-') as tmp, daemon_at(Path(tmp)):
        check()
