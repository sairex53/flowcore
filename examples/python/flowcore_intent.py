"""Small ctypes bridge to the supported C ABI. No independent protocol implementation."""
import contextlib
import ctypes as c


class Options(c.Structure):
    _fields_=[('version',c.c_uint32),('size',c.c_uint32),('service',c.c_char_p)]+[
        (name,c.c_uint32) for name in ('priority','target_us','deadline_us','desired_kbit',
                                     'minimum_kbit','maximum_kbit','ttl_seconds','reliable','isolated')]


class Statistics(c.Structure):
    _fields_=[(name,c.c_uint64) for name in ('queued','accepted','dropped','rejected','transport_errors')]


class Service:
    def __init__(self,library,socket_path,name,priority=0):
        self.lib=c.CDLL(str(library))
        self.lib.fc_v2_open.argtypes=[c.c_char_p,c.POINTER(Options)]
        self.lib.fc_v2_open.restype=c.c_void_p
        for operation in ('wait','flush'):
            function=getattr(self.lib,'fc_v2_'+operation)
            function.argtypes=[c.c_void_p,c.c_uint32]
            function.restype=c.c_int
        self.lib.fc_v2_begin.argtypes=[c.c_void_p,c.c_uint32,c.c_uint64]
        self.lib.fc_v2_begin.restype=c.c_void_p
        self.lib.fc_v2_finish.argtypes=[c.c_void_p,c.c_uint32]
        self.lib.fc_v2_finish.restype=c.c_int
        self.lib.fc_v2_stats.argtypes=[c.c_void_p,c.POINTER(Statistics)]
        self.lib.fc_v2_stats.restype=c.c_int
        self.lib.fc_v2_notifications.argtypes=[c.c_void_p]
        self.lib.fc_v2_notifications.restype=c.c_uint32
        self.lib.fc_v2_close.argtypes=[c.c_void_p]
        self.lib.fc_v2_error.restype=c.c_char_p
        options=Options(2,c.sizeof(Options),name.encode(),priority,30000,25000,0,0,0,300,0,0)
        self.handle=self.lib.fc_v2_open(str(socket_path).encode(),c.byref(options))
        if not self.handle:
            raise ValueError(self.lib.fc_v2_error().decode())

    def ready(self,timeout_ms=3000):
        return bool(self.lib.fc_v2_wait(self.handle,timeout_ms))

    @contextlib.contextmanager
    def request(self,deadline_us=0,cookie=0):
        request=self.lib.fc_v2_begin(self.handle,deadline_us,cookie)
        if not request:
            raise RuntimeError('cannot create request')
        try:
            yield
        except BaseException:
            self.lib.fc_v2_finish(request,3)
            raise
        else:
            self.lib.fc_v2_finish(request,0)

    def flush(self,timeout_ms=3000):
        return bool(self.lib.fc_v2_flush(self.handle,timeout_ms))

    def statistics(self):
        result=Statistics()
        if not self.lib.fc_v2_stats(self.handle,c.byref(result)):
            raise RuntimeError('statistics unavailable')
        return {name:getattr(result,name) for name,_ in result._fields_}

    def notifications(self):
        return self.lib.fc_v2_notifications(self.handle)

    def close(self):
        if self.handle:
            self.lib.fc_v2_close(self.handle)
            self.handle=None

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()
