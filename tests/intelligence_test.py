"""Native feasibility, deterministic allocation and state-machine contracts."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from intelligence import Allocator, State, STATES
from intents import Registry
from observability import Observer
from observability_config import DEFAULT
import copy
from types import SimpleNamespace
from adaptive import Controller, DEFAULT as CONTROL_DEFAULT


class Intelligence(unittest.TestCase):
    def test_native_constraints_and_fair_selection(self):
        allocator = Allocator()
        current = {name:(rate,10000) for name,rate in
                   [('critical',3000),('interactive',2000),('standard',2000),('background',3000)]}
        maxima = dict(critical=4500,interactive=3500,standard=3500,background=3000)
        name, next_state = allocator.allocate(10000,current,maxima,2000,500,
                                             dict(critical=2,interactive=3))
        self.assertEqual(name,'interactive')
        self.assertEqual(next_state['interactive'][0],2500)
        self.assertEqual(next_state['background'][0],2500)
        self.assertEqual(sum(rate for rate,_ in next_state.values()),10000)
        with self.assertRaises(ValueError):
            allocator.allocate(9000,current,maxima,2000,500,{})
        with self.assertRaises(ValueError):
            allocator.allocate(10000,current,maxima,3500,500,{})
        self.assertIsNone(allocator.allocate(10000,current,maxima,3000,500,dict(critical=5)))
        self.assertIsNone(allocator.allocate(10000,current,maxima,2000,500,{}))

    def test_shared_services_are_explicit(self):
        registry = Registry(Observer(copy.deepcopy(DEFAULT)))
        registry.register({'name':'a'},0)
        with self.assertRaises(ValueError):
            registry.register({'name':'b'},0)
        registry.shared_classes = True
        registry.register({'name':'b'},0)
        self.assertEqual(set(registry.rows),{'a','b'})

    def test_multiservice_plan_and_infeasibility(self):
        observer = Observer(copy.deepcopy(DEFAULT))
        service = SimpleNamespace(observer=observer,profile={'bandwidth_kbit':10000},
             active='default',config={'adaptive':copy.deepcopy(CONTROL_DEFAULT),
             'policies':{'default':{'classes':{'critical':{'priority':0},'interactive':{'priority':1}}}}})
        controller = Controller(service)
        controller.strategy = 'hybrid'
        controller.registry.shared_classes = True
        controller.registry.register({'name':'a','priority':'critical'},0)
        controller.registry.register({'name':'b','priority':'interactive'},0)
        controller.base = dict(critical=(3000,10000),interactive=(2000,10000),
                               standard=(2000,10000),background=(3000,10000))
        controller.current = dict(controller.base)
        window = dict(queue={'root':{'utilization':.95},
                       'critical':dict(backlogged_samples=2,bytes_per_second=500000),
                       'interactive':dict(backlogged_samples=2,bytes_per_second=350000)},
                      backlog={'p95_bytes':10000},application={
                       name:{'application':dict(samples=200,p99_us=100000)} for name in ('a','b')})
        plan = controller.plan(window,controller.current)
        self.assertTrue(plan['feasible'])
        self.assertEqual(plan['action'],'transfer_reservation')
        self.assertEqual(plan['service'],'a')
        controller.registry.rows['a']['intent']['desired_kbit']=6000
        controller.registry.rows['b']['intent']['desired_kbit']=6000
        plan = controller.plan(window,controller.current)
        self.assertFalse(plan['feasible'])
        self.assertEqual(plan['action'],'hold')

    def test_legal_and_illegal_transitions(self):
        for source,targets in STATES.items():
            for target in STATES:
                state = State()
                state.name = source
                if target in targets or target == source:
                    state.move(target,'fixture')
                    self.assertEqual(state.name,target)
                else:
                    with self.assertRaises(RuntimeError):
                        state.move(target,'fixture')


if __name__ == '__main__':
    unittest.main()
