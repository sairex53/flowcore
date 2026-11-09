"""Bounded connection pools shared by the application lab gateways and backend."""
from contextlib import contextmanager
import http.client
from queue import Empty, LifoQueue
import time
from urllib.parse import urlencode

MAX_RESPONSE = 16384


class Pool:
    def __init__(self, size, connect):
        if not 1 <= size <= 64:
            raise ValueError('pool size outside [1,64]')
        self.slots = LifoQueue(size)
        self.connect = connect
        for _ in range(size):
            self.slots.put_nowait(None)

    @contextmanager
    def checkout(self, timeout):
        started = time.monotonic_ns()
        try:
            connection = self.slots.get(timeout=timeout)
        except Empty as error:
            raise TimeoutError('connection pool exhausted') from error
        wait_ms = (time.monotonic_ns()-started)/1e6
        try:
            if connection is None:
                connection = self.connect()
            yield connection, wait_ms
        except BaseException:
            if connection is not None:
                connection.close()
                connection = None
            raise
        finally:
            self.slots.put_nowait(connection)

    def close(self):
        while True:
            try:
                connection = self.slots.get_nowait()
            except Empty:
                return
            if connection is not None:
                connection.close()


def backend_pool(port, size):
    return Pool(size, lambda: http.client.HTTPConnection('127.0.0.1', port, timeout=2))


def backend_request(pool, key, work_ms, response_bytes, deadline_ms):
    budget = min(2.0, deadline_ms / 1000) if deadline_ms else 2.0
    started = time.monotonic()
    with pool.checkout(budget) as (connection, pool_wait):
        remaining = budget - (time.monotonic()-started)
        if remaining <= 0:
            raise TimeoutError('gateway budget exhausted')
        connection.timeout = remaining
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        path = '/api/critical?' + urlencode(dict(id=key, work_ms=work_ms, response_bytes=response_bytes))
        connection.request('GET', path, headers={'X-Deadline-Ms': str(remaining*1000)})
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE+1)
        if len(data)>MAX_RESPONSE:
            raise ValueError('upstream response exceeds bound')
        metrics = response.getheader('Server-Timing', '')
        return response.status, data, f'gateway_pool;dur={pool_wait:.3f}' + (f', {metrics}' if metrics else '')
