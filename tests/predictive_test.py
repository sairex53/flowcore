"""Causal forecast contracts, native bounds, fallback and unchanged safety constraints."""
import copy
import math
import sys
import time
import tempfile
from unittest.mock import Mock, patch
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from predictive import Native, Predictor, METHODS, metrics
from adaptive import Controller, DEFAULT
from observability import Observer
from observability_config import DEFAULT as OBS


def rows(values):
    return [dict(time_ns=(i+1)*10**9,backlog_bytes=q,departure_bps=100000,
                 capacity_bps=1250000) for i,q in enumerate(values)]


class Predictive(unittest.TestCase):
    def test_corrupt_audit_journal_does_not_activate_or_block(self):
        service=Mock(observer=Observer(copy.deepcopy(OBS)),config={})
        for payload in ('','{}','[1]','x'*524289):
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'build') as name:
                path=Path(name);(path/'decisions.json').write_text(payload)
                controller=Controller(service);controller.restore(path)
                self.assertEqual(controller.mode,'off')
                self.assertFalse(controller.history)
        controller=Controller(service)
        for i in range(20): controller.record(result='fixture',detail='x'*20000)
        import json
        self.assertLessEqual(len(json.dumps(list(controller.history)).encode()),262144)

    def test_benchmark_excludes_prior_boot_history(self):
        from research_runner import session_history
        history=[dict(time_ns=10**16,result='pending'),
                 dict(controller_session='old',time_ns=10**16,result='pending'),
                 dict(controller_session='new',time_ns=5,result='pending'),
                 dict(controller_session='new',time_ns=15,result='hypothetical')]
        self.assertEqual(session_history(history,'new',10),[history[-1]])

    def test_extrapolation_and_ablations(self):
        native=Native();data=rows([0,200,400,600,800,1000,1200,1400])
        for method in METHODS:
            forecast=native.forecast(data,method,2000,.95)
            self.assertTrue(all(math.isfinite(value) for value in forecast.values()))
            self.assertGreaterEqual(forecast['forecast_bytes'],0)
            self.assertLessEqual(forecast['forecast_bytes'],5000000)
        self.assertLess(native.forecast(data,'ema',2000,.95)['forecast_bytes'],1400)
        self.assertAlmostEqual(native.forecast(data,'trend',2000,.95)['forecast_bytes'],1800)
        self.assertGreater(native.forecast(data,'hybrid',2000,.95)['trend_bps'],0)
        stable=native.forecast(rows([0]*8),'hybrid',2000,.95)
        self.assertEqual(stable['risk'],0)
        self.assertEqual(stable['lead_seconds'],-1)
        oscillating=native.forecast(rows([0,20000]*4),'hybrid',2000,.95)
        self.assertLess(oscillating['quality'],.45)

    def test_missing_discontinuous_and_malformed(self):
        native=Native()
        self.assertIsNone(native.forecast(rows([0]*3),'hybrid',2000,.95))
        data=rows([0]*8);data[0]['capacity_bps']=500
        self.assertIsNone(native.forecast(data,'hybrid',2000,.95))
        data=rows([0]*8);data[-1]['time_ns']+=3*10**9
        self.assertIsNone(native.forecast(data,'hybrid',2000,.95))
        for change in (dict(backlog_bytes=float('nan')),dict(backlog_bytes=-1),dict(time_ns=10**9)):
            data=rows([0]*8);data[-1].update(change)
            with self.assertRaises(ValueError): native.forecast(data,'hybrid',2000,.95)
        with self.assertRaises(ValueError): native.forecast(rows([0]*65),'hybrid',2000,.95)

    def test_future_labels_and_censoring(self):
        predictor=Predictor()
        def pending(warning=True,early=True,allocation=()):
            return dict(service_class='critical',method='hybrid',issued_ns=10**9,end_ns=3*10**9,
                        warning=warning,early=early,risk=.9,event=False,observed=0,
                        first_event_ns=None,allocation=allocation,censored=False)
        predictor.pending.append(pending())
        self.assertEqual(predictor.counts['hybrid']['tp'],0)
        predictor._resolve(2*10**9,{'critical':dict(event=True)},())
        self.assertEqual(predictor.counts['hybrid']['tp'],0) # outcome still unavailable
        predictor._resolve(3*10**9,{'critical':dict(event=False)},())
        self.assertEqual(predictor.counts['hybrid']['tp'],1)
        self.assertEqual(predictor.status()['metrics']['hybrid']['early_precision'],1)
        predictor.pending.append(pending())
        predictor._resolve(3*10**9,{'critical':dict(event=False)},(('changed',1),))
        self.assertEqual(predictor.counts['hybrid']['censored'],1)
        predictor.pending.append(pending());predictor._resolve(4*10**9,{},())
        self.assertEqual(predictor.counts['hybrid']['censored'],2)
        predictor.pending.append(pending(False,False))
        predictor._resolve(2*10**9,{'critical':dict(event=True)},())
        predictor._resolve(3*10**9,{'critical':dict(event=False)},())
        self.assertEqual(predictor.counts['hybrid']['fn'],1)
        self.assertEqual(metrics()['alerts'],0)

    def test_planner_proactive_and_reactive_fallback(self):
        observer=Observer(copy.deepcopy(OBS))
        service=SimpleNamespace(observer=observer,profile=dict(bandwidth_kbit=10000),active='default',
            config={'adaptive':copy.deepcopy(DEFAULT),'policies':{'default':{'classes':{
                    'critical':{'priority':0}}}}})
        controller=Controller(service);controller.strategy='predictive'
        controller.registry.register(dict(name='a',priority='critical',target_p99_ms=25),0)
        controller.base=dict(critical=(3000,10000),interactive=(2000,10000),
                             standard=(2000,10000),background=(3000,10000))
        controller.current=dict(controller.base)
        window=dict(queue={'root':dict(utilization=.95),'critical':dict(backlogged_samples=2,
                    bytes_per_second=400000)},backlog=dict(p95_bytes=1000),application={
                    'a':{'application':dict(samples=200,p99_us=10000)}})
        self.assertEqual(controller.plan(window,controller.current)['action'],'hold')
        controller.predictor.latest={'critical':{'hybrid':dict(time_ns=time.monotonic_ns(),
            actionable=True,lead_seconds=1.5,risk=.9)}}
        plan=controller.plan(window,controller.current)
        self.assertEqual(plan['action'],'transfer_reservation')
        self.assertTrue(plan['estimator']['a']['proactive'])
        self.assertEqual(sum(rate for rate,_ in plan['proposed'].values()),10000)
        self.assertGreaterEqual(plan['proposed']['background'][0],2000)
        controller.predictor.latest['critical']['hybrid']['time_ns']-=3*10**9
        self.assertEqual(controller.plan(window,controller.current)['action'],'hold')
        controller.mode='active'
        observer.queue_time=time.monotonic_ns()
        service.telemetry={'qdisc':[],'traffic_classes':{}}
        with patch('predictive.Native',side_effect=OSError('native unavailable')):
            controller.sample_prediction()
        self.assertEqual(controller.predictor.error,'native unavailable')
        self.assertEqual(controller.mode,'active')
        window['application']['a']['application']['p99_us']=100000
        self.assertEqual(controller.plan(window,controller.current)['action'],'transfer_reservation')
        window['queue']['critical']['backlogged_samples']=0
        self.assertEqual(controller.plan(window,controller.current)['action'],'hold')

    def test_static_pilot_is_separate_and_frozen(self):
        import json
        from predictive_suite import select_static
        root=Path(__file__).resolve().parents[1]
        pilot=json.loads((root/'scenarios/predictive-static-pilot.json').read_text())
        evaluation=json.loads((root/'scenarios/predictive-buildup.json').read_text())
        runs=[dict(mode=mode,valid=True,failed=0,p99_ms=p99,deadline_attainment=attain,
                   background_mbps=2) for _ in range(2) for mode,p99,attain in
                   [('static20',40,.7),('static30',20,.8),('static40',30,.9)]]
        data=dict(status='complete',manifest=pilot,block_seeds=[1,2],runs=runs)
        self.assertEqual(select_static(data,evaluation)['critical_pct'],40)
        invalid=copy.deepcopy(evaluation);invalid['seed']=pilot['seed']
        with self.assertRaises(ValueError): select_static(data,invalid)
        invalid=copy.deepcopy(evaluation);invalid['network']['bandwidth_kbit']=7000
        with self.assertRaises(ValueError): select_static(data,invalid)

    def test_memory_bounds_and_gap_reset(self):
        predictor=Predictor()
        for i in range(500):
            tc={'qdisc':[dict(root=True,bytes=i*100000),dict(parent='1:10',backlog=0,drops=0)],
                'traffic_classes':{'critical':dict(handle='1:10',stats=dict(bytes=i*50000))}}
            predictor.observe((i+1)*10**9,tc,10000,dict(critical=3000),{},None,())
        self.assertLessEqual(len(predictor.history['critical']),64)
        self.assertLessEqual(len(predictor.pending),128)
        self.assertLessEqual(len(predictor.outcomes),128)
        self.assertEqual(predictor.counts['hybrid']['fp'],0)
        predictor.observe(510*10**9,tc,10000,dict(critical=3000),{},None,())
        self.assertFalse(predictor.latest)
        self.assertFalse(predictor.history['critical'])


if __name__=='__main__': unittest.main()
