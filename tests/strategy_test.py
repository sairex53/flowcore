"""Deterministic strategy safety and evidence tests. No kernel writes."""
import copy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from research_manifest import load_manifest
from strategy_engine import Engine, NANO
from strategy_registry import CLASS, recommend


LIMITS = dict(minimum_samples=40, max_failure_ratio=.05, max_p99_ms=100,
              p99_regression=.15, max_deadline_miss_ratio=.5,
              minimum_background_kbit=1000, background_floor_pct=20)


def telemetry(tail=20_000, failures=0):
    app = dict(ok=80-failures, failure=failures, timeout=0, deadline_reported=80,
               deadline_miss=0, p99_us=tail, samples=80-failures)
    return dict(application={'api': {'application':app}}, backlog={'p95_bytes':12000},
                queue={'root':{'utilization':.9},'background':{'bytes_per_second':500000}})


class Controller:
    def __init__(self):
        self.mode = 'off'
        self.strategy = 'static'
        self.error = None
        self.current = None
        self.cfg = dict(window_seconds=2, background_floor_pct=20)
        self.registry = SimpleNamespace(revision=0,rows={'api':{}})
        self.starts = self.stops = self.verifications = 0
        self.rollback_failure = False

    def policy(self):
        return dict(scheduler='htb',classes={name:dict(min_pct=25,max_pct=100)
                    for name in ('critical','interactive','standard','background')})

    def capacity(self):
        return 10000

    def verify_lab_baseline(self):
        self.verifications += 1
        if self.rollback_failure:
            raise RuntimeError('unexpected kernel allocation')

    def start(self, mode, strategy):
        self.mode, self.strategy = mode, strategy
        self.starts += 1

    def stop(self, _reason):
        self.mode = 'off'
        self.stops += 1

    def detach(self):
        self.mode = 'off'

    def plan(self, _window, current):
        return dict(action='hold', proposed=current, background_floor_kbit=2000)

    def qualify(self, plan):
        return plan


class Canary(unittest.TestCase):
    def setUp(self):
        self.now = 10*NANO
        self.row = telemetry()
        self.control = Controller()
        observer = SimpleNamespace(cfg={'enabled': True}, generation=1, sampled_ns=self.now,
            errors={},truncated=False,window=lambda *_args,**_options: self.row)
        classes = {name:dict(rate=312500,ceil=1250000) for name in self.control.policy()['classes']}
        self.service = SimpleNamespace(lab=SimpleNamespace(assert_owned_sender=lambda: None),
            research=True, research_link_up=True, error=None, controller=self.control, observer=observer,
            config={'telemetry_seconds':1}, log=lambda *_args,**_kwargs: None,
            telemetry={'traffic_classes':classes})
        self.engine = Engine(self.service, lambda: self.now)
        self.engine.registry = lambda refresh=False: {name:{'canary':True} for name in CLASS}
        self.root = patch('strategy_engine.os.geteuid',return_value=0)
        self.root.start()
        self.addCleanup(self.root.stop)

    def window(self, row=None):
        if row is not None:
            self.row = row
        self.now = self.engine.experiment['next_end']+NANO//3
        self.service.observer.sampled_ns = self.now
        self.engine.tick()

    def arm(self, **options):
        options.setdefault('confirmed', True)
        return self.engine.arm('queue-aware','owned-fc0',20,LIMITS,**options)

    def start(self):
        self.arm()
        self.window()
        self.assertEqual((self.engine.state,self.control.starts),('CANARY',1))

    def test_shadow_is_read_only(self):
        self.engine.subscribe(['static','queue-aware'],30)
        self.engine.tick()
        result=self.engine.status()['shadow']
        self.assertEqual(len(result),2)
        self.assertEqual(result['static']['observation'],result['queue-aware']['observation'])
        self.assertTrue(all(row['evidence']=='prediction' for row in result.values()))
        self.assertEqual((self.control.starts,self.control.stops),(0,0))

    def test_consecutive_relative_regression_and_recovery(self):
        self.start()
        self.window(telemetry(30_000))
        self.assertEqual(self.engine.state,'CANARY')
        self.window(telemetry(20_000))
        self.window(telemetry(30_000))
        self.assertEqual(self.engine.state,'CANARY')
        self.window()
        self.assertEqual((self.engine.state,self.control.stops,self.engine.switches),('RECOVERING',1,2))
        self.assertEqual(self.engine.regressions,1)
        self.assertTrue(self.engine.events[-1]['verified'])
        with self.assertRaises(ValueError):
            self.arm()

    def test_hard_failure_and_stale_telemetry(self):
        self.start()
        self.window(telemetry(failures=8))
        self.assertEqual(self.engine.events[-1]['reason'],'failure_rate')
        self.now += 6*NANO
        self.service.observer.sampled_ns = self.now
        self.row = telemetry()
        self.start_after_recovery()
        self.service.observer.truncated = True
        self.engine.tick()
        self.assertEqual(self.engine.events[-1]['reason'],'telemetry_lost')

    def start_after_recovery(self):
        self.arm()
        self.window()
        self.assertEqual(self.engine.state,'CANARY')

    def test_scope_change_refuses_mutation_and_unconfirmed_lease(self):
        with self.assertRaises(PermissionError):
            self.arm(confirmed=False)
        self.start()
        self.service.observer.generation += 1
        self.engine.tick()
        self.assertEqual((self.control.stops,self.engine.state),(0,'FAULTED'))
        self.assertEqual(self.control.mode, 'off')
        self.assertFalse(self.engine.events[-1]['verified'])

    def test_kernel_rollback_rejection_is_latched(self):
        self.start()
        self.control.rollback_failure = True
        self.engine.rollback()
        self.assertEqual(self.engine.state,'FAULTED')
        self.assertFalse(self.engine.events[-1]['verified'])
        self.now += 6*NANO
        with self.assertRaises(ValueError):
            self.arm()

    def test_dwell_and_unrelated_running_controller(self):
        self.control.mode = 'active'
        with self.assertRaises(ValueError):
            self.arm()
        self.control.mode = 'off'
        self.arm()
        self.window(dict(telemetry(),backlog={'p95_bytes':0}))
        self.assertEqual(self.engine.state,'BASELINE')
        self.now += 25*NANO
        self.engine.tick()
        self.assertEqual(self.control.starts,0)


class Evidence(unittest.TestCase):
    def test_shadow_cli_allows_empty_subscription_to_clear(self):
        import argparse
        from strategy_cli import add_commands
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest='command', required=True)
        add_commands(commands)
        parsed = parser.parse_args(['strategy','shadow'])
        self.assertEqual(parsed.names, [])

    def test_exact_context_minimum_evidence_and_switch_cost(self):
        manifest = load_manifest(ROOT/'scenarios/strategy-calibration.json')
        manifest['repetitions'] = 12
        seeds = list(range(1,13))
        runs = [dict(repetition=rep,mode=mode,workload_seed=seed,valid=True,
                    offered=100,failed=0,p99_ms=40 if mode=='static' else 30,
                    deadline_attainment=.97,total_mbps=10,background_mbps=3,
                    adjustments=int(mode!='static'))
                for rep,seed in enumerate(seeds) for mode in manifest['modes']]
        summary = dict(manifest=manifest,version=2,status='complete',experiment_id='synthetic-fixture',
                       block_seeds=seeds,environment={},runs=runs)
        self.assertEqual(recommend(summary,manifest,['queue-aware'],LIMITS)['selected'],'queue-aware')
        from strategy_cli import dispatch
        from research_manifest import net_profile

        args = SimpleNamespace(action='activate', name='auto', summary='fixture.json',
                               context='context.json', scope='owned-fc0', seconds=12,
                               guards='guards.json', confirm=True, switch_cost=.05)
        responses = {'strategy_list': {name: dict(available=True, canary=name=='queue-aware')
                                       for name in CLASS},
                     'status': {'profile': net_profile(manifest['network'])},
                     'strategy_activate': {'state': 'BASELINE'}}
        with patch('cli.request', side_effect=lambda op, **fields: responses[op]) as rpc, \
             patch('durable_state.read_json', return_value=LIMITS), \
             patch('research_manifest.load_manifest', return_value=manifest), \
             patch('research_stats.load_summary', return_value=summary):
            self.assertTrue(dispatch(args)['armed'])
            self.assertEqual(rpc.call_args.kwargs['name'], 'queue-aware')
            rpc.reset_mock()
            args.switch_cost = .3
            self.assertFalse(dispatch(args)['armed'])
            self.assertEqual([call.args[0] for call in rpc.call_args_list], ['strategy_list'])
        self.assertEqual(recommend(summary,manifest,['queue-aware'],LIMITS,.3)['selected'],'static')
        changed = copy.deepcopy(manifest)
        changed['network']['base_rtt_ms'] += 1
        result = recommend(summary,changed,['queue-aware'],LIMITS)
        self.assertFalse(result['condition_match'])
        self.assertEqual(result['selected'],'static')
        summary['runs'] = [dict(row, adjustments=0) for row in runs]
        result = recommend(summary,manifest,['queue-aware'],LIMITS)
        self.assertEqual(result['evidence']['queue-aware']['status'], 'no_verified_actuation')
        self.assertEqual(result['selected'], 'static')
        summary['runs'] = runs[:4]
        self.assertEqual(recommend(summary,manifest,['queue-aware'],LIMITS)['selected'],'static')


if __name__ == '__main__':
    unittest.main()
