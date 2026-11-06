"""Real persistent/short TCP RPCs and UDP echo; all endpoints inside runner namespaces."""
import argparse
import json
import math
import socket
import socketserver
import struct
import threading
import time


def read(sock,size):
    chunks=bytearray()
    while len(chunks)<size:
        part=sock.recv(size-len(chunks))
        if not part:
            raise EOFError('connection closed')
        chunks.extend(part)
    return chunks


class RPC(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        self.request.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        try:
            while True:
                size=struct.unpack('!I',read(self.request,4))[0]
                if size>65536: return
                read(self.request,size)
                self.request.sendall(b'R'*64)
        except (OSError,EOFError):
            return


def server():
    services=[]
    for family,address in ((socket.AF_INET,'10.203.0.2'),(socket.AF_INET6,'fd42:203::2')):
        for port in (9000,9001,9002,9003):
            class Server(socketserver.ThreadingTCPServer):
                address_family=family
                daemon_threads=True
                allow_reuse_address=True
            service=Server((address,port),RPC)
            services.append(service)
            threading.Thread(target=service.serve_forever,daemon=True).start()
        for port in (9100,9101):
            sock=socket.socket(family,socket.SOCK_DGRAM)
            sock.bind((address,port))
            def echo(sock=sock):
                while True:
                    data,peer=sock.recvfrom(65536)
                    sock.sendto(data,peer)
            threading.Thread(target=echo,daemon=True).start()
    print('ready',flush=True)
    threading.Event().wait()


def client(args):
    started=time.monotonic()
    results=[]
    def worker(family,address,port,bulk=False,udp=False):
        latencies=[]; failures=[]; cookies=[]; offered=0; total=0
        sock=None
        end=started+args.duration
        while time.monotonic()<end:
            offered+=1
            begin=time.monotonic_ns()
            try:
                if sock is None:
                    sock=socket.socket(family,socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
                    sock.settimeout(1)
                    if not udp: sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                    sock.connect((address,port))
                    if len(cookies)<256:
                        cookies.append(int.from_bytes(sock.getsockopt(socket.SOL_SOCKET,57,8),'little'))
                payload=b'B'*(65536 if bulk else 256)
                if udp:
                    sock.send(payload)
                    if sock.recv(1024)!=payload: raise OSError('invalid UDP echo')
                else:
                    sock.sendall(struct.pack('!I',len(payload))+payload)
                    read(sock,64)
                total+=len(payload)
                if not bulk:
                    latencies.append((time.monotonic_ns()-begin)/1e6)
            except (OSError,EOFError) as error:
                failures.append(type(error).__name__)
                if sock: sock.close()
                sock=None
            if args.churn and not bulk and sock:
                sock.close(); sock=None
            if not bulk:
                time.sleep(.01)
        if sock: sock.close()
        results.append(dict(family=family,protocol=17 if udp else 6,port=port,bulk=bulk,
            offered=offered,completed=offered-len(failures),failures=failures,bytes=total,
            latencies_ms=latencies,cookies=cookies))
    threads=[]
    for family,address in ((socket.AF_INET,'10.203.0.2'),(socket.AF_INET6,'fd42:203::2')):
        specs=[(9000,False,False),(9002,False,False),(9003,False,False),(9100,False,True)]
        if args.bulk: specs.append((9001,True,False))
        for port,bulk,udp in specs:
            threads.append(threading.Thread(target=worker,args=(family,address,port,bulk,udp)))
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    latency=sorted(value for row in results for value in row['latencies_ms'])
    offered=sum(row['offered'] for row in results if not row['bulk'])
    failed=sum(len(row['failures']) for row in results if not row['bulk'])
    elapsed=time.monotonic()-started
    print(json.dumps(dict(duration=elapsed,offered=offered,failed=failed,
        completion_ratio=(offered-failed)/offered,
        background_failed=sum(len(row['failures']) for row in results if row['bulk']),
        deadline_attainment=sum(x<=25 for x in latency)/offered,
        p99_ms=latency[math.ceil(.99*len(latency))-1] if latency else None,
        total_mbps=sum(row['bytes'] for row in results)*8/elapsed/1e6,
        background_mbps=sum(row['bytes'] for row in results if row['bulk'])*8/elapsed/1e6,
        flows=results)),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('server','client'))
    parser.add_argument('--duration',type=float,default=2)
    parser.add_argument('--bulk',action='store_true')
    parser.add_argument('--churn',action='store_true')
    args=parser.parse_args()
    if not 0<args.duration<=30: parser.error('duration must be 0..30 seconds')
    server() if args.mode=='server' else client(args)
