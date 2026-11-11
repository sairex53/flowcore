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


class CapabilitySnapshot(c.Structure):
    _fields_=[('version',c.c_uint32),('size',c.c_uint32),('protocol_version',c.c_uint32),
              ('reserved',c.c_uint32),('features',c.c_uint64),('udp_message_authorized',c.c_uint32),
              ('reserved2',c.c_uint32)]


class PolicySnapshot(c.Structure):
    _fields_=[('version',c.c_uint32),('size',c.c_uint32),('state',c.c_uint32),
              ('kernel_verified',c.c_uint32),('policy_generation',c.c_uint64),
              ('priority',c.c_uint32),('target_latency_us',c.c_uint32),('deadline_us',c.c_uint32),
              ('desired_kbit',c.c_uint32),('minimum_kbit',c.c_uint32),('maximum_kbit',c.c_uint32),
              ('lifetime_seconds',c.c_uint32),('prefer_reliable',c.c_uint32),
              ('class_rate_kbit',c.c_uint32),('class_ceil_kbit',c.c_uint32)]


class NotificationSnapshot(c.Structure):
    _fields_=[('version',c.c_uint32),('size',c.c_uint32),('flags',c.c_uint32),
              ('network_observed',c.c_uint32),('observed_ns',c.c_uint64),
              ('application_samples',c.c_uint64)]


class Request:
    def __init__(self,library,handle):
        self.lib,self.handle=library,handle
        self.id=int(library.fc_v2_request_id(handle))

    def cancel(self):
        if not self.handle:
            return False
        handle,self.handle=self.handle,None
        return bool(self.lib.fc_v2_cancel(handle))

    def complete(self,outcome=0):
        if not isinstance(outcome,int) or isinstance(outcome,bool) or not 0<=outcome<=3:
            raise ValueError('outcome must be 0 OK, 1 timeout, 2 failure or 3 cancellation')
        if not self.handle:
            return False
        handle,self.handle=self.handle,None
        return bool(self.lib.fc_v2_finish(handle,outcome))


class Service:
    def __init__(self, library, socket_path, name, priority=0, *,
                 target_latency_us=30000, deadline_us=25000, desired_kbit=0,
                 minimum_kbit=0, maximum_kbit=0, ttl_seconds=300):
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
        for function,output in (('fc_v2_get_capabilities',CapabilitySnapshot),
                                ('fc_v2_inspect_policy',PolicySnapshot),
                                ('fc_v2_get_notification_snapshot',NotificationSnapshot)):
            call=getattr(self.lib,function)
            call.argtypes=[c.c_void_p,c.POINTER(output)]
            call.restype=c.c_int
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
        values=(target_latency_us,deadline_us,desired_kbit,minimum_kbit,maximum_kbit,ttl_seconds)
        if any(type(value) is not int or value<0 or value>600000000 for value in values):
            raise ValueError('intent numeric field outside supported bounds')
        if (not 0 < target_latency_us <= 600000000 or not 1 <= ttl_seconds <= 3600
                or max(desired_kbit, minimum_kbit, maximum_kbit) > 1000000):
            raise ValueError('intent numeric field outside supported bounds')
        if maximum_kbit and (minimum_kbit>maximum_kbit or desired_kbit>maximum_kbit):
            raise ValueError('throughput constraints conflict')
        options=Options(2,c.sizeof(Options),name.encode(),priority,target_latency_us,deadline_us,
                        desired_kbit,minimum_kbit,maximum_kbit,ttl_seconds,0,0)
        self.handle=self.lib.fc_v2_open(str(socket_path).encode(),c.byref(options))
        if not self.handle:
            raise ValueError(self.lib.fc_v2_error().decode())

    def declare_transport(self,session,kind,multiplexed=False,mobility=False,cookie=0):
        function=self.lib.fc_v2_declare_transport
        function.argtypes=[c.c_void_p,c.c_char_p,c.c_uint32,c.c_uint32,c.c_uint32,c.c_uint64]
        function.restype=c.c_int
        kinds={'tcp':0,'mptcp':1,'udp':2,'quic':3,'logical':4}
        if kind not in kinds:
            raise ValueError('unsupported transport')
        if not function(self.handle,session.encode(),kinds[kind],int(multiplexed),int(mobility),cookie):
            raise RuntimeError(self.lib.fc_v2_error().decode())

    def report_transport_state(self,session,degraded,error='none'):
        function=self.lib.fc_v2_transport_state
        function.argtypes=[c.c_void_p,c.c_char_p,c.c_uint32,c.c_char_p]
        function.restype=c.c_int
        if not function(self.handle,session.encode(),int(degraded),error.encode()):
            raise RuntimeError(self.lib.fc_v2_error().decode())

    def ready(self,timeout_ms=3000):
        return bool(self.lib.fc_v2_wait(self.handle,timeout_ms))

    def subscribe_notifications(self, enabled=True):
        function=self.lib.fc_v2_subscribe_notifications
        function.argtypes=[c.c_void_p,c.c_uint32]
        function.restype=c.c_int
        if not function(self.handle,int(bool(enabled))):
            raise RuntimeError(self.lib.fc_v2_error().decode())

    def capabilities(self):
        result=CapabilitySnapshot(1,c.sizeof(CapabilitySnapshot))
        if not self.lib.fc_v2_get_capabilities(self.handle,c.byref(result)):
            raise RuntimeError(self.lib.fc_v2_error().decode())
        return {'protocol_version':result.protocol_version,'features':int(result.features),
                'udp_message_authorized':bool(result.udp_message_authorized)}

    def effective_policy(self):
        result=PolicySnapshot(1,c.sizeof(PolicySnapshot))
        if not self.lib.fc_v2_inspect_policy(self.handle,c.byref(result)):
            raise RuntimeError(self.lib.fc_v2_error().decode())
        return {'generation':result.policy_generation,'state':result.state,
                'kernel_verified':bool(result.kernel_verified),'priority':result.priority,
                'target_latency_us':result.target_latency_us,'deadline_us':result.deadline_us,
                'desired_kbit':result.desired_kbit,'minimum_kbit':result.minimum_kbit,
                'maximum_kbit':result.maximum_kbit,'lifetime_seconds':result.lifetime_seconds,
                'prefer_reliable':bool(result.prefer_reliable),'class_rate_kbit':result.class_rate_kbit,
                'class_ceil_kbit':result.class_ceil_kbit}

    def notification_snapshot(self):
        result=NotificationSnapshot(1,c.sizeof(NotificationSnapshot))
        if not self.lib.fc_v2_get_notification_snapshot(self.handle,c.byref(result)):
            raise RuntimeError(self.lib.fc_v2_error().decode())
        return {'flags':result.flags,'network_observed':bool(result.network_observed),
                'observed_ns':result.observed_ns,'application_samples':result.application_samples}

    @contextlib.contextmanager
    def stream_request(self,stream_id,priority=0,deadline_us=0,cookie=0):
        function=self.lib.fc_v2_begin_stream
        function.argtypes=[c.c_void_p,c.c_uint64,c.c_uint32,c.c_uint32,c.c_uint64]
        function.restype=c.c_void_p
        handle=function(self.handle,stream_id,priority,deadline_us,cookie)
        if not handle:
            raise RuntimeError(self.lib.fc_v2_error().decode())
        scope=Request(self.lib,handle)
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
