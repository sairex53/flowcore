"""Bounded nonblocking V2-only sessions. Legacy stream control stays independent."""
import os
import socket
import stat
import struct
import time
from ipc_security import decode
from intent_v2 import identity

LIMIT=65536
MAX_CLIENTS=16


class Sessions:
    def __init__(self,service,path, *, listener=None, allowed=None):
        self.service=service
        self.path=path
        self.clients={}
        self.serial=0
        self.allowed = allowed if allowed is not None else lambda uid: uid in (0, os.geteuid())
        if listener is not None:
            self.server, self.inode = listener, None
            return
        self.server=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET|socket.SOCK_NONBLOCK)
        try:
            try:
                old=path.lstat()
            except FileNotFoundError:
                old=None
            if old:
                if not stat.S_ISSOCK(old.st_mode) or old.st_uid!=os.geteuid():
                    raise PermissionError('intent endpoint is not an owned socket')
                path.unlink()
            self.server.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
            self.server.bind(str(path))
            self.inode=path.lstat().st_ino
            self.server.listen(MAX_CLIENTS)
        except BaseException:
            self.server.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self,*_):
        for client in list(self.clients):
            self.drop(client)
        self.server.close()
        if self.inode is None:
            return
        try:
            current=self.path.lstat()
            if stat.S_ISSOCK(current.st_mode) and current.st_ino==self.inode:
                self.path.unlink()
        except FileNotFoundError:
            pass

    def drop(self,client):
        self.clients.pop(client,None)
        client.close()

    def inputs(self):
        return [self.server,*self.clients]

    def poll(self,ready):
        if self.server in ready:
            try:
                client,_=self.server.accept()
                client.setblocking(False)
                peer=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if len(self.clients)>=MAX_CLIENTS or not self.allowed(peer[1]):
                    client.close()
                else:
                    try:
                        birth=identity(peer[0])[0]
                    except OSError:
                        client.close()
                    else:
                        self.serial+=1
                        self.clients[client]=dict(peer=peer,birth=birth,last=time.monotonic(),
                                                  credits=2048.0,credit_time=time.monotonic(),id=self.serial)
            except BlockingIOError:
                pass
        handled=0
        for client,state in list(self.clients.items()):
            if time.monotonic()-state['last']>30:
                self.drop(client)
                continue
            if client not in ready or handled>=8:
                continue
            handled+=1
            self.frame(client,state)
            if client in self.clients:
                self.clients.pop(client)
                self.clients[client]=state

    def frame(self,client,state):
        descriptors=[]
        disconnect=False
        try:
            data,ancillary,flags,_=client.recvmsg(LIMIT+1,socket.CMSG_SPACE(12)+socket.CMSG_SPACE(32),
                                                 socket.MSG_DONTWAIT|socket.MSG_CMSG_CLOEXEC)
            credential=None
            for level,kind,payload in ancillary:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:
                    descriptors.extend(value[0] for value in struct.iter_unpack('=i',payload[:len(payload)//4*4]))
                if level==socket.SOL_SOCKET and kind==socket.SCM_CREDENTIALS and len(payload)==12:
                    credential=struct.unpack('3i',payload)
            if not data:
                self.drop(client)
                return
            if credential!=state['peer'] or identity(state['peer'][0])[0]!=state['birth']:
                disconnect=True
                raise PermissionError('session sender differs from authenticated process')
            if flags&(socket.MSG_TRUNC|socket.MSG_CTRUNC) or len(data)>LIMIT or len(descriptors)>1:
                disconnect=True
                raise ValueError('intent frame or descriptor limit exceeded')
            now=time.monotonic()
            state['credits']=min(2048,state['credits']+(now-state['credit_time'])*2048)
            state['credit_time']=state['last']=now
            if state['credits']<1:
                disconnect=True
                raise ValueError('session request rate exceeded')
            state['credits']-=1
            request=decode(data)
            if request['op']!='intent_v2':
                raise PermissionError('session accepts only intent_v2')
            if descriptors and request.get('action')!='associate':
                raise ValueError('descriptors require association operation')
            result=dict(ok=True,result=self.service.command(request,state['peer'][1],state['peer'][0],
                                                           descriptors[0] if descriptors else None))
        except BlockingIOError:
            return
        except (OSError,ValueError,RuntimeError,TypeError,KeyError,OverflowError,RecursionError,struct.error) as error:
            code=('forbidden' if isinstance(error,PermissionError) else
                  'invalid_request' if isinstance(error,(ValueError,TypeError,KeyError,OverflowError,RecursionError))
                  else 'operation_failed')
            result=dict(ok=False,code=code,error=str(error),
                        retry_registration=bool(getattr(error,'retry_registration',False)))
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        try:
            import json
            response=json.dumps(result).encode()+b'\n'
            if len(response)>32768:
                response=b'{"ok": false, "code": "invalid_request", "error": "response bound exceeded"}\n'
            if client.send(response,socket.MSG_DONTWAIT|socket.MSG_NOSIGNAL)!=len(response):
                disconnect=True
        except OSError:
            disconnect=True
        if disconnect:
            self.drop(client)
