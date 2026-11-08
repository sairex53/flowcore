"""Shared ephemeral PKI and process utilities for FLOWID experiments."""
import hashlib
import json
import os
import select
import subprocess
import time

SERVICE = 'payments.test'


def pki(path):
    def openssl(*args):
        return subprocess.run(['openssl', *args], cwd=path, check=True, capture_output=True, timeout=10).stdout
    openssl('req', '-x509', '-newkey', 'ed25519', '-nodes', '-days', '2', '-subj', '/CN=FLOWID ephemeral CA',
            '-keyout', 'ca.key', '-out', 'ca.pem', '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-addext', 'subjectKeyIdentifier=hash')
    for name in ('server', 'client', 'foreign'):
        extension = ('basicConstraints=critical,CA:FALSE\nauthorityKeyIdentifier=keyid:always\n' +
                     (f'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:{SERVICE}\n' if name == 'server'
                      else 'extendedKeyUsage=clientAuth\n'))
        (path/'extensions').write_text(extension)
        openssl('req', '-new', '-newkey', 'ed25519', '-nodes', '-subj', f'/CN={name}',
                '-keyout', name+'.key', '-out', name+'.csr')
        openssl('x509', '-req', '-in', name+'.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
                '-CAcreateserial', '-days', '2', '-extfile', 'extensions', '-out', name+'.pem')
    return hashlib.sha256(openssl('x509', '-in', 'client.pem', '-outform', 'DER')).hexdigest()


def settings(path, pin, address='127.0.0.1', transport='tcp', ttl=120):
    return {'service': SERVICE, 'listen': {'address': address, 'port': 0, 'transport': transport},
            'clients': [pin], 'ttl_seconds': ttl,
            'tls': {key: str(path/value) for key, value in
                    {'ca': 'ca.pem', 'certificate': 'server.pem', 'key': 'server.key'}.items()}}


def endpoint_file(path, port, address='127.0.0.1', transport='tcp', generation=1, service=SERVICE, expires=None):
    expiry = int(time.time())+600 if expires is None else expires
    temp = path.with_suffix('.next')
    temp.write_text(f'FLOWID-DIRECTORY/1 {service} {generation}\n{transport} {address} {port} {expiry}\n')
    temp.replace(path)


def client_args(binary, path, *, service=SERVICE, transport='tcp', timeout=600, identity='client'):
    return [str(binary), '--service', service, '--directory', str(path/'endpoints'),
            '--ca', str(path/'ca.pem'), '--cert', str(path/(identity+'.pem')),
            '--key', str(path/(identity+'.key')), '--transport', transport,
            '--timeout-ms', str(timeout), '--attempt-ms', '175']


def stop(proc, force=False):
    if proc.poll() is None:
        (proc.kill if force else proc.terminate)()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=4)


class Reader:
    def __init__(self, proc):
        self.proc = proc
        self.buffer = b''

    def get(self, timeout=5):
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic()+timeout
        while b'\n' not in self.buffer:
            if len(self.buffer) > 65536 or not select.select([fd], [], [], max(0, deadline-time.monotonic()))[0]:
                raise TimeoutError('expected FLOWID JSON event')
            chunk = os.read(fd, 4096)
            if not chunk:
                raise EOFError(f'FLOWID process exited ({self.proc.poll()})')
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def command(self, command, timeout=5):
        self.proc.stdin.write((command+'\n').encode())
        self.proc.stdin.flush()
        rows = []
        while len(rows) < 16:
            row = self.get(timeout)
            rows.append(row)
            if row['event'] != 'reconnected':
                return rows
        raise ValueError('too many reconnection events')
