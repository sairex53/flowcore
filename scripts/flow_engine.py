"""Opt-in TCX engine for a caller-owned ServiceLab; no host auto-attachment."""
import json
import ipaddress
import os
import select
import subprocess
import time
from collections import deque
from paths import BINDIR, BPF_DATA, BPF_MARKER
from service_config import keys, number

CLASSES=(0x10010,0x10011,0x10012,0x10020)


def validate_rules(rules):
    if not isinstance(rules,list) or len(rules)>16:
        raise ValueError('at most 16 ordered rules required')
    result=[]
    for supplied in rules:
        if not isinstance(supplied,dict):
            raise ValueError('rule object required')
        rule=dict(dict(mark=0,cgroup=0,cookie=0),**supplied)
        keys(rule,('protocol','dport','service','classid','mark','cgroup','cookie'))
        for field,bounds in dict(protocol=(6,17),dport=(0,65535),service=(1,16),
                                 classid=(0,2**32-1),mark=(0,2**32-1),
                                 cgroup=(0,2**64-1),cookie=(0,2**64-1)).items():
            number(rule[field],*bounds,True)
        if rule['protocol'] not in (6,17) or rule['classid'] not in CLASSES:
            raise ValueError('unsupported protocol or HTB leaf')
        result.append(rule)
    return result


def validate_page(row,cursor,snapshot=None):
    if not isinstance(row,dict) or row.get('type')!='page' or type(row.get('protocol')) is not int or row.get('protocol')!=2:
        raise ValueError('unsupported flow page')
    for key,bounds in dict(snapshot=(1,2**64-1),session_ns=(1,2**64-1),
                           captured_ns=(1,2**64-1),cursor=(0,4096),total=(0,4096)).items():
        number(row.get(key),*bounds,True)
    if row['cursor']!=cursor or snapshot is not None and row['snapshot']!=snapshot:
        raise ValueError('flow page identity mismatch')
    flows=row.get('flows')
    if not isinstance(flows,list) or len(flows)>4 or cursor>row['total']:
        raise ValueError('invalid flow page length')
    end=cursor+len(flows)
    expected=min(cursor+4,row['total'])
    if row.get('next') is not None: number(row['next'],0,4096,True)
    if end!=expected or row.get('next')!=(end if end<row['total'] else None):
        raise ValueError('flow page gap or invalid continuation')
    for flow in flows:
        if not isinstance(flow,dict) or flow.get('type')!='flow' or flow.get('session_ns')!=row['session_ns']:
            raise ValueError('invalid flow observation')
        for key,bounds in dict(netns=(1,2**64-1),ifindex=(0,2**32-1),sport=(0,65535),dport=(0,65535),
                               first_ns=(1,2**64-1),last_ns=(1,2**64-1),vlan_depth=(0,2)).items():
            number(flow.get(key),*bounds,True)
        number(flow.get('family'),2,10,True)
        number(flow.get('protocol'),6,17,True)
        if flow.get('family') not in (2,10) or flow.get('protocol') not in (6,17):
            raise ValueError('invalid flow protocol/family')
        if flow['first_ns']>flow['last_ns'] or flow['last_ns']>row['captured_ns']:
            raise ValueError('invalid flow timestamp interval')
        for field in ('source','destination'):
            if ipaddress.ip_address(flow.get(field)).version!=(4 if flow['family']==2 else 6):
                raise ValueError('flow address family mismatch')
        cookie=flow.get('cookie')
        if not isinstance(cookie,str) or not cookie.isascii() or not cookie.isdigit() or len(cookie)>20 or int(cookie)>=2**64:
            raise ValueError('invalid socket cookie')
        for name,maximum in (('vlan_id',4095),('vlan_proto',65535)):
            values=flow.get(name)
            if not isinstance(values,list) or len(values)!=2:
                raise ValueError('invalid VLAN metadata')
            for value in values: number(value,0,maximum,True)
    return flows


class FlowEngine:
    def __init__(self):
        self.proc=None
        self.log=None
        self.reason='disabled; explicit owned lab required'
        self.pending=b''
        self.records=deque(maxlen=512)
        self.stats={}
        self.ready=False
        self.decode_lost=0
        self.lab=None
        self.last_snapshot=dict(complete=False)
        self.export_failures=0

    @staticmethod
    def validate_htb(lab):
        lab.validate()
        state=lab.counters()
        if (not any(row.get('root') and row.get('kind')=='htb' and row.get('handle')=='1:'
                    for row in state['qdisc']) or
                set(state['traffic_classes'])!={'critical','interactive','standard','background'}):
            raise RuntimeError('owned four-leaf HTB hierarchy required')

    def start(self,lab,capacity=4096,observe=False):
        self.close()
        self.last_snapshot=dict(complete=False)
        self.export_failures=0
        number(capacity,16,4096,True)
        if type(observe) is not bool:
            raise ValueError('observe must be boolean')
        binary=BINDIR/'flowcore-flow-engine'
        obj=BPF_DATA/'flow_engine.bpf.o'
        if (not binary.is_file() or not obj.is_file() or os.geteuid()!=0
                or not BPF_MARKER.is_file() or BPF_MARKER.read_text().strip()!='1'):
            self.reason='TCX artifacts or privileged isolated runner unavailable; userspace unchanged'
            return False
        try:
            self.validate_htb(lab)
        except (OSError,RuntimeError,subprocess.SubprocessError) as error:
            self.reason=str(error)
            return False
        self.lab=lab
        self.log=(lab.output/f'flow-engine-{time.monotonic_ns()}.stderr').open('w')
        self.pending=b''
        self.records.clear()
        self.stats={}
        self.decode_lost=0
        fd=lab.namespace_fds[0]
        try:
            self.proc=subprocess.Popen([binary,obj,str(fd),'fc0',str(capacity),*(['observe'] if observe else [])],
                pass_fds=(fd,),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log)
            os.set_blocking(self.proc.stdout.fileno(),False)
            os.set_blocking(self.proc.stdin.fileno(),False)
            self.wait(lambda:any(x.get('type')=='ready' for x in self.records))
            ready=next(row for row in self.records if row['type']=='ready')
            if ready.get('protocol')!=2:
                raise RuntimeError('flow helper protocol mismatch; userspace fallback required')
            self.ready=True
            self.reason='TCX attached only to owned fc0'
            return True
        except (OSError,RuntimeError,TimeoutError) as error:
            self.reason=str(error)
            self.close()
            return False

    def drain(self):
        if not self.proc:
            return
        for _ in range(32):
            try:
                block=os.read(self.proc.stdout.fileno(),16384)
            except BlockingIOError:
                break
            if not block:
                break
            self.pending+=block
            while b'\n' in self.pending:
                line,self.pending=self.pending.split(b'\n',1)
                try:
                    row=json.loads(line)
                    if not isinstance(row,dict) or row.get('type') not in ('ready','policy','flow','stats','page','error'):
                        raise ValueError('invalid engine record')
                    if len(self.records)==self.records.maxlen:
                        self.decode_lost+=1
                    self.records.append(row)
                    if row['type']=='stats':
                        self.stats=row
                except (ValueError,TypeError):
                    self.decode_lost+=1
            if len(self.pending)>4096:
                self.pending=b''
                self.decode_lost+=1
        if self.proc.poll() is not None:
            self.ready=False
            self.reason='engine exited; userspace policy unchanged'

    def wait(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.drain()
            if predicate():
                return
            if not self.proc or self.proc.poll() is not None:
                raise RuntimeError(f'flow engine failed; inspect {self.log.name if self.log else "collector log"}')
            select.select([self.proc.stdout],[],[],min(.05,max(0,deadline-time.monotonic())))
        raise TimeoutError('flow engine acknowledgement timeout')

    def set_policy(self,generation,rules):
        number(generation,2,2**64-1,True)
        rules=validate_rules(rules)
        if self.lab is None:
            raise RuntimeError('owned lab unavailable')
        try:
            self.validate_htb(self.lab)
        except (OSError,RuntimeError,subprocess.SubprocessError):
            self.close()
            raise RuntimeError('HTB ownership lost; engine detached') from None
        message=f'set {generation} {len(rules)}'
        for rule in rules:
            message+=' '+' '.join(str(rule[key]) for key in
                ('protocol','dport','service','classid','mark','cgroup','cookie'))
        self.records.clear()
        self.send(message)
        self.wait(lambda:any(row.get('generation')==generation and row['type']=='policy' or
                             row['type']=='error' for row in self.records))
        if any(row['type']=='error' for row in self.records):
            raise RuntimeError('policy rejected; previous generation remains active')

    def send(self,message):
        if not self.proc or not self.ready or '\n' in message or len(message)>4000:
            raise RuntimeError('flow engine unavailable or invalid command')
        payload=message.encode()+b'\n'
        if os.write(self.proc.stdin.fileno(),payload)!=len(payload):
            raise RuntimeError('command pipe backpressure')

    def dump(self,timeout=8):
        number(timeout,.1,8)
        self.last_snapshot=dict(complete=False)
        deadline=time.monotonic()+timeout
        result=[]
        identifier=None
        cursor=0
        total=None
        try:
            while True:
                command='dump' if identifier is None else f'page {identifier} {cursor}'
                self.records.clear()
                self.send(command)
                self.wait(lambda:any(row['type']=='page' and row.get('cursor')==cursor and
                                     (identifier is None or row.get('snapshot')==identifier)
                                     or row['type']=='error' for row in self.records),
                          min(1,max(.001,deadline-time.monotonic())))
                page=next((row for row in self.records if row['type']=='page' and
                           row.get('cursor')==cursor and (identifier is None or row.get('snapshot')==identifier)),None)
                if page is None:
                    raise ValueError('snapshot expired or invalid cursor')
                flows=validate_page(page,cursor,identifier)
                if identifier is None:
                    identifier,total=page['snapshot'],page['total']
                    self.last_snapshot.update(snapshot=identifier,total=total,session_ns=page['session_ns'],
                                              captured_ns=page['captured_ns'])
                elif (page['total']!=total or page['captured_ns']!=self.last_snapshot['captured_ns'] or
                      page['session_ns']!=self.last_snapshot['session_ns']):
                    raise ValueError('snapshot changed between pages')
                result.extend(flows)
                if page['next'] is None:
                    break
                cursor=page['next']
                if time.monotonic()>=deadline:
                    raise TimeoutError('flow snapshot overall deadline')
            identities={(row['netns'],row['ifindex'],row['cookie'],row['family'],row['protocol'],
                         row['source'],row['destination'],row['sport'],row['dport'],
                         row['vlan_depth'],tuple(row['vlan_id']),tuple(row['vlan_proto'])) for row in result}
            if len(result)!=total or len(identities)!=total:
                raise ValueError('incomplete or duplicate flow snapshot')
            self.last_snapshot.update(complete=True,received=len(result),
                                      atomic_across_flows=False)
            return result
        except (OSError,RuntimeError,TimeoutError,ValueError,KeyError,TypeError):
            self.export_failures+=1
            self.last_snapshot.update(received=len(result),complete=False)
            raise


    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.proc=None
        if self.log:
            self.log.close()
            self.log=None
        self.lab=None
        self.ready=False
