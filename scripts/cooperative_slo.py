"""Bounded cooperative SLO budget, latency forecast and aggregate telemetry."""
import collections
import ctypes as c
from functools import cache
from pathlib import Path
import time

from durable_state import read_json
from fabric_protocol import fields, ident, integer
from paths import BINDIR

MAX_BUDGET_US = 10_000_000
STALE_US = 2_500_000
MAX_BYTES = 16_384
VERDICTS = ('fits', 'at_risk', 'insufficient', 'invalid', 'expired')


@cache
def native():
    library = c.CDLL(str(BINDIR/'libflowcore_intelligence.so'))
    library.flowcore_slo_receive_budget_v1.argtypes = (c.c_uint32, c.c_uint32)
    library.flowcore_slo_receive_budget_v1.restype = c.c_uint32
    library.flowcore_slo_forecast_v1.argtypes = (c.POINTER(c.c_uint32), c.POINTER(c.c_uint32),
        c.c_uint, c.c_uint32, c.POINTER(c.c_uint64))
    library.flowcore_slo_forecast_v1.restype = c.c_uint
    return library


def remaining_us(deadline_ns):
    return max(0, (deadline_ns-time.monotonic_ns())//1000)


def receive_budget(budget_us, challenge_ns, now_ns):
    integer(budget_us, 1, MAX_BUDGET_US)
    if now_ns < challenge_ns:
        return 0
    elapsed = min(MAX_BUDGET_US, (now_ns-challenge_ns+999)//1000)
    return native().flowcore_slo_receive_budget_v1(budget_us, elapsed)


def forecast(stages, remaining):
    integer(remaining, 0, MAX_BUDGET_US)
    count = len(stages)
    if not 1 <= count <= 32:
        return 'invalid', 0
    deps = (c.c_uint32*count)(*(integer(mask, 0, 2**32-1) for mask, _ in stages))
    high = (c.c_uint32*count)(*(2**32-1 if value is None else integer(value, 0, MAX_BUDGET_US)
                               for _, value in stages))
    out = c.c_uint64()
    verdict = native().flowcore_slo_forecast_v1(deps, high, count, remaining, c.byref(out))
    return VERDICTS[verdict], out.value


class History:
    """A small, fresh, success-only forecast input. Failures stay in the ledger."""
    def __init__(self):
        self.samples = collections.deque(maxlen=128)

    def record(self, duration_us, now_ns):
        if 0 <= duration_us <= MAX_BUDGET_US:
            self.samples.append((now_ns, duration_us))

    def high(self, now_ns):
        while self.samples and now_ns-self.samples[0][0] > STALE_US*1000:
            self.samples.popleft()
        if len(self.samples) < 8:
            return None
        samples = sorted(duration for _, duration in self.samples)
        return samples[(95*len(samples)+99)//100-1]


def aggregate(value):
    fields(value, ('service', 'age_us', 'offered', 'risk', 'misses', 'errors', 'deferred', 'p95_us'))
    ident(value['service'])
    integer(value['age_us'], 0, STALE_US)
    count = integer(value['offered'], 1, 10_000)
    for key in ('risk', 'misses', 'errors', 'deferred'):
        integer(value[key], 0, count)
    if value['p95_us'] is not None:
        integer(value['p95_us'], 0, MAX_BUDGET_US)
    return value


def read_aggregate(path, expected):
    value = read_json(Path(path), limit=2048)
    fields(value, ('sample_ns', 'summary'))
    stamp = integer(value['sample_ns'], 1, 2**63-1)
    now = time.monotonic_ns()
    if not 0 <= now-stamp <= STALE_US*1000:
        raise ValueError('stale cooperative application summary')
    summary = fields(value['summary'], ('service', 'offered', 'risk', 'misses', 'errors', 'deferred', 'p95_us'))
    row = aggregate(dict(summary, age_us=(now-stamp)//1000))
    if row['service'] != expected:
        raise ValueError('summary service differs from configured identity')
    return row
