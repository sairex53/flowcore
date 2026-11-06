"""Opt-in TCX engine for a caller-owned ServiceLab; no host auto-attachment."""
import json
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
                    if not isinstance(row,dict) or row.get('type') not in ('ready','policy','flow','stats','error'):
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
                raise RuntimeError('flow engine failed; inspect flow-engine.stderr')
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

    def dump(self):
        self.records.clear()
        self.send('dump')
        self.wait(lambda:any(x['type']=='stats' and x.get('dump') is True for x in self.records))
        return [row for row in self.records if row['type']=='flow']

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
