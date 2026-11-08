"""Research-only adapter between asyncio-owned sockets and the C++ pacing engine."""
import ctypes as c
import errno
import fcntl
import json
import os
import time

from durable_state import read_json
from deadline_runtime import PACING_BASELINE
from ipc_security import private_lock
from ecn_feedback import estimate
from paths import BINDIR, INSTALLED, ROOT

FLOOR = 50_000
CEILING = 1_500_000
FALLBACK = 400_000
STATIC = 180_000
SLO_TARGET = 30_000
STRATEGIES = {'pacing-queue': 1, 'pacing-slo': 2, 'pacing-hybrid': 3, 'ecn-pacing-control': 3, 'ecn-aware': 4,
              'deadline-adaptive': 5}
SETTINGS = ('cookie', 'cap_bytes_s', 'tcp_pacing_bytes_s', 'delivery_bytes_s', 'bytes_acked',
            'notsent_lowat', 'notsent_bytes', 'rtt_us', 'cwnd_segments', 'total_retransmits',
            'output_queue_bytes', 'send_buffer_bytes', 'ecn_negotiated',
            'delivered_segments', 'delivered_ce_segments', 'ecn_mode')
WIDE_MISSING = 2**64-1
NARROW_MISSING = 2**32-1
U64 = c.c_uint64
P = c.c_void_p


def library():
    directory = (ROOT/(ROOT/'installed-lib-layout').read_text().strip()).resolve() if INSTALLED else BINDIR
    result = c.CDLL(str(directory/'libflowcore_pacing_lab.so'))
    signatures = {
        'open': (P, [c.c_int, U64, U64, U64, c.c_uint32, c.POINTER(c.c_int)]),
        'rate': (c.c_int, [P, U64]),
        'close': (c.c_int, [P]),
        'inspect_v2': (c.c_int, [c.c_int, c.POINTER(U64)]),
        'controller': (P, [c.c_uint, U64, U64, U64, c.c_uint32]),
        'delete_controller': (None, [P]),
        'evaluate_v3': (c.c_uint, [P, c.POINTER(U64), U64, c.POINTER(U64)]),
        'confirm': (None, [P, c.c_bool]),
    }
    for name, (restype, argtypes) in signatures.items():
        method = getattr(result, 'fc_pacing_'+name)
        method.restype, method.argtypes = restype, argtypes
    return result


def checked(status, operation):
    if status:
        raise RuntimeError(f'{operation}: {os.strerror(status)}')


class PacingWorkload:
    def __init__(self, output, mode, deadline_floor=None):
        self.output = output
        self.mode = mode
        self.lib = library()
        self.controller = None
        self.actuator_lock = None
        fallback = PACING_BASELINE if mode == 'deadline-adaptive' else FALLBACK
        self.rate = fallback if mode in STRATEGIES else STATIC if mode == 'pacing-static' else 0
        if mode in ('deadline-admission', 'deadline-paced-static'):
            self.rate = PACING_BASELINE
        self.floor = deadline_floor if mode.startswith('deadline-') else FLOOR
        self.lowat = 4096 if mode == 'pacing-lowat' else 0
        self.algorithm = mode.removeprefix('tcp-') if mode in ('tcp-cubic', 'tcp-reno', 'tcp-bbr') else None
        if mode in STRATEGIES:
            self.actuator_lock = private_lock(output/'pacing.lock')
            floor = deadline_floor if mode == 'deadline-adaptive' else FLOOR
            self.controller = self.lib.fc_pacing_controller(STRATEGIES[mode], floor, CEILING, fallback, SLO_TARGET)
            if not self.controller:
                self.actuator_lock.close()
                raise RuntimeError('cannot create native pacing controller')
        self.sockets = {}
        self.previous = {}
        self.previous_ecn = {}
        self.previous_aqm = {}
        self.last_sample_ns = 0
        self.last_feedback_ns = 0
        self.polls = 0
        self.actions = 0
        self.transition_holds = 0
        self.rollbacks = 0
        self.fallbacks = 0
        self.drain_count = 0
        self.drain_over_ms = 0
        self.drain_wait_ns = 0
        self.sample_cpu_ns = 0
        self.started_cpu = time.process_time()
        self.peak_notsent = 0
        self.observed_notsent = 0
        self.background_attaches = 0

    def add(self, flow, sock):
        if flow in self.sockets or len(self.sockets) >= 32:
            raise RuntimeError('background socket quota or overlap')
        handle = None
        if self.rate or self.lowat:
            failure = c.c_int()
            handle = self.lib.fc_pacing_open(sock.fileno(), self.rate, self.floor, CEILING, self.lowat, c.byref(failure))
            if not handle:
                checked(failure.value or 5, 'attach socket policy')
        self.sockets[flow] = (sock, handle)
        self.background_attaches += 1

    def remove(self, flow):
        _, handle = self.sockets.pop(flow)
        if handle:
            status = self.lib.fc_pacing_close(handle)
            # An asynchronously closed connection has no reachable socket policy.
            if status not in (0, errno.EBADF):
                checked(status, 'restore socket policy')

    def drain(self, elapsed_ns):
        self.drain_count += 1
        self.drain_wait_ns += elapsed_ns
        self.drain_over_ms += elapsed_ns >= 1_000_000

    def observe(self, now, p99_ms, requests, deadline_pressure):
        info = []
        for flow, (sock, _) in self.sockets.items():
            values = (U64*len(SETTINGS))()
            if self.lib.fc_pacing_inspect_v2(sock.fileno(), values):
                continue  # The transport may already have closed.
            result = dict(zip(SETTINGS, values))
            for field in ('tcp_pacing_bytes_s', 'delivery_bytes_s', 'bytes_acked'):
                if result[field] == WIDE_MISSING:
                    result[field] = None
            for field in SETTINGS[6:]:
                if result[field] == NARROW_MISSING:
                    result[field] = None
            if result['ecn_negotiated'] is not None:
                result['ecn_negotiated'] = bool(result['ecn_negotiated'])
            if result['ecn_mode'] is not None:
                result['ecn_mode'] = ('disabled', 'classic', 'accurate', 'pending')[result['ecn_mode']]
            result['flow'] = flow
            if result['notsent_bytes'] is not None:
                self.peak_notsent = max(self.peak_notsent, result['notsent_bytes'])
                self.observed_notsent += 1
            info.append(result)

        previous, last = self.previous, self.last_sample_ns
        self.previous = {row['cookie']: row['bytes_acked'] for row in info if row['bytes_acked'] is not None}
        self.last_sample_ns = now
        common = self.previous.keys() & previous.keys()
        acked = sum(max(0, self.previous[key]-previous[key]) for key in common)
        throughput = acked*10**9//(now-last) if last and now > last else 0
        snapshot_path = self.output/'pacing-input.json'
        try:
            feed = read_json(snapshot_path, limit=8192)
        except (OSError, ValueError):
            feed = {}
        when = feed.get('time_ns', 0)
        fresh = 0 <= now-when <= 2_500_000_000 and when != self.last_feedback_ns
        earlier = self.last_feedback_ns
        if fresh:
            self.last_feedback_ns = when
        capacity = feed.get('capacity_bytes_s', 0)
        queues = sum((row['notsent_bytes'] or 0) for row in info)
        demand = min(capacity, max(throughput, self.rate*len(info) if queues else 0))
        rtts = [row['rtt_us'] for row in info if row['rtt_us'] is not None]
        valid = fresh and bool(common) and len(info) == len(self.sockets)
        decision, proposed, verified = 'hold', self.rate, None
        congestion = None
        if self.mode.startswith('ecn-'):
            current_ecn = {row['cookie']: row for row in info}
            aqm = feed.get('ecn_aqm', {})
            sample_ns = feed.get('ecn_sample_ns')
            ecn_fresh = valid and type(sample_ns) is int and 0 <= now-sample_ns <= 2_500_000_000
            congestion = estimate(previous=self.previous_ecn, current=current_ecn,
                before_aqm=self.previous_aqm, after_aqm=aqm, fresh=ecn_fresh,
                capacity=capacity, delivery_rate=throughput, rtt_us=max(rtts, default=0),
                p99_us=round(p99_ms*1000), requests=requests, elapsed_ns=when-earlier,
                scope_matches=self.mode != 'ecn-cake')
            if fresh:
                self.previous_ecn, self.previous_aqm = current_ecn, aqm
        if self.controller and (fresh or not when or now-when > 2_500_000_000):
            inputs = (U64*13)(capacity, throughput, demand, feed.get('backlog_bytes', 0),
                              max(rtts, default=0), min(round(p99_ms*1000), 30_000_000),
                              requests, len(info), valid, feed.get('class_active', False),
                              feed.get('transition', False), bool(congestion and congestion['early']), deadline_pressure)
            next_rate = U64()
            answer = self.lib.fc_pacing_evaluate_v3(self.controller, inputs, self.rate, c.byref(next_rate))
            decision = ('hold', 'adjust', 'rollback', 'fallback', 'class-only')[answer]
            proposed = next_rate.value
            if decision in ('adjust', 'rollback', 'fallback'):
                verified = self.apply(proposed, decision)

        with (self.output/'socket-pacing.jsonl').open('a') as log:
            log.write(json.dumps(dict(time_ns=now, fresh=fresh, valid=valid, decision=decision,
                proposed_bytes_s=proposed, verified=verified,
                verified_ns=time.monotonic_ns() if verified else None, applied_bytes_s=self.rate, capacity_bytes_s=capacity,
                throughput_bytes_s=throughput, background_demand_bytes_s=demand, backlog_bytes=feed.get('backlog_bytes'),
                class_active=feed.get('class_active'), transition=feed.get('transition'),
                request_p99_ms=p99_ms, requests=requests, congestion=congestion,
                deadline_pressure=deadline_pressure, sockets=info))+'\n')
        self.polls += 1

    def apply(self, rate, decision):
        applied = []
        for _, handle in self.sockets.values():
            if not handle or self.lib.fc_pacing_rate(handle, rate):
                rollback_ok = True
                for changed in reversed(applied):
                    if self.lib.fc_pacing_rate(changed, self.rate):
                        rollback_ok = False
                values = (U64*len(SETTINGS))()
                for sock, _ in self.sockets.values():
                    if self.lib.fc_pacing_inspect_v2(sock.fileno(), values) or values[1] != self.rate:
                        rollback_ok = False
                self.lib.fc_pacing_confirm(self.controller, False)
                if not rollback_ok:
                    raise RuntimeError('socket group rollback could not be verified')
                return False
            applied.append(handle)
        self.lib.fc_pacing_confirm(self.controller, True)
        self.rate = rate
        self.actions += 1
        self.rollbacks += decision == 'rollback'
        self.fallbacks += decision == 'fallback'
        return True

    def tick(self, p99_ms, requests, deadline_pressure=False):
        begin = time.process_time_ns()
        try:
            if self.actuator_lock is None:
                self.observe(time.monotonic_ns(), p99_ms, requests, deadline_pressure)
                return
            try:
                fcntl.flock(self.actuator_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.transition_holds += 1
                return
            try:
                self.observe(time.monotonic_ns(), p99_ms, requests, deadline_pressure)
            finally:
                fcntl.flock(self.actuator_lock, fcntl.LOCK_UN)
        finally:
            self.sample_cpu_ns += time.process_time_ns()-begin

    def finish(self):
        if self.controller:
            self.lib.fc_pacing_delete_controller(self.controller)
            self.controller = None
            self.actuator_lock.close()
        return dict(mode=self.mode, socket_actions=self.actions, rollbacks=self.rollbacks, fallbacks=self.fallbacks,
                    samples=self.polls, transition_holds=self.transition_holds, peak_notsent_bytes=self.peak_notsent if self.observed_notsent else None,
                    drain_calls=self.drain_count, drain_wait_ms=self.drain_wait_ns/1e6,
                    drain_waits_over_ms=self.drain_over_ms, sampler_cpu_ms=self.sample_cpu_ns/1e6,
                    workload_cpu_seconds=time.process_time()-self.started_cpu,
                    requested_cc=self.algorithm, background_attaches=self.background_attaches,
                    notsent_lowat=self.lowat or None)
