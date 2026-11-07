"""Autotuning search-space, paired-score and uncertainty tests."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))

import autotune
from research_manifest import validate


def manifest(seed=71,bandwidth=10000,rps=30):
    data=copy.deepcopy(json.loads((ROOT/'scenarios/policy-optimizer-paired.json').read_text()))
    data['duration_seconds']=6
    data['workload']['phases']=[dict(seconds=6,requests_per_second=rps,background_streams=1)]
    data['workload']['payload_bytes']=1024
    data['workload']['max_inflight']=16
    data['workload']['bulk_bytes_per_stream']=0
    data['network']['bandwidth_kbit']=bandwidth
    data['repetitions']=10
    data['seed']=seed
    data['modes']=['static30']
    data['gates']['baseline']='static30'
    return validate(data)


class AutotuneTests(unittest.TestCase):
    def test_grid_contains_baseline_and_is_bounded(self):
        result=autotune._search_values('grid',31,6,5,9)
        self.assertEqual(result['values'],[20,25,30,31,35,40])
        with self.assertRaises(ValueError):
            autotune._search_values('grid',30,4,1,9)

    def test_random_is_reproducible_unique_and_keeps_manual(self):
        left=autotune._search_values('random',30,8,5,123)
        right=autotune._search_values('random',30,8,5,123)
        self.assertEqual(left,right)
        self.assertIn(30,left['values'])
        self.assertEqual(len(left['values']),len(set(left['values'])))
        with self.assertRaises(ValueError):
            autotune._search_values('random',30,1,5,123)

    def test_manual_and_adaptive_boundaries(self):
        self.assertEqual(autotune._search_values('manual',20,1,1,3)['values'],[20])
        self.assertEqual(autotune._search_values('coordinate',40,5,4,3),
                         dict(kind='adaptive',initial=40,step=4))
        with self.assertRaises(ValueError):
            autotune._search_values('manual',30,2,5,1)

    def test_plan_requires_independent_generalization(self):
        training=manifest(seed=71,bandwidth=10000,rps=30)
        other=manifest(seed=72,bandwidth=8000,rps=45)
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as tmp:
            train=Path(tmp)/'train.json'; hold=Path(tmp)/'hold.json'
            train.write_text(json.dumps(training)); hold.write_text(json.dumps(other))
            plan=autotune.make_plan(train,'grid',5,5,10,24,[hold],budget_seconds=3600)
            self.assertEqual(len(plan['holdouts']),1)
            self.assertEqual(plan['training']['manifest']['seed'],71)
            with self.assertRaises(ValueError):
                autotune.make_plan(train,'grid',5,5,10,24,[train],budget_seconds=3600)

    def test_budget_and_invalid_weight_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as tmp:
            train=Path(tmp)/'train.json'; train.write_text(json.dumps(manifest()))
            with self.assertRaises(ValueError):
                autotune.make_plan(train,'grid',5,5,10,24,budget_seconds=10)
            with self.assertRaises(ValueError):
                autotune.make_plan(train,'manual',1,5,10,24,weights={'unknown':1})

    def test_plan_snapshot_roundtrip_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as tmp:
            src=Path(tmp)/'train.json'; src.write_text(json.dumps(manifest()))
            plan=autotune.make_plan(src,'manual',1,5,10,24,budget_seconds=3600)
            plan['hard_constraints'].pop('min_objective_improvement')
            plan['search']['unexpected']=1
            path=Path(tmp)/'plan.json'; path.write_text(json.dumps(plan))
            with self.assertRaises(ValueError): autotune.load_plan(path)
            plan['search'].pop('unexpected')
            path.write_text(json.dumps(plan))
            loaded=autotune.load_plan(path)
            self.assertEqual(loaded['schema'],1)
            self.assertIn('min_objective_improvement',loaded['hard_constraints'])
            plan['search']['baseline_pct']=19
            path.write_text(json.dumps(plan))
            with self.assertRaises(ValueError):
                autotune.load_plan(path)

    def test_paired_score_uses_all_failures_and_censors_tail_comparison(self):
        base=dict(offered=100,failed=5,p99_ms=10,deadline_attainment=.8,
                  background_mbps=2,jain_index=.9,cpu_seconds=1,
                  peak_daemon_rss_bytes=100,adjustments_per_minute=0)
        contender=dict(offered=100,failed=0,p99_ms=5,deadline_attainment=.9,
                       background_mbps=2.2,jain_index=.95,cpu_seconds=.8,
                       peak_daemon_rss_bytes=90,adjustments_per_minute=0)
        score,parts=autotune._paired_improvement(base,contender,autotune.WEIGHT_DEFAULTS)
        self.assertGreater(score,0)
        self.assertEqual(parts['failure_ratio'],.05)
        self.assertNotIn('p99_ms',parts)
        near_zero=dict(base, cpu_seconds=0, peak_daemon_rss_bytes=0)
        changed=dict(contender, cpu_seconds=1, peak_daemon_rss_bytes=4096)
        _, bounded=autotune._paired_improvement(near_zero,changed,autotune.WEIGHT_DEFAULTS)
        self.assertLessEqual(abs(bounded['cpu_seconds']),1)
        self.assertLessEqual(abs(bounded['peak_daemon_rss_bytes']),1)

    def test_candidate_report_uses_paired_run_blocks_and_component_intervals(self):
        data_manifest=manifest()
        data_manifest['modes']=['static20','static30']
        data_manifest['gates']['baseline']='static30'
        rows=[]
        for repetition in range(10):
            common=dict(repetition=repetition,valid=True,offered=100,failed=0,
                deadline_attainment=.9,background_mbps=1.5,jain_index=.98,
                cpu_seconds=.4,peak_daemon_rss_bytes=30_000_000,adjustments_per_minute=0,
                workload_seed=repetition+1)
            rows.append(dict(common,mode='static30',p99_ms=20.0))
            rows.append(dict(common,mode='static20',p99_ms=19.0))
        plan=dict(objectives=autotune._normalize_objectives({}),
                  hard_constraints=autotune._hard_constraints({}))
        report=autotune._candidate_result(dict(manifest=data_manifest,runs=rows),
                                           'static30','static20',plan,7)
        self.assertTrue(report['valid'])
        self.assertEqual(report['pairs'],10)
        self.assertEqual(report['confidence_interval']['independent_units'],10)
        self.assertIn('p99_ms',report['paired_component_intervals'])
        self.assertEqual(report['paired_component_intervals']['p99_ms']['low'],.05)

    def test_bootstrap_reproducible_and_noise_stays_inconclusive(self):
        clear=[.2,.2,.2,.2,.2,.2,.2,.2,.2,.2]
        first=autotune._bootstrap(clear,991)
        self.assertEqual(first,autotune._bootstrap(clear,991))
        self.assertGreater(first['low'],0)
        noisy=[-.3,.2,-.1,.3,-.2,.1,-.15,.25,-.05,.05]
        ci=autotune._bootstrap(noisy,992)
        self.assertLessEqual(ci['low'],0)
        self.assertGreaterEqual(ci['high'],0)

    def test_budget_stops_before_network_experiment(self):
        import paths
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as tmp:
            source=Path(tmp)/'training.json'; source.write_text(json.dumps(manifest()))
            plan=autotune.make_plan(source,'manual',1,5,10,10,budget_seconds=3600)
            plan['budget']['max_test_seconds']=1
            plan_path=Path(tmp)/'plan.json'; plan_path.write_text(json.dumps(plan))
            old_results=paths.RESULTS; old_owned=autotune.owned_directory; old_execute=autotune.execute
            paths.RESULTS=Path(tmp)/'results'; paths.RESULTS.mkdir()
            def owned(path): Path(path).mkdir(); return Path(path)
            def forbidden(*args,**kwargs): raise AssertionError('experiment must not start')
            autotune.owned_directory=owned; autotune.execute=forbidden
            try:
                state=autotune.run_plan(plan_path)
                self.assertEqual(state['status'],'budget_exhausted')
            finally:
                paths.RESULTS=old_results; autotune.owned_directory=old_owned; autotune.execute=old_execute

    def test_interrupted_run_persists_partial_state(self):
        import paths
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as tmp:
            source=Path(tmp)/'training.json'; source.write_text(json.dumps(manifest()))
            plan=autotune.make_plan(source,'manual',1,5,10,11,budget_seconds=3600)
            plan_path=Path(tmp)/'plan.json'; plan_path.write_text(json.dumps(plan))
            old_results=paths.RESULTS; old_owned=autotune.owned_directory; old_execute=autotune.execute
            paths.RESULTS=Path(tmp)/'results'; paths.RESULTS.mkdir()
            def owned(path): Path(path).mkdir(); return Path(path)
            def interrupted(*args,**kwargs): raise KeyboardInterrupt()
            autotune.owned_directory=owned; autotune.execute=interrupted
            try:
                with self.assertRaises(KeyboardInterrupt): autotune.run_plan(plan_path)
                job=next(paths.RESULTS.glob('autotune-*'))
                self.assertEqual(autotune.load_job(job)['status'],'interrupted')
                self.assertEqual(autotune.load_job(job)['outcome'],'INCOMPLETE')
            finally:
                paths.RESULTS=old_results; autotune.owned_directory=old_owned; autotune.execute=old_execute

    def test_live_status_reads_incremental_research_runner_progress(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as tmp:
            job=Path(tmp); stages=job/'stages'; stages.mkdir()
            state=dict(schema=1,status='running',outcome='INCOMPLETE',algorithm='grid',
                started_unix_ns=1,finished_unix_ns=None,selected_pct=None,
                resource_usage={},error=None)
            (job/'job.json').write_text(json.dumps(state))
            stage=stages/'autotune-search-test'; stage.mkdir()
            source=manifest()
            source['modes']=['static20','static30']; source['gates']['baseline']='static30'
            summary=dict(status='running',manifest=source,block_seeds=list(range(1,11)),runs=[
                dict(mode='static30',repetition=0,valid=True),dict(mode='static20',repetition=0,valid=False)])
            (stage/'summary.json').write_text(json.dumps(summary))
            progress=autotune.status(job)['active_stages'][0]
            self.assertEqual(progress['completed_runs'],2)
            self.assertEqual(progress['expected_runs'],20)
            self.assertEqual(progress['valid_runs'],1)
            self.assertEqual(progress['invalid_runs'],1)

    def test_dynamic_static_candidate_manifest_is_validated(self):
        data=manifest()
        data['modes']=['static21','static30','static39']
        data['gates']['baseline']='static30'
        self.assertEqual(validate(data)['modes'],['static21','static30','static39'])

    def test_candidate_with_hard_failure_guard_is_not_selected(self):
        self.assertEqual(autotune._select({30:{'valid':True,'median_objective_improvement':0,
            'hard_constraints':{'max_failure_ratio':{'status':'fail'}}}},30),30)


if __name__=='__main__':
    unittest.main()
