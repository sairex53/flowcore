"""Small ctypes bridge to the supported C ABI. No independent protocol implementation."""
import contextlib
import ctypes as c


class Options(c.Structure):
    _fields_=[('version',c.c_uint32),('size',c.c_uint32),('service',c.c_char_p)]+[
        (name,c.c_uint32) for name in ('priority','target_us','deadline_us','desired_kbit',
                                     'minimum_kbit','maximum_kbit','ttl_seconds','reliable','isolated')]


class Statistics(c.Structure):
    _fields_=[(name,c.c_uint64) for name in ('queued','accepted','dropped','rejected','transport_errors')]


class TransportStatistics(c.Structure):
    _fields_=[(name,c.c_uint64) for name in ('session_connections','session_requests','legacy_requests')]


class Request:
    def __init__(self,library,handle):
        self.lib,self.handle=library,handle
        self.id=int(library.fc_v2_request_id(handle))

    def complete(self,outcome=0):
        if not isinstance(outcome,int) or isinstance(outcome,bool) or not 0<=outcome<=3:
            raise ValueError('outcome must be 0 OK, 1 timeout, 2 failure or 3 cancellation')
        if not self.handle:
            return False
        handle,self.handle=self.handle,None
        return bool(self.lib.fc_v2_finish(handle,outcome))


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
        self.lib.fc_v2_request_id.argtypes=[c.c_void_p]
        self.lib.fc_v2_request_id.restype=c.c_uint64
        self.lib.fc_v2_transport_stats.argtypes=[c.c_void_p,c.POINTER(TransportStatistics)]
        self.lib.fc_v2_transport_stats.restype=c.c_int
        self.lib.fc_v2_associate.argtypes=[c.c_void_p,c.c_int]
        self.lib.fc_v2_associate.restype=c.c_uint64
        self.lib.fc_v2_dissociate.argtypes=[c.c_void_p,c.c_uint64,c.POINTER(c.c_uint64)]
        self.lib.fc_v2_dissociate.restype=c.c_int
        for diagnostic in ('generation','notifications_time'):
            function=getattr(self.lib,'fc_v2_'+diagnostic)
            function.argtypes=[c.c_void_p]
            function.restype=c.c_uint64
        self.lib.fc_v2_last_error.argtypes=[c.c_void_p]
        self.lib.fc_v2_last_error.restype=c.c_uint32
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
        scope=Request(self.lib,request)
        try:
            yield scope
        except TimeoutError:
            scope.complete(1)
            raise
        except BaseException:
            scope.complete(3)
            raise
        else:
            scope.complete(0)

    def flush(self,timeout_ms=3000):
        return bool(self.lib.fc_v2_flush(self.handle,timeout_ms))

    def statistics(self):
        result=Statistics()
        if not self.lib.fc_v2_stats(self.handle,c.byref(result)):
            raise RuntimeError('statistics unavailable')
        return {name:getattr(result,name) for name,_ in result._fields_}

    def transport_statistics(self):
        result=TransportStatistics()
        if not self.lib.fc_v2_transport_stats(self.handle,c.byref(result)):
            raise RuntimeError('transport statistics unavailable')
        return {name:getattr(result,name) for name,_ in result._fields_}

    def generation(self):
        return int(self.lib.fc_v2_generation(self.handle))

    def last_error(self):
        return int(self.lib.fc_v2_last_error(self.handle))

    def notifications_time(self):
        return int(self.lib.fc_v2_notifications_time(self.handle))

    def associate_socket(self,fd):
        cookie=int(self.lib.fc_v2_associate(self.handle,fd))
        if not cookie:
            raise RuntimeError(self.lib.fc_v2_error().decode())
        return cookie

    def dissociate_socket(self,cookie):
        abandoned=c.c_uint64()
        if not self.lib.fc_v2_dissociate(self.handle,cookie,c.byref(abandoned)):
            raise RuntimeError(self.lib.fc_v2_error().decode())
        return int(abandoned.value)

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
