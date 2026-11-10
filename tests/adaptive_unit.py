"""Intent, stability and actuator invariants without root."""
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import adaptive_tc
from adaptive import Controller, STRATEGIES, objective
from intents import Registry
from observability import Observer
from observability_config import DEFAULT
from service_config import CLASSES


class Unit(unittest.TestCase):
    def test_objective_uses_application_samples_and_deadband(self):
        intent = dict(priority='critical', target_p99_ms=10, desired_kbit=0)
        view = {'application': {}, 'queue': {}, 'rtt': {'p99_us': 99999}}
        self.assertEqual(objective(view, 'demo', intent, 20)['state'], 'insufficient_data')
        view['application']['demo'] = {'application': {'samples': 20, 'p99_us': 10500}}
        result = objective(view, 'demo', intent, 20)
        self.assertEqual(result['state'], 'violated')
        self.assertFalse(result['outside_deadband'])

    def test_registry_conflicts_expiry_and_persistence(self):
        obs = Observer(copy.deepcopy(DEFAULT))
        registry = Registry(obs)
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as directory:
            path = Path(directory)
            registry.restore(path)
            a = registry.register({'name':'demo','persistent':True}, 0)
            b = registry.register({'name':'demo','persistent':True}, 0)
            self.assertNotEqual(a['instance_token'], b['instance_token'])
            with self.assertRaises(ValueError):
                registry.register({'name':'demo','persistent':True}, 123)
            with self.assertRaises(ValueError):
                registry.register({'name':'other'}, 0)
            with self.assertRaises(ValueError):
                registry.register({'name':'other','min_kbit':900,'max_kbit':800}, 0)
            restored = Registry(Observer(copy.deepcopy(DEFAULT)))
            restored.restore(path)
            self.assertEqual(restored.inspect('demo')['owner_uid'], 0)
            self.assertEqual(restored.inspect('demo')['instances'], [])
            restored.rows['demo']['monotonic_expiry'] = 0
            restored.expire()
            self.assertNotIn('demo', restored.rows)
            self.assertEqual(json.loads((path/'intents.json').read_text()), {})

    def test_plan_does_not_exceed_floor(self):
        observer = Observer(copy.deepcopy(DEFAULT))
        service = Mock(observer=observer, profile={'bandwidth_kbit': 10000}, active='default',
                       config={'policies':{'default':{'classes':{'critical':{'priority':0}}}}})
        service.lab.assert_owned_sender = Mock()
        engine = Controller(service)
        engine.registry.register({'name':'demo','background_min_pct':25}, 0)
        engine.base = {'critical':(3000,10000),'interactive':(2000,10000),
                       'standard':(2000,10000),'background':(3000,10000)}
        engine.current = engine.base
        window = {'queue':{'root':{'utilization':.9}},'backlog':{'p95_bytes':10000},
                  'application':{'demo':{'application':{'samples':150,'p99_us':100000}}}}
        measured = objective(window,'demo',engine.registry.rows['demo']['intent'],20)
        self.assertTrue(STRATEGIES['slo'](measured, True, None))
        self.assertEqual(engine.plan(window, engine.current)['action'], 'hold')
        window['queue']['critical'] = dict(backlogged_samples=2, bytes_per_second=400000, borrowed=35)
        window['application']['demo']['application']['samples'] = 30
        self.assertEqual(engine.plan(window, engine.current)['action'], 'hold')
        window['application']['demo']['application']['samples'] = 150
        proposed = engine.plan(window, engine.current)['proposed']
        self.assertEqual(proposed['background'][0], 2500)
        self.assertEqual(proposed['critical'][0], 3500)
        self.assertEqual(engine.plan(window, proposed)['action'], 'hold')

    def test_reversals_and_telemetry_failure_stop_controller(self):
        observer = Observer(copy.deepcopy(DEFAULT))
        service = Mock(observer=observer, profile={'bandwidth_kbit':10000}, active='default',
                       config={'telemetry_seconds':1,
                               'policies':{'default':{'classes':{'critical':{'priority':0}}}}})
        service.lab.assert_owned_sender = Mock()
        engine = Controller(service)
        engine.registry.register({'name':'demo'}, 0)
        baseline = {'critical':(3000,10000),'interactive':(2000,10000),
                    'standard':(2000,10000),'background':(3000,10000)}
        boosted = dict(baseline, critical=(3500,10000), background=(2500,10000))
        engine.base = baseline
        engine.revision = engine.registry.revision
        engine.mode = 'active'
        engine.allocation_confirmed = True
        service.error = None
        engine.write = lambda target: setattr(engine, 'current', target)
        window = {'queue':{'root':{'utilization':.9}},'backlog':{'p95_bytes':10000},
                  'application':{'demo':{'application':{'samples':150,'p99_us':100000}}}}
        rows = [{'class':'htb','handle':f'1:{minor}','rate':boosted[name][0]*125,
                 'ceil':boosted[name][1]*125} for name,minor in CLASSES.items()]
        rows.append({'class':'htb','handle':'1:1','rate':1250000,'ceil':1250000})
        service.telemetry = {'class':rows,'qdisc':[]}
        with patch.object(observer, 'window', return_value=window):
            engine.current = boosted
            observer.sampled_ns = time.monotonic_ns()
            before = engine.plan(window, boosted)['objectives']['demo']
            engine.pending = dict(service='demo', allocation=baseline, before=before,
                                  start_ns=0, end_ns=0, verified_ns=0)
            engine.last_tick = time.monotonic()
            window['application']['demo']['application']['samples'] = 250
            window['application']['demo']['application']['deadline_miss'] = 10
            engine.tick()
            self.assertEqual(engine.history[-1]['result'], 'inconclusive')
            self.assertTrue(engine.history[-1]['degraded'])
            self.assertEqual(engine.reversals, 0)
            self.assertEqual(engine.current, baseline)
            window['application']['demo']['application']['samples'] = 150
            window['application']['demo']['application']['deadline_miss'] = 0
            for _ in range(2):
                engine.current = boosted
                engine.last_tick = 0
                observer.sampled_ns = time.monotonic_ns()
                before = engine.plan(window, boosted)['objectives']['demo']
                engine.pending = dict(service='demo', allocation=baseline, before=before, start_ns=0,end_ns=0,verified_ns=0)
                engine.tick()
            self.assertEqual(engine.mode, 'off')
            self.assertIn('oscillation', engine.error)
            self.assertEqual(engine.current, baseline)
            engine.mode = 'active'
            engine.allocation_confirmed = True
            engine.current = boosted
            engine.last_tick = 0
            observer.errors['inet_diag'] = 'injected outage'
            engine.tick()
            self.assertEqual(engine.mode, 'off')
            self.assertEqual(engine.error, 'telemetry unavailable')
            self.assertEqual(engine.current, baseline)
            observer.errors.clear()
            engine.mode = 'active'
            engine.allocation_confirmed = True
            engine.current = boosted
            engine.next_action = 0
            window['application']['demo']['application']['p99_us'] = 5000
            for _ in range(engine.cfg['recovery_windows']):
                engine.last_tick = 0
                observer.sampled_ns = time.monotonic_ns()
                engine.tick()
            self.assertEqual(engine.current, boosted)
            window['queue']['root']['utilization'] = .5
            for _ in range(engine.cfg['recovery_windows']):
                engine.last_tick = 0
                observer.sampled_ns = time.monotonic_ns()
                engine.tick()
            self.assertEqual(engine.current, baseline)
            self.assertEqual(engine.history[-1]['result'], 'released')

    def test_fault_fallback_preserves_authorized_class_ceiling(self):
        observer = Observer(copy.deepcopy(DEFAULT))
        service = Mock(observer=observer, profile={'bandwidth_kbit':10000})
        engine = Controller(service)
        engine.registry.register({'name':'demo','max_kbit':7000}, 0)
        engine.base = {'critical':(3000,10000),'interactive':(2000,10000),
                       'standard':(2000,10000),'background':(3000,10000)}
        engine.current = dict(engine.base, critical=(3500,7000), background=(2500,10000))
        engine.mode = 'active'
        engine.state.move('OBSERVING','test activation')
        with (patch.object(engine,'tick',side_effect=RuntimeError('injected evaluation failure')),
              patch.object(engine,'write') as write):
            engine.safe_tick()
        write.assert_called_once_with(dict(engine.base, critical=(3000,7000)))
        self.assertEqual(engine.mode,'off')
        self.assertEqual(engine.state.name,'SAFE_FALLBACK')

    def test_probe_cutoff_is_kernel_commit_and_deadline_is_independent(self):
        observer = Observer(copy.deepcopy(DEFAULT))
        service = Mock(observer=observer, profile={'bandwidth_kbit':10000}, active='default',
                       config={'telemetry_seconds':1,
                               'policies':{'default':{'classes':{'critical':{'priority':0}}}}})
        service.lab.assert_owned_sender = Mock()
        engine = Controller(service)
        engine.registry.register({'name':'demo'}, 0)
        engine.base = engine.current = {'critical':(3000,10000),'interactive':(2000,10000),
                                       'standard':(2000,10000),'background':(3000,10000)}
        engine.mode = 'active'
        engine.allocation_confirmed = True
        service.error = None
        engine.revision = engine.registry.revision
        window = dict(queue={'root':{'utilization':.9},
                             'critical':dict(backlogged_samples=2,bytes_per_second=400000)},
                      backlog={'p95_bytes':10000},
                      application={'demo':{'application':dict(samples=150,p99_us=100000)}})
        service.telemetry = dict(qdisc=[], **{'class':[
            dict(handle=f'1:{minor}', rate=engine.current[name][0]*125,
                 ceil=10000*125, **{'class':'htb'}) for name,minor in CLASSES.items()]})
        service.telemetry['class'].append(dict(handle='1:1',rate=1250000,ceil=1250000,**{'class':'htb'}))
        verified = time.monotonic_ns()
        def write(target):
            engine.current = target
            return dict(verified_ns=verified)
        engine.write = write
        with patch.object(observer,'window',return_value=window):
            for _ in range(2):
                engine.last_tick = 0
                observer.sampled_ns = time.monotonic_ns()
                engine.tick()
        self.assertIsNotNone(engine.pending)
        self.assertEqual(observer.probe_start_ns, verified)
        self.assertGreater(engine.pending['start_ns'], verified)
        now = engine.pending['start_ns']+10_000_000
        with patch.object(observer,'clock',return_value=now):
            observer.ingest(dict(op='request_sample',service='demo',outcome='ok',duration_us=20000),0)
        app = observer.window(now,2)['application']['demo']['application']
        self.assertEqual(app['probe_overlap'],0)

    def test_completed_windows_exclude_partial_and_track_probe_overlap(self):
        now = 10_100_000_000
        observer = Observer(copy.deepcopy(DEFAULT), clock=lambda: now)
        observer.probe_start_ns = 9_000_000_000
        observer.ingest(dict(op='request_sample', service='demo', outcome='ok',
                             duration_us=100_000, completed_ns=9_020_000_000), 0)
        observer.ingest(dict(op='request_sample', service='demo', outcome='failure',
                             duration_us=100, completed_ns=9_050_000_000, deadline_ms=10), 0)
        observer.ingest(dict(op='request_sample', service='demo', outcome='ok', duration_us=20), 0)
        complete = observer.window(now, 2, complete=True)['application']['demo']['application']
        self.assertEqual((complete['ok'], complete['failure'], complete['probe_overlap']), (1,1,1))
        self.assertEqual(complete['deadline_miss'], 1)
        self.assertEqual(observer.window(now,2)['application']['demo']['application']['ok'],2)
        with self.assertRaises(ValueError):
            observer.ingest(dict(op='request_sample', service='demo', outcome='ok',
                                 duration_us=10, completed_ns=now+1), 0)

    def test_transaction_rolls_back_partial_write_and_refuses_drift(self):
        before = {'critical':(3000,10000),'interactive':(2000,10000),
                  'standard':(2000,10000),'background':(3000,10000)}
        after = dict(before, critical=(3500,10000), background=(2500,10000))
        actual = dict(before)
        policy = {'classes':{name:{'priority':i,'min_pct':1,'max_pct':100} for i,name in enumerate(CLASSES)}}
        class Lab:
            def assert_owned_sender(self):
                pass

            def tc(self, *args):
                minor = int(args[args.index('classid')+1].split(':')[1])
                name = next(name for name, value in CLASSES.items() if value==minor)
                rate = int(args[args.index('rate')+1].removesuffix('kbit'))
                if name == 'critical':
                    raise RuntimeError('injected tc failure')
                actual[name] = (rate,10000)
        with patch('adaptive_tc.read', side_effect=lambda *_: dict(actual)):
            with self.assertRaisesRegex(RuntimeError, 'rolled back'):
                adaptive_tc.change(Lab(), 10000, policy, before, after)
            self.assertEqual(actual,before)
            actual['background'] = (2800,10000)
            with self.assertRaisesRegex(RuntimeError, 'drift'):
                adaptive_tc.change(Lab(), 10000, policy, before, after)
            self.assertEqual(actual['background'][0],2800)


if __name__ == '__main__':
    unittest.main()
