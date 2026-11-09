"""Opt-in Linux fixture for Hybrid V3 authorization, readback and rebase."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))


@contextmanager
def daemon_at(work):
    os.environ.update(FLOWCORE_RUNTIME=str(work/'run'), FLOWCORE_STATE=str(work/'state'),
                      FLOWCORE_RESULTS=str(work/'results'))
    from benchmark import ENV
    from observer_perf import await_service, stop
    from research_runner import child
    from service_config import load

    (work/'results').mkdir()
    cfg = load(ROOT/'config/service.json')
    cfg['lab'].update(bandwidth_kbit=12000, delay_ms=6, jitter_ms=0)
    cfg['telemetry_seconds'] = 1
    cfg['observability']['bpf'] = 'off'
    config = work/'service.json'
    config.write_text(json.dumps(cfg))
    config.chmod(0o600)
    with (work/'daemon.log').open('w+') as log:
        daemon = child([ROOT/'build/flowcored', '--research', '--config', config], stdout=log, stderr=log, env=ENV)
        try:
            await_service(daemon)
            yield
        except BaseException:
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            stop(daemon)


def check():
    from cli import request
    from kernel_api_experiment import host_identity

    host = host_identity()
    network = dict(bandwidth_kbit=12000, base_rtt_ms=6, jitter_ms=0, loss_pct=0, reorder_pct=0)
    request('lab_up')
    request('research_conditions', network=network, mode='hybrid-v3', seed=123, link_up=True)
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        sample = request('research_sample')
        if sample['sample_ns'] and sample['integrated_quality']:
            break
        time.sleep(.05)
    else:
        raise AssertionError('no healthy Hybrid V3 observer sample')
    auth = sample['hybrid']
    opts = dict(action='shift', revision=auth['revision'], sample_ns=sample['sample_ns'], capacity_kbit=12000)

    def rejected(op, reason, **values):
        try:
            request(op, **values)
        except ValueError as error:
            assert reason in str(error), error
        else:
            raise AssertionError(f'{op} unexpectedly accepted')

    rejected('hybrid_apply', 'stale Hybrid V3 class transaction', **dict(opts, revision=opts['revision']-1))
    rejected('hybrid_apply', 'stale Hybrid V3 observer sample', **dict(opts, sample_ns=0))
    rejected('controller_start', 'reserves adaptive authority', strategy='queue-aware')
    attempt = subprocess.run([sys.executable, '-B', '-c',
                              'import json,sys; sys.path.insert(0,"scripts"); from cli import request; '
                              'request("hybrid_apply", **json.loads(sys.argv[1]))', json.dumps(opts)],
                             cwd=ROOT, text=True, capture_output=True, timeout=5)
    assert attempt.returncode and 'owning Research Lab runner' in attempt.stderr, attempt.stderr

    def rates():
        return {row['handle']: [row['rate']//125, row['ceil']//125]
                for row in request('research_snapshot')['tc']['class'] if row.get('class')=='htb'}

    baseline = rates()
    opts['sample_ns'] = request('research_sample')['sample_ns']
    shifted = request('hybrid_apply', **opts)
    actual = rates()
    assert shifted['revision'] == auth['revision']+1 and shifted['boosted']
    assert actual['1:10'] == [4200, 12000] and actual['1:20'] == [3000, 12000], actual
    assert actual == dict(baseline, **{'1:10': [4200, 12000], '1:20': [3000, 12000]})
    rejected('hybrid_apply', 'stale Hybrid V3 class transaction', **opts)
    # Capacity transition must first restore static HTB, then rebase its percentages.
    network['bandwidth_kbit'] = 7000
    request('research_conditions', network=network, mode='hybrid-v3', seed=124, link_up=True)
    rebased = request('research_sample')['hybrid']
    actual = rates()
    assert not rebased['boosted'] and rebased['revision'] == shifted['revision']+2
    assert actual['1:10'] == actual['1:20'] == [2100, 7000], actual
    request('lab_down')
    assert host_identity() == host, 'host network changed'
    print('Hybrid V3 namespace: peer authorization, stale generation/telemetry, 12 Mbit/s shift, 7 Mbit/s verified rebase')


if __name__ == '__main__':
    if os.geteuid():
        raise SystemExit('requires root for disposable namespace setup')
    with tempfile.TemporaryDirectory(dir=ROOT/'build', prefix='hybrid-v3-') as tmp, daemon_at(Path(tmp)):
        check()
