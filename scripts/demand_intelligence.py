"""Python adapter for FLOWCORE's native, evidence-bounded demand model."""
import ctypes
import os
from pathlib import Path

from paths import BINDIR


EVIDENCE = {
    'app_traffic': 1 << 0, 'app_counts': 1 << 1, 'delivered': 1 << 2,
    'root_queue': 1 << 3, 'capacity': 1 << 4, 'class': 1 << 5,
    'tcp': 1 << 6, 'tcp_limits': 1 << 7, 'socket_pacing': 1 << 8,
    'remote_pressure': 1 << 9, 'udp_accounting': 1 << 10,
    'configured_cap': 1 << 11, 'nominal_cap': 1 << 12,
    'app_incomplete': 1 << 13, 'class_drops': 1 << 14, 'root_drops': 1 << 15,
}
CLASSIFICATIONS = {
    0: 'UNKNOWN', 1: 'DEMAND_LIMITED', 2: 'LOCAL_CAPACITY_LIMITED',
    3: 'CLASS_RESTRICTED', 4: 'SOCKET_PACING_RESTRICTED',
    5: 'RECEIVER_LIMITED_SUSPECTED', 6: 'REMOTE_BOTTLENECK_SUSPECTED',
}
FLAGS = {
    1 << 0: 'QUEUE_ACCOUNTED_LOWER_BOUND', 1 << 1: 'SUSPECTED_DOUBLE_THROTTLING',
    1 << 2: 'CAPACITY_UNCERTAIN', 1 << 3: 'APPLICATION_REPORT_INCOMPLETE',
    1 << 4: 'RECEIVER_LIMITED_EVIDENCE', 1 << 5: 'REMOTE_PRESSURE_EVIDENCE',
    1 << 6: 'SENDER_BUFFER_LIMITED_EVIDENCE',
}
COUNTERS = {
    'requests_attempted', 'requests_admitted', 'requests_completed', 'requests_failed',
    'requests_rejected', 'deadline_misses', 'udp_messages_attempted',
    'udp_messages_transmitted', 'udp_send_failures', 'udp_urgent_messages',
    'udp_background_messages',
}
INPUT_FIELDS = (
    'app_offered_bytes', 'app_admitted_bytes', 'app_transmitted_bytes', 'app_received_bytes',
    'requests_attempted', 'requests_admitted', 'requests_completed', 'requests_failed',
    'requests_rejected', 'deadline_misses', 'udp_messages_attempted', 'udp_messages_transmitted',
    'udp_send_failures', 'udp_urgent_messages', 'udp_background_messages', 'delivered_bytes',
    'capacity_bps', 'configured_shaping_bps', 'observed_bps', 'root_backlog_before_bytes',
    'root_backlog_after_bytes', 'root_drops', 'class_rate_bps', 'class_ceil_bps',
    'class_served_bytes', 'class_backlog_before_bytes', 'class_backlog_after_bytes', 'class_drops',
    'socket_delivery_bps', 'socket_pacing_bps', 'socket_send_queue_before_bytes',
    'socket_send_queue_after_bytes', 'socket_notsent_bytes', 'socket_sndbuf_bytes',
    'tcp_sendbuf_limited_fraction', 'tcp_rwnd_limited_fraction', 'tcp_app_limited_fraction',
    'retransmit_ratio', 'rtt_us', 'min_rtt_us',
)
OUTPUT_FIELDS = (
    'offered_bps', 'admitted_bps', 'transmitted_bps', 'received_bps', 'delivered_bps',
    'queue_accounted_demand_bps', 'socket_demand_bps', 'selected_demand_bps',
    'demand_uncertainty_bps', 'socket_demand_uncertainty_bps', 'configured_shaping_bps',
    'observed_utilization', 'unused_capacity_bps', 'reclaimable_capacity_bps',
    'class_rate_bps', 'class_ceil_bps', 'class_headroom_bps', 'socket_queue_growth_bps',
    'failure_ratio', 'deadline_miss_ratio', 'requests_attempted', 'requests_admitted',
    'requests_completed', 'requests_failed', 'requests_rejected', 'deadline_misses',
    'udp_messages_attempted', 'udp_messages_transmitted', 'udp_send_failures',
    'udp_urgent_messages', 'udp_background_messages',
)


class Input(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('size', ctypes.c_uint32),
                ('evidence', ctypes.c_uint64), ('interval_seconds', ctypes.c_double)] + [
        (name, ctypes.c_uint64 if name in COUNTERS else ctypes.c_double) for name in INPUT_FIELDS
    ]


class Output(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('classification', ctypes.c_uint32),
                ('confidence_percent', ctypes.c_uint32), ('flags', ctypes.c_uint32),
                ('evidence_used', ctypes.c_uint64)] + [
        (name, ctypes.c_uint64 if name in COUNTERS else ctypes.c_double) for name in OUTPUT_FIELDS
    ]


class ForecastSample(ctypes.Structure):
    _fields_ = [('time_ns', ctypes.c_uint64), ('demand_bps', ctypes.c_double),
                ('capacity_bps', ctypes.c_double)]


class Forecast(ctypes.Structure):
    _fields_ = [(name, ctypes.c_double) for name in
                ('latest_bps', 'ema_bps', 'trend_bps', 'forecast_bps',
                 'last_value_baseline_bps', 'uncertainty_bps', 'burst_coefficient', 'confidence')]


class NativeDemand:
    def __init__(self, library=None):
        path = library or os.environ.get('FLOWCORE_DEMAND_LIBRARY')
        if path is None:
            path = Path(BINDIR) / 'libflowcore_intelligence.so'
        self.library = ctypes.CDLL(str(path))
        self.assess_fn = self.library.flowcore_demand_assess_v1
        self.assess_fn.argtypes = [ctypes.POINTER(Input), ctypes.POINTER(Output)]
        self.assess_fn.restype = ctypes.c_int
        self.forecast_fn = self.library.flowcore_demand_forecast_v1
        self.forecast_fn.argtypes = [ctypes.POINTER(ForecastSample), ctypes.c_uint,
                                     ctypes.c_double, ctypes.c_double, ctypes.POINTER(Forecast)]
        self.forecast_fn.restype = ctypes.c_int

    def assess(self, values):
        row = Input()
        row.version, row.size = 1, ctypes.sizeof(Input)
        row.evidence = sum(EVIDENCE[name] for name in values.get('evidence', ()))
        row.interval_seconds = values.get('interval_seconds', 1.0)
        for name in INPUT_FIELDS:
            setattr(row, name, values.get(name, 0))
        output = Output()
        if self.assess_fn(ctypes.byref(row), ctypes.byref(output)):
            raise ValueError('invalid native demand observation')
        result = {name: getattr(output, name) for name, _ in Output._fields_}
        result['classification'] = CLASSIFICATIONS[output.classification]
        result['confidence'] = output.confidence_percent / 100.0
        result['flags'] = [label for bit, label in FLAGS.items() if output.flags & bit]
        if 'app_counts' not in values.get('evidence', ()):
            for name in ('requests_attempted', 'requests_admitted', 'requests_completed',
                         'requests_failed', 'requests_rejected', 'deadline_misses',
                         'failure_ratio', 'deadline_miss_ratio'):
                result[name] = None
        if 'udp_accounting' not in values.get('evidence', ()):
            for name in ('udp_messages_attempted', 'udp_messages_transmitted', 'udp_send_failures',
                         'udp_urgent_messages', 'udp_background_messages'):
                result[name] = None
        for name in OUTPUT_FIELDS:
            if name not in COUNTERS and result[name] is not None and result[name] < 0.0:
                result[name] = None
        return result

    def forecast(self, rows, horizon_seconds=2.0, half_life_seconds=2.0):
        samples = (ForecastSample * len(rows))(*(
            ForecastSample(item['time_ns'], item['demand_bps'], item['capacity_bps']) for item in rows))
        output = Forecast()
        status = self.forecast_fn(samples, len(rows), horizon_seconds, half_life_seconds,
                                  ctypes.byref(output))
        if status < 0:
            raise ValueError('invalid native demand forecast samples')
        return None if status else {name: getattr(output, name) for name, _ in Forecast._fields_}
