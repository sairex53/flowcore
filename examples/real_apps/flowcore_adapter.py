"""Optional adapter to FLOWCORE's C ABI; application behavior survives daemon loss."""
import contextlib
import ctypes.util
import os
import socket
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'examples' / 'python'))


def default_library():
    configured = os.environ.get('FLOWCORE_INTENT_LIBRARY')
    if configured:
        return configured
    candidates = (ROOT / 'build' / 'libflowcore_intent.so',
                  ROOT / 'build' / 'lib' / 'libflowcore_intent.so')
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return ctypes.util.find_library('flowcore_intent')


class FlowcoreAdapter:
    """One bounded SDK channel per process; setup may block, request scopes do not."""

    def __init__(self, service, priority='critical', *, target_p99_ms=30,
                 deadline_ms=25, min_kbit=0, max_kbit=0, desired_kbit=0,
                 socket_path=None, library=None, enabled=True):
        self.service_name = service
        self._lock = threading.Lock()
        self._cookies = {}
        self.sdk = None
        self.reason = 'disabled'
        if not enabled:
            return
        library = library or default_library()
        if not library:
            self.reason = 'C ABI library unavailable'
            return
        try:
            from flowcore_intent import Service
            priorities = {'standard': 0, 'interactive': 1, 'critical': 2}
            if priority not in priorities:
                raise ValueError('unsupported priority')
            self.sdk = Service(library, socket_path or os.environ.get(
                'FLOWCORE_SOCKET', '/run/flowcore/flowcored.sock'), service,
                priorities[priority], target_latency_us=int(target_p99_ms * 1000),
                deadline_us=int(deadline_ms * 1000) if deadline_ms else 0,
                desired_kbit=desired_kbit, minimum_kbit=min_kbit,
                maximum_kbit=max_kbit)
            if not self.sdk.ready(3000):
                self.sdk.close()
                self.sdk = None
                self.reason = 'FLOWCORE daemon unavailable'
                return
            self.reason = 'connected'
        except (OSError, ValueError, RuntimeError) as error:
            self.sdk = None
            self.reason = str(error)[:160]

    def _cookie(self, sock):
        if self.sdk is None or sock is None:
            return 0
        fd = sock.fileno()
        if fd < 0:
            return 0
        try:
            key = int.from_bytes(sock.getsockopt(socket.SOL_SOCKET, 57, 8), sys.byteorder)
        except OSError:
            key = fd
        with self._lock:
            if key not in self._cookies:
                self._cookies[key] = self.sdk.associate_socket(fd)
            return self._cookies[key]

    @contextlib.contextmanager
    def request(self, *, sock=None, cookie=0, deadline_ms=0, stream_id=None, stream_priority=0):
        if self.sdk is None:
            yield None
            return
        try:
            cookie = cookie or self._cookie(sock)
        except (OSError, RuntimeError):
            # Flow correlation is optional. The application request still runs.
            cookie = 0
        try:
            scope = (self.sdk.stream_request(stream_id, stream_priority,
                                             int(deadline_ms * 1000), cookie)
                     if stream_id is not None else
                     self.sdk.request(int(deadline_ms * 1000), cookie))
        except (OSError, RuntimeError, ValueError):
            yield None
            return
        with scope as request:
            try:
                yield request
            except TimeoutError:
                if request is not None:
                    request.complete(1)
                raise
            except BaseException:
                if request is not None:
                    request.complete(2)
                raise

    def flush(self, timeout_ms=3000):
        return self.sdk.flush(timeout_ms) if self.sdk is not None else True

    def status(self):
        return {'enabled': self.sdk is not None, 'service': self.service_name,
                'connection': self.reason,
                'sdk': self.sdk.statistics() if self.sdk is not None else None}

    def close(self):
        if self.sdk is not None:
            self.sdk.flush(3000)
            self.sdk.close()
            self.sdk = None


class NullAdapter(FlowcoreAdapter):
    def __init__(self, service):
        super().__init__(service, enabled=False)
