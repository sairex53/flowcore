# Request failure diagnostics

The V3 open-loop ledger retains scheduled, released, admitted,
rejected, submission attempted, write drained, completed, timed out,
connection failure and deadline-miss counts for the offered cohort.
A closed-loop observation has release records and zero scheduled
records. A socket write attempt is recorded immediately before the
call. Write drain describes the application transport buffer. It does
not establish packet transmission, arrival or remote execution.

Every unsuccessful attempt has a stage reason. The supported reasons
are client in-flight limit, application admission rejection or deferral,
connection failure, connected I/O failure, response EOF and timeout
before submission, during connection or drain, or while awaiting a
response. An unrecognized outcome is UNKNOWN. Deadline misses include
failed requests and late successes when a deadline was supplied.
Request-payload offered and admitted rates describe the measured
cohort. Goodput includes only successful payload completed in the
measurement window. Saturating bulk demand and wire overhead cannot
be inferred from these application counters.

The earlier mixed-collapse matrix provides the following evidence.

| Mode | Offered | Rejected at client cap | Timed out awaiting response |
| --- | ---: | ---: | ---: |
| Shaped fq_codel | 6670 | 0 | 0 |
| CAKE | 6670 | 550 | 0 |
| Static HTB | 6670 | 495 | 72 |
| Optimizer | 6670 | 488 | 62 |

The cap rejection occurred before a socket operation. In the optimizer
arm the response timeouts occurred after write drain. TCP retransmission
and qdisc drop counters describe sampled scopes. They cannot assign a
loss to each failed request. The client cap is an observed mechanism.
The path from queue pressure and delayed completions to cap exhaustion
is an explanation supported by the experiment and its counterfactual
configuration change. A separate application processing error signal,
socket send-queue history and remote service saturation are unavailable
in this fixture and remain unknown.

The optimizer recorded zero adaptive actions. A representative
optimizer window measured 4.65 Mbit/s at the configured 4.65 Mbit/s
egress limit. At 91 requests per second, the workload schedules
5.22 Mbit/s of request payload alone before background or headers.
These numbers demonstrate that part of the offered workload was
infeasible at the imposed capacity, even before scheduler effects.
Generator release lag, CPU pressure and other infrastructure checks
are kept separate from offered workload failures.

The legacy sender used TCP flower matches. Bulk UDP port 9002 fell
through to standard class 1:12, where it competed with standard TCP
traffic. The corrected arm gives it the existing background class
1:20. The classification fixture sends real IPv4 and IPv6 UDP
packets and checks class counter deltas. UDP shares the background
guarantee with TCP bulk, so both forms of goodput must be inspected.

[Historical comparison](PERFORMANCE_EVIDENCE.md) ·
[Overload remediation](OVERLOAD_REMEDIATION_RESULTS.md)
