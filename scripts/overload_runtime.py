"""Typed Python adapter for the native cooperative overload advice engine."""
import ctypes as c
from functools import cache
import time

from paths import BINDIR

MISSING = 2**64-1
FEASIBILITY = ('FEASIBLE', 'AT_RISK', 'INFEASIBLE', 'INSUFFICIENT_EVIDENCE')
RECOMMENDATIONS = ('ACCEPT', 'ACCEPT_WITH_RISK', 'DEFER', 'REJECT_RECOMMENDED', 'UNKNOWN')
REASONS = ('fits', 'marginal', 'queue_pressure', 'hard_deadline_bound', 'expired',
           'missing_evidence', 'stale_evidence', 'invalid', 'capacity_collapsed',
           'recovering', 'fairness_floor_infeasible')
FAIRNESS = ('unknown', 'floor_feasible', 'floor_at_risk', 'floor_impossible', 'floor_observed')
STATES = ('NORMAL', 'APPROACHING_CAPACITY', 'OVERLOADED', 'CAPACITY_COLLAPSE',
          'RECOVERING', 'INSUFFICIENT_DATA')
ACTIONS = ('HOLD', 'REDUCE_CONCURRENCY', 'INCREASE_CONCURRENCY', 'NO_INTERVENTION')


@cache
def native():
    lib = c.CDLL(str(BINDIR/'libflowcore_intelligence.so'))
    U64P = c.POINTER(c.c_uint64)
    lib.flowcore_overload_assess_v1.argtypes = [U64P, U64P, c.c_uint64, U64P]
    lib.flowcore_overload_assess_v1.restype = c.c_int
    lib.flowcore_overload_controller_create_v1.argtypes = [c.c_uint32, c.c_uint32, c.c_uint32]
    lib.flowcore_overload_controller_create_v1.restype = c.c_void_p
    lib.flowcore_overload_controller_destroy_v1.argtypes = [c.c_void_p]
    lib.flowcore_overload_controller_observe_v1.argtypes = [c.c_void_p, U64P, c.c_uint64, U64P]
    lib.flowcore_overload_controller_observe_v1.restype = c.c_int
    return lib


def _encoded(value, *, missing=MISSING):
    if value is None:
        return missing
    if type(value) not in (int, bool) or int(value) < 0 or int(value) >= MISSING:
        raise ValueError('overload observation fields must be nonnegative integer values')
    return int(value)


def assess(work, evidence, now_ns=None):
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    work_fields = ('deadline_ns', 'request_bytes', 'response_bytes', 'processing_p50_us',
                   'processing_p95_us', 'processing_min_us', 'prefer_reliable',
                   'observed_request_rate_bytes_s', 'observed_response_rate_bytes_s')
    evidence_fields = ('observed_ns', 'available_bytes_s', 'response_available_bytes_s',
                       'backlog_bytes', 'response_backlog_bytes', 'rtt_p50_us', 'rtt_p95_us',
                       'active_concurrency', 'concurrency_limit', 'queue_depth',
                       'verified_max_bytes_s', 'verified_response_max_bytes_s',
                       'verified_min_rtt_us', 'scope_verified', 'capacity_bytes_s',
                       'background_floor_bytes_s', 'background_goodput_bytes_s',
                       'response_path_verified', 'operator_fairness_verified')
    w = (c.c_uint64*9)(*(_encoded(work.get(key)) for key in work_fields))
    e = (c.c_uint64*19)(*(_encoded(evidence.get(key)) for key in evidence_fields))
    out = (c.c_uint64*13)()
    status = native().flowcore_overload_assess_v1(w, e, now_ns, out)
    if status:
        raise ValueError('invalid native overload assessment input')
    def optional(index):
        return None if out[index] == MISSING else out[index]
    return dict(feasibility=FEASIBILITY[out[0]], recommendation=RECOMMENDATIONS[out[1]],
                reason=REASONS[out[2]], fairness=FAIRNESS[out[3]], remaining_us=out[4],
                hard_minimum_us=optional(5), predicted_p50_us=optional(6),
                predicted_p95_us=optional(7), uncertainty_us=optional(8),
                queue_wait_p50_us=optional(9), queue_wait_p95_us=optional(10),
                queue_pressure=bool(out[11]), stale_evidence=bool(out[12]))


class Backpressure:
    def __init__(self, minimum_concurrency, maximum_concurrency, maximum_admissions_per_second):
        self._ptr = native().flowcore_overload_controller_create_v1(
            minimum_concurrency, maximum_concurrency, maximum_admissions_per_second)
        if not self._ptr:
            raise ValueError('invalid backpressure controller configuration')

    def close(self):
        if self._ptr:
            native().flowcore_overload_controller_destroy_v1(self._ptr)
            self._ptr = None

    def __del__(self):
        self.close()

    def observe(self, sample, now_ns=None):
        if not self._ptr:
            raise RuntimeError('backpressure controller is closed')
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        fields = ('sampled_ns', 'capacity_bytes_s', 'delivered_bytes_s', 'backlog_bytes',
                  'active_concurrency', 'queue_depth', 'offered_requests', 'admitted_requests',
                  'rejected_requests', 'completed_requests', 'failed_requests', 'deadline_misses',
                  'background_goodput_bytes_s', 'background_floor_bytes_s', 'scope_verified',
                  'counters_complete', 'operator_fairness_verified')
        encoded = []
        for key in fields:
            try:
                encoded.append(_encoded(sample.get(key)))
            except ValueError as error:
                raise ValueError(f'invalid overload observation field {key}') from error
        values = (c.c_uint64*17)(*encoded)
        out = (c.c_uint64*7)()
        status = native().flowcore_overload_controller_observe_v1(self._ptr, values, now_ns, out)
        if status:
            raise ValueError('invalid native backpressure observation')
        return dict(state=STATES[out[0]], action=ACTIONS[out[1]], fairness=FAIRNESS[out[2]],
                    recommended_concurrency=out[3], available_admission_slots=out[4],
                    admission_budget_per_second=out[5], changed=bool(out[6]))
