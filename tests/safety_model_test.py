"""Run the finite spec and differential tests at FLOWCORE safety boundaries."""
import copy
import errno
import json
import os
from pathlib import Path
import random
import sys
import tempfile
from unittest.mock import Mock, patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import adaptive_tc
import durable_state
from daemon import Service
from multipath import add_owned_endpoint, remove_owned_endpoint, set_endpoint_backup
from network_helper import Actuator, Rejected
from operations import Operations
from intents import Registry
from platform_policy import EMPTY, PolicyStore, merged, validate
from safety_model import explore
from service_config import CLASSES, load

BASE = {cls:(25,100) for cls in CLASSES}
TARGET = dict(BASE,critical=(35,100),background=(15,100))
POLICY = {'classes':{cls:{'priority':i,'min_pct':1,'max_pct':100} for i,cls in enumerate(CLASSES)}}


class Kernel:
    """Replace only I/O. Test the real transaction code with independent oracles."""
    def __init__(self, initial, fault):
        self.actual = dict(initial)
        self.fault = fault
        self.reads = self.writes = 0
        self.mutations = []

    def assert_owned_sender(self):
        if self.fault=='unowned':
            raise RuntimeError('unowned')

    def read(self, *_):
        self.reads += 1
        if self.reads==2 and self.fault=='verify':
            raise OSError('readback temporarily unavailable')
        if self.reads==2 and self.fault=='foreign':
            self.actual['interactive']=(24,100)
        return dict(self.actual)

    def tc(self, *args):
        self.writes += 1
        if (self.writes==1 and self.fault=='first' or
                self.writes==2 and self.fault in ('second','rollback') or
                self.writes==3 and self.fault=='rollback'):
            raise OSError('injected kernel rejection')
        cls = next(cls for cls,i in CLASSES.items() if args[7]==f'1:{i}')
        self.actual[cls] = int(args[10][:-4]),int(args[12][:-4])
        assert sum(rate for rate,_ in self.actual.values()) <= 100
        self.mutations.append(cls)


class Endpoints:
    def __init__(self):
        self.rows = {}
        self.writes = 0
        self.namespace = (1,44)

    def namespace_identity(self,index):
        assert index==1
        return self.namespace

    def command(self,index,args):
        assert index==1
        if args[1]=='-j':
            return json.dumps(list(self.rows.values()))
        self.writes += 1
        action = args[3]
        identifier = int(args[args.index('id')+1])
        if action=='add':
            self.rows[identifier] = dict(id=identifier,address=args[4],dev=args[6],signal=True)
        elif action=='change':
            self.rows[identifier]['backup'] = args[-1]=='backup'
        elif action=='delete':
            del self.rows[identifier]


class HelperKernel:
    tag = b'flowcore:'+b'a'*32

    def __init__(self):
        self.indices = None
        self.queue = None
        self.writes = 0

    def create(self):
        self.indices = (3,4)
        self.writes += 1

    def set_queue(self,old,target):
        assert self.queue==old
        self.queue = target
        self.writes += 1

    def remove(self,limit):
        assert self.queue==limit
        self.indices = self.queue = None
        self.writes += 1


class Safety(unittest.TestCase):
    def test_finite_spec(self):
        result = explore()
        self.assertGreaterEqual(result['actions'],25)
        self.assertGreater(result['states'],100)
        print('finite specification',result)

    def test_transaction_outcomes_and_capacity_properties(self):
        rng = random.Random(193083)
        for _ in range(900):
            initial = dict(BASE)
            size = rng.randint(1,24)
            desired = dict(initial,critical=(25+size,100),background=(25-size,100))
            fault = rng.choice(('none','unowned','first','second','verify','rollback','foreign','invalid'))
            kernel = Kernel(initial,fault)
            if fault=='invalid':
                desired['background'] = (26,100)
            with patch.object(adaptive_tc,'read',side_effect=kernel.read):
                if fault=='none':
                    result = adaptive_tc.change(kernel,100,POLICY,initial,desired)
                    self.assertEqual(result['transaction'],'VERIFIED')
                    self.assertEqual(kernel.actual,desired)
                else:
                    with self.assertRaises((ValueError,RuntimeError)) as error:
                        adaptive_tc.change(kernel,100,POLICY,initial,desired)
                    self.assertEqual(getattr(error.exception,'compensated',False),fault in ('first','second','verify'))
                    if fault=='foreign':
                        self.assertEqual(kernel.actual,dict(desired,interactive=(24,100)))
                        self.assertEqual(kernel.writes,2)
                    elif fault=='rollback':
                        self.assertEqual(kernel.actual,dict(initial,background=desired['background']))
                    else:
                        self.assertEqual(kernel.actual,initial)
                    if fault in ('unowned','invalid'):
                        self.assertEqual(kernel.writes,0)

    def test_adversarial_activation_and_policy_payload(self):
        service = Service(load(ROOT/'config/service.json'))
        with self.assertRaises(PermissionError):
            service.command(dict(op='controller_start'),uid=os.geteuid()+1)
        service.monitor_only = True
        with self.assertRaises(PermissionError):
            service.command(dict(op='controller_start'),uid=os.geteuid())
        with self.assertRaises(PermissionError):
            service.controller.write(TARGET)
        oversized = {**EMPTY,'service':{f'profile-{i}':{} for i in range(13)}}
        with self.assertRaisesRegex(ValueError,'at most 12'):
            validate(oversized)
        self.assertEqual(service.controller.status()['mode'],'off')

    def test_controller_revokes_permission_after_rejected_write(self):
        service = Service(load(ROOT/'config/service.json'))
        ctl = service.controller
        service.lab = object()
        ctl.mode, ctl.pending, ctl.current = 'active',{'probe':True},dict(BASE)
        ctl.state.move('OBSERVING','test activation')
        ctl.allocation_confirmed = True
        with patch('adaptive_tc.change',side_effect=adaptive_tc.TransactionFailure('verify mismatch',compensated=False)) as io:
            with self.assertRaises(RuntimeError):
                ctl.write(TARGET)
            ctl.safe_tick()
            self.assertEqual(io.call_count,1)
        view = ctl.status()
        self.assertEqual((view['mode'],view['engine_state']),('off','SAFE_FALLBACK'))
        self.assertFalse(view['allocation_verified'])
        self.assertIsNone(view['pending'])
        self.assertIsNone(view['allocation'])

    def test_policy_cas_restart_precedence_and_persistence_faults(self):
        rng = random.Random(12086)
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as directory:
            path = Path(directory)
            store = PolicyStore()
            store.restore(path)
            revision, expected = 0,copy.deepcopy(EMPTY)
            for i in range(100):
                policy = copy.deepcopy(EMPTY)
                priorities = ('critical','interactive','standard')
                selectors = {'device':'veth','interface':'fc0','service':'app','container':'ctr','cgroup':'/app'}
                layers = ['global',*selectors]
                selected = {name:rng.choice(priorities) for name in layers if rng.randrange(2)}
                for name,value in selected.items():
                    if name=='global':
                        policy[name]['priority'] = value
                    else:
                        policy[name][selectors[name]] = {'priority':value}
                mode = rng.choice(('commit','restart','stale','pre_rename','post_rename','invalid'))
                if mode=='restart':
                    store = PolicyStore()
                    store.restore(path)
                else:
                    fault_at = 1 if mode=='pre_rename' else 2 if mode=='post_rename' else None
                    real_fsync = os.fsync
                    count = 0
                    def fsync(fd):
                        nonlocal count
                        count += 1
                        if count==fault_at:
                            raise OSError(errno.EIO,'injected fsync')
                        return real_fsync(fd)
                    if mode=='invalid':
                        policy['global'].update(min_kbit=10,max_kbit=5)
                    version = revision if mode!='stale' else revision+1
                    with patch.object(durable_state.os,'fsync',fsync):
                        try:
                            result = store.commit(policy,version,os.getuid())
                        except durable_state.DurabilityUncertain:
                            self.assertEqual(mode,'post_rename')
                            revision, expected = revision+1, policy
                        except OSError:
                            self.assertEqual(mode,'pre_rename')
                        except ValueError:
                            self.assertIn(mode,('stale','invalid'))
                        else:
                            self.assertNotIn(mode,('stale','invalid'))
                            if policy != expected:
                                self.assertIsNone(fault_at)
                                revision, expected = revision+1, policy
                            self.assertEqual(result['revision'],revision)
                self.assertEqual(store.state['revision'],revision)
                self.assertEqual(store.state['policy'],expected)
                restored = PolicyStore()
                restored.restore(path)
                self.assertEqual(restored.state,store.state)
                self.assertEqual(validate(json.loads(json.dumps(expected))),expected)
                ordered = [expected.get(scope,{}).get(selector,{}) for scope,selector in selectors.items()]
                entries = dict(zip(layers,[expected['global'],*ordered]))
                source = next((scope for scope in reversed(layers) if 'priority' in entries[scope]),'built-in')
                want = 'critical' if source=='built-in' else entries[source]['priority']
                self.assertEqual(merged(expected,**selectors)['fields']['priority'],want)
                self.assertEqual(merged(expected,**selectors)['provenance']['priority'],source)
            store.state['revision'] = 2**63-1
            with self.assertRaisesRegex(ValueError,'revision exhausted'):
                store.commit({**EMPTY,'global':{'deadline_ms':42}},2**63-1,os.getuid())

    def test_helper_role_epoch_and_revision_sequences(self):
        rng = random.Random(344901)
        net = HelperKernel()
        helper = Actuator(net,dict(operators=[21],readers=[22]))
        revision, exists, queue = 0,False,None
        with patch('network_helper.audit'), patch('network_helper.time.monotonic',side_effect=range(1,2001)):
            for _ in range(500):
                op = rng.choice(('lab.create','lab.remove','queue.set','queue.clear'))
                uid = rng.choice((0,21,22,99))
                epoch = rng.choice(('a'*32,'a'*32,'b'*32))
                sent = rng.choice((revision,revision,revision+1))
                target = rng.randint(1,4096) if op=='queue.set' else None
                expected = ('forbidden' if uid not in (0,21) else 'stale_revision' if epoch!='a'*32 or sent!=revision
                            else 'invalid_state' if ((op=='lab.create')==exists or (op=='queue.clear' and queue is None)) else None)
                cmd = dict(op=op,epoch=epoch,revision=sent,limit_packets=target)
                if expected:
                    with self.assertRaisesRegex(Rejected,expected):
                        helper.run(uid,cmd)
                else:
                    helper.run(uid,cmd)
                    revision += 1
                    exists = op!='lab.remove'
                    queue = target
                view = helper.view()
                self.assertEqual((view['revision'],view['lab_up'],view['limit_packets'],net.writes),
                                 (revision,exists,queue,revision))

    def test_service_owner_and_expiry_sequences(self):
        service = Service(load(ROOT/'config/service.json'))
        registry = Registry(service.observer)
        rng = random.Random(64893)
        model = {}
        now = 1000
        with patch('intents.time.monotonic',side_effect=lambda:now):
            for _ in range(140):
                name = rng.choice(('one','two','three'))
                uid = rng.choice((1001,1002))
                operation = rng.choice(('register','remove','advance'))
                registry.expire()
                model = {k:v for k,v in model.items() if now<v[1]}
                row = model.get(name)
                if operation=='register':
                    spec = dict(name=name,priority='standard',ttl_seconds=5)
                    registry.shared_classes = True
                    if row and row[0]!=uid:
                        with self.assertRaisesRegex(ValueError,'conflicting'):
                            registry.register(spec,uid)
                    else:
                        registry.register(spec,uid)
                        if not row:
                            model[name]=(uid,now+5)
                elif operation=='remove':
                    if row and row[0]==uid:
                        registry.remove(name,uid)
                        del model[name]
                    else:
                        with self.assertRaises(ValueError):
                            registry.remove(name,uid)
                else:
                    now += rng.randint(1,6)
                    registry.expire()
                    model = {k:v for k,v in model.items() if now<v[1]}
                self.assertEqual({k:(v['owner_uid'],v['monotonic_expiry']) for k,v in registry.rows.items()},model)

    def test_shutdown_attempts_all_releases_and_does_not_publish_false_clean(self):
        service = Service(load(ROOT/'config/service.json'))
        lab = service.lab = Mock()
        failed, remaining = Mock(),Mock()
        failed.close.side_effect = OSError('injected watcher cleanup failure')
        service.watches = [remaining,failed]
        service.controller.allocation_confirmed = True
        with self.assertRaisesRegex(OSError,'watcher cleanup'):
            service.close_lab()
        self.assertIsNone(service.lab)
        self.assertFalse(service.watches)
        self.assertFalse(service.controller.allocation_confirmed)
        lab.close.assert_called_once()
        lab.storage.cleanup.assert_called_once()
        remaining.close.assert_called_once()

        config = ROOT/'config/service.json'
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as directory:
            path = Path(directory)/'recovery.json'
            operations = Operations(service,path,config)
            operations.start()
            with patch.object(durable_state.os,'fsync',side_effect=OSError(errno.EIO,'injected shutdown write')):
                with self.assertRaises(OSError):
                    operations.finish()
            self.assertFalse(operations.checkpoint['clean_shutdown'])
            self.assertFalse(durable_state.read_json(path)['clean_shutdown'])

    def test_reused_mptcp_id_and_cleanup(self):
        net = Endpoints()
        lease = {}
        path = dict(id=3,server_address='10.3.0.1',server_interface='eth3')
        for mode in ('delete','change','missing','namespace','normal'):
            add_owned_endpoint(net,path,lease)
            if mode=='missing':
                net.rows.clear()
            elif mode=='namespace':
                net.namespace = (1,45)
            elif mode!='normal':
                net.rows[3]['address'] = '10.99.0.1'
            before = copy.deepcopy(net.rows)
            count = net.writes
            if mode=='normal':
                self.assertTrue(set_endpoint_backup(net,path,True,lease)['backup'])
                remove_owned_endpoint(net,3,lease)
            else:
                with self.assertRaisesRegex(RuntimeError,'ownership drift'):
                    (set_endpoint_backup(net,path,True,lease) if mode=='change'
                     else remove_owned_endpoint(net,3,lease))
                self.assertEqual(net.writes,count)
                self.assertEqual(net.rows,before)
            self.assertFalse(lease)
            net.rows.clear()


if __name__=='__main__':
    unittest.main()
