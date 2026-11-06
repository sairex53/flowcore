"""Deterministic generated properties for scenario and schedule invariants."""
import copy
import json
import random
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from benchmark import Lab
from intelligence import STATES, State
from research_manifest import load_manifest, validate
from research_minimize import reduce_duration
from research_runner import compose_scenario_network
from service_config import load as load_service_config


def main():
    rng = random.Random(0xF10C0E)
    base = load_manifest(ROOT/'scenarios/chaos-overlap-v3.json')
    # Generated valid bounded event sets always compose to a capacity no greater
    # than the configured path and preserve the worst active impairment.
    for _ in range(400):
        count = rng.randint(0,6)
        events = []
        for index in range(count):
            effects = {}
            if rng.random() < .75:
                effects['bandwidth_kbit'] = rng.randint(100,1000000)
            if rng.random() < .75:
                effects['reverse_bandwidth_kbit'] = rng.randint(100,1000000)
            if rng.random() < .75:
                effects['base_rtt_ms'] = rng.uniform(0,5000)
                effects['jitter_ms'] = min(effects['base_rtt_ms'],rng.uniform(0,100))
            if rng.random() < .75:
                effects['loss_pct'] = rng.uniform(0,20)
            events.append(dict(id=f'event-{index}',effects=effects))
        network = compose_scenario_network(base['network'],events)
        assert 100 <= network['bandwidth_kbit'] <= base['network']['bandwidth_kbit']
        assert 100 <= network['reverse_bandwidth_kbit'] <= base['network']['reverse_bandwidth_kbit']
        assert network['base_rtt_ms'] >= base['network']['base_rtt_ms']
        assert network['jitter_ms'] >= base['network']['jitter_ms']
        assert 0 <= network['loss_pct'] <= 100
        assert 0 <= network['duplicate_pct'] <= 100

    # Manifest generation exercises legal and illegal parser boundaries.
    for _ in range(250):
        candidate = copy.deepcopy(base)
        event = candidate['events'][0]
        event['effects']['bandwidth_kbit'] = rng.randint(100,1000000)
        candidate['events'][0]['duration_seconds'] = rng.randint(1,6)
        assert validate(candidate)['version'] == 3
        invalid = copy.deepcopy(candidate)
        invalid['events'][0]['effects']['bandwidth_kbit'] = rng.choice((-1,0,1000001,1.5,True))
        try:
            validate(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('out-of-range network rate was accepted')

    # Replay boundaries must partition duration and preserve all required stages.
    for _ in range(100):
        candidate = copy.deepcopy(base)
        shortened = reduce_duration(candidate)
        if shortened is not None:
            assert validate(shortened)['duration_seconds'] == sum(s['seconds'] for s in shortened['stages'])
            assert shortened['duration_seconds'] == sum(p['seconds'] for p in shortened['workload']['phases'])

    # Allocation minima stay feasible and overcommit is rejected by the actual config loader.
    with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
        path=Path(work)/'service.json'
        for _ in range(100):
            candidate=json.loads((ROOT/'config/service.json').read_text())
            classes=candidate['policies']['default']['classes']
            for item in classes.values():
                item['min_pct']=rng.randint(1,20)
                item['max_pct']=100
            path.write_text(json.dumps(candidate))
            accepted=load_service_config(path)
            assert sum(item['min_pct'] for item in accepted['policies']['default']['classes'].values()) <= 100
            classes['critical']['min_pct']=25
            classes['interactive']['min_pct']=25
            classes['standard']['min_pct']=25
            classes['background']['min_pct']=26
            path.write_text(json.dumps(candidate))
            try:
                load_service_config(path)
            except ValueError as error:
                assert 'reservations exceed' in str(error)
            else:
                raise AssertionError('overcommitted service reservations were accepted')

    # Namespace references are tied to their live holder and teardown closes all scoped FDs.
    class FakeProcess:
        def __init__(self,pid):
            self.pid=pid
            self.alive=True
            self.waited=False
        def poll(self):
            return None if self.alive else 0
        def wait(self,timeout=None):
            self.waited=True
            self.alive=False
            return 0

    for _ in range(100):
        worker_count=rng.randint(0,8)
        holder_count=rng.randint(1,4)
        lab=Lab(ROOT/'build')
        lab.processes=[FakeProcess(1000+index) for index in range(worker_count)]
        lab.holders=[FakeProcess(2000+index) for index in range(holder_count)]
        lab.namespace_fds=list(range(3000,3000+holder_count))
        expected=[p.pid for p in list(reversed(lab.processes))+list(reversed(lab.holders))]
        with patch('benchmark.os.killpg') as killpg, patch('benchmark.os.close') as close:
            lab.close()
        assert [call.args[0] for call in killpg.call_args_list] == expected
        assert all(proc.waited for proc in lab.processes+lab.holders)
        assert [call.args[0] for call in close.call_args_list] == list(range(3000,3000+holder_count))
        assert not lab.namespace_fds
        reference=Lab(ROOT/'build')
        live=FakeProcess(4000)
        reference.holders=[live]
        reference.namespace_fds=[5000]
        assert reference.namespace_path(0)==f'/proc/{__import__("os").getpid()}/fd/5000'
        live.alive=False
        try:
            reference.namespace_path(0)
        except RuntimeError:
            pass
        else:
            raise AssertionError('stale namespace holder was accepted')

    # The controller state graph only accepts its explicitly declared edges.
    for source, destinations in STATES.items():
        assert all(target in STATES for target in destinations)
        for target in STATES:
            state = State()
            state.name = source
            expected = target == source or target in destinations
            try:
                state.move(target,'generated edge')
            except RuntimeError:
                assert not expected
            else:
                assert expected

    # A correlated loss model cannot be merged with a simultaneous independent loss.
    conflict = copy.deepcopy(base)
    conflict['events'][1]['at_seconds'] = 6.5
    conflict['events'][1]['duration_seconds'] = 2
    conflict['events'][1]['effects'] = {'loss_pct':2}
    try:
        validate(conflict)
    except ValueError:
        pass
    else:
        raise AssertionError('ambiguous overlapping loss model was accepted')
    print('PASS: 1,000 generated scenario/minimizer cases, 100 policy-boundary pairs, 100 namespace cleanup/ownership cases and controller state edges')


if __name__ == '__main__':
    main()
