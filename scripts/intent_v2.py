"""Version 2 process-bound leases, request ledgers and bounded notifications."""
import os
import re
import socket
import time
from collections import Counter, deque
from pathlib import Path
from service_config import keys, number


def identity(pid):
    with open(f'/proc/{pid}/stat') as stream:
        start = stream.read().rsplit(')',1)[1].split()[19]
    try:
        cgroup = Path(f'/proc/{pid}/cgroup').read_text()[:4096]
    except OSError:
        cgroup = None
    return start,cgroup


class IntentAPI:
    def __init__(self, service):
        self.service=service
        self.leases={}
        self.revoked=set()
        self.audit=deque(maxlen=128)

    def expire(self):
        now=time.monotonic_ns()
        for token,lease in list(self.leases.items()):
            try:
                alive=identity(lease['pid'])[0]==lease['process_start']
            except OSError:
                alive=False
            if now>=lease['expires_ns'] or not alive or lease['name'] not in self.service.controller.registry.rows:
                self.release(token,'expired_or_revoked')
        for lease in self.leases.values():
            for request,entry in list(lease['requests'].items()):
                if now-entry[0]>600*10**9:
                    del lease['requests'][request]
                    lease['expired_requests']+=1

    def release(self,token,reason):
        lease=self.leases.pop(token)
        registry=self.service.controller.registry
        if lease['name'] in registry.rows:
            row=registry.rows[lease['name']]
            if token in row['instances']:
                registry.remove(lease['name'],lease['uid'],token)
        self.audit.append(dict(time_ns=time.monotonic_ns(),event=reason,uid=lease['uid'],service=lease['name']))
        self.service.log('intent_v2_'+reason,uid=lease['uid'],service=lease['name'])

    def revoke(self,name):
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',name):
            raise ValueError('invalid service name')
        if len(self.revoked)>=16 and name not in self.revoked:
            raise ValueError('revocation quota exhausted')
        self.revoked.add(name)
        for token,lease in list(self.leases.items()):
            if lease['name']==name:
                self.release(token,'revoked')
        return dict(revoked=True,service=name)

    def call(self,request,uid,pid,fd=None):
        self.expire()
        number(request.get('version'),2,2,True)
        action=request.get('action')
        if action=='register':
            keys(request,('op','version','action','intent','isolation'))
            if request['isolation'] not in ('shared','dedicated_preference'):
                raise ValueError('unsupported isolation preference')
            if len(self.leases)>=64:
                raise ValueError('v2 instance quota exhausted')
            value=request['intent']
            intent=self.service.controller.registry.validate(value)
            if intent['name'] in self.revoked:
                raise PermissionError('service revoked until operator restart')
            allowed=self.service.config.get('intent_api',{}).get('priorities',['standard'])
            if intent['priority'] not in allowed:
                raise PermissionError('priority not authorized by operator')
            if intent['persistent'] or intent['ttl_seconds']>3600:
                raise ValueError('v2 requires ephemeral leases of at most one hour')
            start,cgroup=identity(pid)
            registry=self.service.controller.registry
            old=getattr(registry,'shared_classes',False)
            if self.service.controller.mode=='off':
                registry.shared_classes=True
            try:
                registration=self.service.controller.register(intent,uid,pid)
            finally:
                registry.shared_classes=old
            token=registration['instance_token']
            self.leases[token]=dict(name=intent['name'],uid=uid,pid=pid,process_start=start,
                cgroup=cgroup,expires_ns=time.monotonic_ns()+intent['ttl_seconds']*10**9,
                requests={},completed=deque(maxlen=256),cookies=set(),notifications={},
                expired_requests=0,deadline_ms=intent['deadline_ms'],outcomes=Counter(),
                credits=4096,credit_ns=time.monotonic_ns())
            self.audit.append(dict(time_ns=time.monotonic_ns(),event='registered',uid=uid,service=intent['name']))
            self.service.log('intent_v2_registered',uid=uid,service=intent['name'])
            return dict(version=2,lease=token,isolation='preference_only',identity='SO_PEERCRED+process_start',
                        constraints='class contracts require admitted HTB lab',notifications={})
        token=request.get('lease')
        lease=self.leases.get(token) if isinstance(token,str) else None
        if not lease or (lease['uid'],lease['pid'])!=(uid,pid) or identity(pid)[0]!=lease['process_start']:
            raise PermissionError('unknown, revoked or foreign lease')
        if action=='status':
            keys(request,('op','version','action','lease'))
            return dict(version=2,inflight=len(lease['requests']),outcomes=dict(lease['outcomes']),
                        expired_requests=lease['expired_requests'],
                        socket_associations=len(lease['cookies']),pid=lease['pid'],
                        process_start=lease['process_start'],cgroup=lease['cgroup'],
                        expires_ns=lease['expires_ns'])
        if action=='close':
            keys(request,('op','version','action','lease'))
            self.release(token,'closed')
            return dict(version=2,closed=True)
        if action=='associate':
            keys(request,('op','version','action','lease'))
            if fd is None:
                raise ValueError('one socket descriptor required')
            if len(lease['cookies'])>=64:
                raise ValueError('socket association quota exhausted')
            copied=os.dup(fd)
            try:
                received=socket.socket(fileno=copied)
            except BaseException:
                os.close(copied)
                raise
            with received as sock:
                if sock.family not in (socket.AF_INET,socket.AF_INET6) or sock.type!=socket.SOCK_STREAM:
                    raise ValueError('association requires an Internet stream socket')
                cookie=int.from_bytes(sock.getsockopt(socket.SOL_SOCKET,57,8),os.sys.byteorder)
                local=sock.getsockname()
            lease['cookies'].add(cookie)
            return dict(version=2,cookie=str(cookie),association='received_socket_descriptor',
                        local=local,classification='none')
        if action!='batch':
            raise ValueError('unsupported intent API action')
        keys(request,('op','version','action','lease','events'))
        events=request['events']
        if not isinstance(events,list) or len(events)>32:
            raise ValueError('batch must contain at most 32 events')
        now=time.monotonic_ns()
        lease['credits']=min(4096,lease['credits']+(now-lease['credit_ns'])*2000/10**9)
        lease['credit_ns']=now
        if len(events)>lease['credits']:
            return dict(version=2,accepted=0,rejected=len(events),notifications={},reason='rate_limited')
        lease['credits']-=len(events)
        accepted=rejected=0
        for event in events:
            try:
                self.event(lease,event,uid)
                accepted+=1
            except (ValueError,KeyError,TypeError):
                rejected+=1
        view=self.service.observer.window(time.monotonic_ns(),2)
        app=view['application'].get(lease['name'],{}).get('application',{})
        registry=self.service.controller.registry
        objective=registry.rows[lease['name']]['intent']['target_p99_ms']
        notifications=dict(slo_risk=bool(app.get('p99_us') and app['p99_us']>objective*1000),
             congestion=bool(view['backlog'].get('p95_bytes')),
             policy_fallback=self.service.controller.state.name=='SAFE_FALLBACK',
             interface_degradation=any(not row.get('available',False) for row in
                 self.service.inventory.status().get('interfaces',[]) if row['name']!='lo'),
             insufficient_capacity=self.service.controller.timing.get('feasible') is False)
        lease['notifications']=notifications
        return dict(version=2,accepted=accepted,rejected=rejected,notifications=notifications,
                    expired_requests=lease['expired_requests'])

    def event(self,lease,event,uid):
        keys(event,('id','phase','start_ns','end_ns','deadline_us','cookie'))
        number(event['id'],1,2**63-1,True)
        number(event['start_ns'],1,time.monotonic_ns(),True)
        number(event['deadline_us'],0,600_000_000,True)
        if event['cookie'] is not None:
            number(event['cookie'],1,2**64-1,True)
            if event['cookie'] not in lease['cookies']:
                raise ValueError('socket cookie is not associated with this lease')
        number(event['end_ns'],0,time.monotonic_ns(),True)
        request=event['id']
        if event['phase']=='start':
            if request in lease['requests'] or request in lease['completed'] or len(lease['requests'])>=256 or event['end_ns']!=0:
                raise ValueError('duplicate request or inflight quota')
            lease['requests'][request]=(event['start_ns'],event['deadline_us'],event['cookie'])
            return
        if event['phase'] not in ('ok','timeout','failure','cancelled'):
            raise ValueError('invalid terminal outcome')
        entry=lease['requests'].get(request)
        if entry!=(event['start_ns'],event['deadline_us'],event['cookie']):
            raise ValueError('request identity or metadata mismatch')
        number(event['end_ns'],event['start_ns'],time.monotonic_ns(),True)
        duration=(event['end_ns']-event['start_ns'])/1000
        number(duration,0,600_000_000)
        self.service.observer.ingest(dict(op='request_sample',service=lease['name'],
            outcome='failure' if event['phase']=='cancelled' else event['phase'],
            duration_us=duration,completed_ns=event['end_ns'],
            deadline_ms=event['deadline_us']/1000 if event['deadline_us'] else lease['deadline_ms']),uid)
        del lease['requests'][request]
        lease['completed'].append(request)
        lease['outcomes'][event['phase']]+=1
