"""Version 2 process-bound leases, request ledgers and bounded notifications."""
import os
import re
import socket
import time
from collections import Counter, deque
from service_config import keys, number


def identity(pid):
    with open(f'/proc/{pid}/stat','rb') as stream:
        raw=stream.read(4096)
    try:
        birth=raw.rsplit(b')',1)[1].split()[19]
        if not birth.isdigit():
            raise ValueError('invalid start ticks')
        start=birth.decode('ascii')
    except (IndexError,ValueError) as error:
        raise OSError('process start identity unavailable') from error
    try:
        with open(f'/proc/{pid}/cgroup',encoding='utf-8',errors='replace') as stream:
            cgroup=stream.read(4096)
    except OSError:
        cgroup=None
    return start,cgroup


class UnknownLease(PermissionError):
    retry_registration=True


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
        self.audit.append(dict(time_ns=time.monotonic_ns(),event=reason,uid=lease['uid'],service=lease['name'],
                               pid=lease['pid'],process_start=lease['process_start']))
        self.service.log('intent_v2_'+reason,uid=lease['uid'],service=lease['name'],
                         pid=lease['pid'],process_start=lease['process_start'])

    def revoke(self,name,actor_uid=None,actor_pid=None):
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',name):
            raise ValueError('invalid service name')
        if len(self.revoked)>=16 and name not in self.revoked:
            raise ValueError('revocation quota exhausted')
        self.revoked.add(name)
        actor=os.geteuid() if actor_uid is None else actor_uid
        self.audit.append(dict(time_ns=time.monotonic_ns(),event='revocation',uid=actor,pid=actor_pid,service=name))
        self.service.log('intent_v2_revocation',uid=actor,pid=actor_pid,service=name)
        for token,lease in list(self.leases.items()):
            if lease['name']==name:
                self.release(token,'revoked')
        return dict(revoked=True,service=name)

    def call(self,request,uid,pid,fd=None):
        self.expire()
        number(request.get('version'),2,2,True)
        try:
            shared_clock=(os.stat(f'/proc/{pid}/ns/time').st_ino==os.stat('/proc/self/ns/time').st_ino)
        except OSError:
            shared_clock=False
        if not shared_clock:
            raise PermissionError('shared monotonic time namespace could not be verified')
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
                cgroup=cgroup,ttl_seconds=intent['ttl_seconds'],expires_ns=time.monotonic_ns()+intent['ttl_seconds']*10**9,
                requests={},completed=deque(maxlen=256),cookies=set(),notifications={},
                expired_requests=0,abandoned_requests=0,deadline_ms=intent['deadline_ms'],outcomes=Counter(),
                credits=4096,credit_ns=time.monotonic_ns())
            row=registry.rows[intent['name']]
            row['monotonic_expiry']=max(row['monotonic_expiry'],self.leases[token]['expires_ns']/10**9)
            row['expires']=time.time()+max(0,row['monotonic_expiry']-time.monotonic())
            self.audit.append(dict(time_ns=time.monotonic_ns(),event='registered',uid=uid,service=intent['name'],pid=pid,process_start=start))
            self.service.log('intent_v2_registered',uid=uid,service=intent['name'],pid=pid,process_start=start)
            return dict(version=2,lease=token,renewable=True,
                        features=['lease_renewal','udp_socket_association','socket_dissociation','notification_quality'],
                        isolation='preference_only',identity='SO_PEERCRED+process_start',
                        constraints='class contracts require admitted HTB lab',notifications={})
        token=request.get('lease')
        lease=self.leases.get(token) if isinstance(token,str) else None
        if not lease or (lease['uid'],lease['pid'])!=(uid,pid) or identity(pid)[0]!=lease['process_start']:
            raise UnknownLease('unknown, revoked or foreign lease')
        if action=='renew':
            keys(request,('op','version','action','lease'))
            lease['expires_ns']=time.monotonic_ns()+lease['ttl_seconds']*10**9
            row=self.service.controller.registry.rows[lease['name']]
            row['monotonic_expiry']=max(row['monotonic_expiry'],lease['expires_ns']/10**9)
            row['expires']=time.time()+max(0,row['monotonic_expiry']-time.monotonic())
            self.audit.append(dict(time_ns=time.monotonic_ns(),event='renewed',uid=uid,service=lease['name'],pid=pid,process_start=lease['process_start']))
            self.service.log('intent_v2_renewed',uid=uid,service=lease['name'],pid=pid,process_start=lease['process_start'])
            return dict(version=2,expires_ns=lease['expires_ns'])
        if action=='status':
            keys(request,('op','version','action','lease'))
            return dict(version=2,inflight=len(lease['requests']),outcomes=dict(lease['outcomes']),
                        expired_requests=lease['expired_requests'],abandoned_requests=lease['abandoned_requests'],
                        socket_associations=len(lease['cookies']),pid=lease['pid'],
                        process_start=lease['process_start'],cgroup=lease['cgroup'],
                        expires_ns=lease['expires_ns'])
        if action=='close':
            keys(request,('op','version','action','lease'))
            self.release(token,'closed')
            return dict(version=2,closed=True)
        if action=='dissociate':
            keys(request,('op','version','action','lease','cookie'))
            cookie=request['cookie']
            number(cookie,1,2**64-1,True)
            abandoned=[identifier for identifier,entry in lease['requests'].items() if entry[2]==cookie]
            for identifier in abandoned:
                del lease['requests'][identifier]
                lease['completed'].append(identifier)
            lease['abandoned_requests']+=len(abandoned)
            lease['cookies'].discard(cookie)
            self.audit.append(dict(time_ns=time.monotonic_ns(),event='dissociated',uid=uid,
                                   pid=pid,service=lease['name'],cookie=cookie,abandoned=len(abandoned)))
            self.service.log('intent_v2_dissociated',uid=uid,pid=pid,service=lease['name'],
                             cookie=cookie,abandoned=len(abandoned))
            return dict(version=2,abandoned=len(abandoned))
        if action=='associate':
            keys(request,('op','version','action','lease'))
            if fd is None:
                raise ValueError('one socket descriptor required')
            copied=os.dup(fd)
            try:
                received=socket.socket(fileno=copied)
            except BaseException:
                os.close(copied)
                raise
            with received as sock:
                protocol=sock.getsockopt(socket.SOL_SOCKET,socket.SO_PROTOCOL)
                if sock.family not in (socket.AF_INET,socket.AF_INET6) or (sock.type,protocol) not in (
                        (socket.SOCK_STREAM,6),(socket.SOCK_DGRAM,17)):
                    raise ValueError('association requires an Internet TCP or UDP socket')
                cookie=int.from_bytes(sock.getsockopt(socket.SOL_SOCKET,57,8),os.sys.byteorder)
                local=sock.getsockname()
            if cookie not in lease['cookies'] and len(lease['cookies'])>=64:
                raise ValueError('socket association quota exhausted')
            lease['cookies'].add(cookie)
            return dict(version=2,cookie=str(cookie),protocol=protocol,association='received_socket_descriptor',
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
            return dict(version=2,accepted=0,rejected=len(events),notifications={},reason='rate_limited',
                        observed_ns=now,application_samples=0,network_observed=False)
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
        notifications=dict(slo_risk=bool((app.get('p99_us') and app['p99_us']>objective*1000) or
                              app.get('deadline_miss') or app.get('failure') or app.get('timeout')),
             congestion=bool(view['backlog'].get('p95_bytes')),
             policy_fallback=self.service.controller.state.name=='SAFE_FALLBACK',
             interface_degradation=any(not row.get('available',False) for row in
                 self.service.inventory.status().get('interfaces',[]) if row['name']!='lo'),
             insufficient_capacity=self.service.controller.timing.get('feasible') is False)
        lease['notifications']=notifications
        return dict(version=2,accepted=accepted,rejected=rejected,notifications=notifications,
                    observed_ns=time.monotonic_ns(),application_samples=app.get('samples',0),
                    network_observed=bool(view['backlog'].get('samples')),
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
