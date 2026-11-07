"""Seeded optimization properties, an exhaustive oracle and failure injection."""
import os
from pathlib import Path
import random
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import adaptive_tc
from adaptive import objective
from daemon import Service
from policy_optimizer import ALGORITHMS, CHURN, Solver, plan
from research_runner import convergence
from service_config import CLASSES, load

NAMES = tuple(CLASSES)


def value(rows, capacity, solution):
    score = 0
    for name,row in rows.items():
        rate = solution[name]
        useful = max(0,min(rate,row['demand'])-row['floor'])
        score += row['utility']*useful*(2*capacity-useful)-CHURN*capacity*abs(rate-row['current'])
    return score


def oracle(rows, capacity, step, minimum):
    current = {name:row['current'] for name,row in rows.items()}
    candidates = [current]
    for name,row in rows.items():
        if not row['eligible']:
            continue
        for donor in (*NAMES,None):
            if donor == name:
                continue
            supply = current[donor]-rows[donor]['floor'] if donor else capacity-sum(current.values())
            limit = min(step,supply,row['demand']-current[name],row['limit']-current[name])
            for size in range(minimum,limit+1):
                candidate = dict(current)
                candidate[name] += size
                if donor:
                    candidate[donor] -= size
                candidates.append(candidate)
    return max(candidates,key=lambda item:value(rows,capacity,item))


class Optimizer(unittest.TestCase):
    def setUp(self):
        self.solver = Solver(os.environ.get('FLOWCORE_TEST_OPTIMIZER'))

    def test_exhaustive_neighborhood_and_feasibility(self):
        rng = random.Random(1985741)
        for _ in range(600):
            capacity = rng.randrange(100,1000)
            rows = {}
            for name in NAMES:
                floor = rng.randint(1,capacity//10)
                limit = rng.randint(floor,capacity)
                rows[name] = dict(floor=floor,limit=limit,demand=rng.randint(0,capacity),
                    current=min(limit,floor+rng.randint(0,capacity//10)),baseline=floor,
                    priority=rng.randint(1,100),utility=rng.randint(1,1000),eligible=rng.randrange(2))
            minimum = rng.randint(1,10)
            step = rng.randint(minimum,40)
            for algorithm in ALGORITHMS:
                actual, score = self.solver.solve(algorithm,capacity,step,minimum,rows)
                self.assertLessEqual(sum(actual.values()),capacity)
                for name,row in rows.items():
                    self.assertLessEqual(row['floor'],actual[name])
                    self.assertLessEqual(actual[name],row['limit'])
                self.assertEqual(score,value(rows,capacity,actual))
                self.assertEqual((actual,score),self.solver.solve(algorithm,capacity,step,minimum,rows))
                if algorithm == 'adaptive':
                    self.assertEqual(actual,oracle(rows,capacity,step,minimum))
                if algorithm == 'static':
                    self.assertEqual(actual,{name:rows[name]['baseline'] for name in NAMES})

    def test_weighted_fairness_demand_cap_and_numeric_bounds(self):
        rows = {name:dict(floor=10,limit=1000,demand=1000,current=10,baseline=10,
                          priority=weight,utility=200,eligible=1)
                for name,weight in zip(NAMES,(4,2,1,1))}
        a, _ = self.solver.solve('max-min',1000,50,1,rows)
        self.assertEqual(tuple(a.values()),(490,250,130,130))
        rows['critical']['demand'] = 50
        a, _ = self.solver.solve('max-min',1000,50,1,rows)
        self.assertEqual(tuple(a.values()),(50,470,240,240))
        a, _ = self.solver.solve('proportional',1000,50,1,rows)
        self.assertEqual(tuple(a.values()),(47,471,241,241))
        a, _ = self.solver.solve('max-min',1000000,50000,10000,rows)
        self.assertEqual(tuple(a.values()),(50,1000,1000,1000))
        full = {name:dict(row,limit=1000000,demand=1000000,utility=1000) for name,row in rows.items()}
        a, score = self.solver.solve('proportional',1000000,50000,10000,full)
        self.assertEqual(sum(a.values()),1000000)
        self.assertEqual(score,value(full,1000000,a))
        rows['background']['floor'] = 1001
        with self.assertRaisesRegex(ValueError,'hard floors'):
            self.solver.solve('adaptive',10000,100,10,rows)

    def test_conflict_expiry_stale_and_soft_overcommit(self):
        service = Service(load(ROOT/'config/service.json'))
        control = service.controller
        control.optimizer = self.solver
        control.registry.shared_classes = True
        current = dict(critical=(6000,20000),interactive=(4000,20000),standard=(4000,20000),background=(6000,20000))
        now = time.monotonic()
        def register(name, **kwargs):
            control.registry.register(dict(name=name,**kwargs),os.getuid(),os.getpid())
        register('one',min_kbit=4000,desired_kbit=17000)
        register('two',min_kbit=3000,desired_kbit=15000)
        queue = dict(root=dict(utilization=.99),critical=dict(backlogged_samples=5,bytes_per_second=1250000),
                     background=dict(backlogged_samples=4,bytes_per_second=600000))
        sample = dict(samples=140,p99_us=60000,ok=140)
        window = dict(queue=queue,backlog=dict(p95_bytes=64000),
                      application={name:dict(application=sample) for name in ('one','two')})
        failure = plan(control,window,current,objective,now=now)
        self.assertFalse(failure['feasible'])
        self.assertTrue(any(x['code']=='SERVICE_MINIMA_EXCEED_AUTHORIZED_RESERVATION'
                            for x in failure['optimizer']['conflicts']))
        self.assertIsNone(failure['optimizer']['static_fallback'])
        control.registry.rows['two']['intent']['min_kbit'] = 1000
        active = plan(control,window,current,objective,now=now)
        self.assertTrue(active['feasible'])
        with patch.object(self.solver,'solve',side_effect=OSError('unavailable')):
            fallback = plan(control,window,current,objective,now=now)
            self.assertEqual(fallback['action'],'hold')
            self.assertEqual(fallback['optimizer']['stage'],'SOLVER_UNAVAILABLE')
            self.assertEqual(fallback['optimizer']['static_fallback'],current)
        self.assertEqual(active['action'],'transfer_reservation')
        self.assertEqual(active['optimizer']['soft_target_overcommit_kbit'],12000)
        self.assertLess(active['proposed']['background'][0],6000)
        self.assertGreaterEqual(active['proposed']['background'][0],4000)
        stale = plan(control,window,current,objective,now=now,allow_adaptive=False)
        self.assertEqual(stale['action'],'hold')
        control.registry.rows['two']['monotonic_expiry'] = now-1
        expired = plan(control,window,current,objective,now=now)
        self.assertEqual(expired['optimizer']['excluded_requests'],[dict(service='two',reason='EXPIRED')])
        control.registry.rows['one']['owner_uid'] = -1
        denied = plan(control,window,current,objective,now=now)
        self.assertEqual(denied['optimizer']['conflicts'],[dict(code='UNAUTHORIZED_POLICY',service='one')])
        self.assertFalse(control.optimization_preview('adaptive')['feasible'])
        service.lab, service.active = object(),'fair'
        result = control.optimization_preview('adaptive')
        self.assertEqual(result['optimizer']['conflicts'],[dict(code='UNSUPPORTED_SCHEDULER',scheduler='cake')])

    def test_convergence_requires_two_healthy_unchanged_windows(self):
        plan = {'controller':dict(window_seconds=2,minimum_samples=100,target_p99_ms=25)}
        healthy = dict(application=dict(samples=120,p99_us=15000))
        def sample(seconds, rate, pending=None):
            return dict(time_ns=seconds*10**9,control_window={'critical':healthy},
                        state=dict(mode='active',allocation={'critical':(rate,8000)},pending=pending,error=None))
        samples = [sample(12,3000),sample(13,3000),sample(14,4000),
                   sample(16,4000,pending=True),sample(18,4000),sample(20,4000)]
        self.assertEqual(convergence(samples,[],[],plan,10,'optimizer'),10)
        self.assertIsNone(convergence(samples[:-1],[],[],plan,10,'optimizer'))

    def test_transaction_preflight_compensation_and_drift(self):
        current = {name:(25,100) for name in NAMES}
        desired = dict(current,critical=(30,100),background=(20,100))
        policy = dict(classes={name:dict(priority=idx) for idx,name in enumerate(NAMES)})

        class Kernel:
            def __init__(self):
                self.state = dict(current)
                self.calls = []
                self.fail = None

            def tc(self, *args):
                cls = next(name for name in NAMES if args[7]==f'1:{CLASSES[name]}')
                self.calls.append(cls)
                if self.fail == cls:
                    raise OSError('kernel rejected class update')
                self.state[cls] = (int(args[10][:-4]),int(args[12][:-4]))
                assert sum(v[0] for v in self.state.values()) <= 100

        kernel = Kernel()
        with patch.object(adaptive_tc,'read',side_effect=lambda *args: dict(kernel.state)):
            kernel.fail = 'critical'
            with self.assertRaisesRegex(RuntimeError,'failed and rolled back'):
                adaptive_tc.change(kernel,100,policy,current,desired)
            self.assertEqual(kernel.state,current)
            self.assertEqual(kernel.calls,['background','critical','background'])
            kernel.fail = None
            timing = adaptive_tc.change(kernel,100,policy,current,desired)
            self.assertEqual(timing['transaction'],'VERIFIED')
            self.assertEqual(kernel.state,desired)
            count = len(kernel.calls)
            with self.assertRaisesRegex(RuntimeError,'allocation drift'):
                adaptive_tc.change(kernel,100,policy,current,desired)
            with self.assertRaisesRegex(ValueError,'unsafe HTB'):
                adaptive_tc.change(kernel,100,policy,desired,dict(desired,critical=(40,100)))
            self.assertEqual(len(kernel.calls),count)


if __name__ == '__main__':
    unittest.main()
