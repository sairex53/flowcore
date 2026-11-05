# Reproducible local-egress experiments

## Topology

Two anonymous network namespaces, sender and receiver, are connected by one
veth pair. Sender is 10.203.0.1/30 and receiver is 10.203.0.2/30. MTU is 1500.
TSO/GSO/GRO are disabled and recorded. Sender egress is shaped at 20 Mbit/s.
Receiver egress is unshaped. There is no artificial RTT in this initial slice.
All traffic originates in the sender, so uploads compete with critical requests.

## Policies

* fifo: HTB single 20 Mbit/s class with a 1,000-packet pfifo leaf.
* fq_codel: same HTB shaper with fq_codel limit 1,000 target 5ms interval 100ms.
* cake: CAKE's integrated 20 Mbit/s shaper, besteffort flows, RTT 100ms.
* static10/static20/static40: HTB critical reservations 10/20/40%, priority 0,
  remainder reserved for background at priority 2, both ceilings 20 Mbit/s.
  Both leaves use the same fq_codel settings as above.
* flowcore: identical classified hierarchy, initially balanced at 20%, adapting
  to 40% and higher borrowing priority using application feedback.

A flower filter classifies TCP destination port 9000. Port 9001 carries four
persistent, equally configured background TCP streams. CAKE and fq_codel do not
receive a service priority hint. Static policies and FLOWCORE share the same
hint. CAKE has its own shaper implementation, so burst behavior differs from HTB.
All queue settings and counters are retained for interpretation.

## Workload and measurements

Critical load is 100 requests/second, each on a fresh TCP connection, with a
4,096-byte upload and 64-byte reply. Each response must match the expected
payload. Requests are scheduled independently of prior completion using a
monotonic clock. Reported latency starts at the scheduled issue time and ends
at complete response reception, including connect and client scheduling delay.
There is a five-second request timeout, a 512-in-flight guard and explicit
failure records. Late issue times remain included. This avoids the most direct
closed-loop coordinated-omission bias. Python event-loop scheduling remains a
measurement limitation. Background uses bounded write buffers and measures
bytes received separately for each flow during the sample interval.

Run background for three seconds before issuing measured critical load. Keep
background running while the last request completes or times out. Receiver byte counters are sampled every 100ms and linearly interpolated at
measurement boundaries. Only receiver
bytes in the request-issue measurement interval count toward reported bandwidth.
P99 uses nearest rank per trial. Never pool trials to hide variation. Raw samples
and counts accompany every percentile. Request failures invalidate latency success claims and remain reported outcomes.
Client exit code 2 represents recorded request failures. Infrastructure failures
abort the experiment with retained evidence. P99 is conditional on success when
failures exist, and such a trial cannot win static-policy tuning.

## Tuning and evaluation

Use separate pilot runs for static10/static20/static40. Select the smallest pilot
P99 meeting background goodput >=50% of configured capacity and Jain index >=0.8.
Record the selection and any pilot that fails those criteria. Held-out runs use
fresh namespaces and the selected static configuration. Also retain all static
candidates' results. Randomize execution order using a recorded seed (20261105).
Run at least three repetitions of FIFO, fq_codel, CAKE, static variants and
FLOWCORE. Default measured duration is 12 seconds (1,200 offered requests),
which is exploratory. For stronger evidence use 120 seconds and >=10 repetitions.

An initial success gate is a >=20% median per-run P99 improvement over FIFO,
background goodput >=50% of capacity, and Jain index >=0.8 in every valid run.
A separate value gate requires improvement over the independently tuned static
policy and strongest fair-queue baseline. Results may fail either gate. Report
that explicitly. These are hypotheses until the saved measurements establish them.

## Execution and evidence

Build with `cmake -S . -B build -G Ninja -DCMAKE_CXX_COMPILER=/usr/bin/g++`,
`cmake --build build`, then `ctest --test-dir build --output-on-failure`.
Run `sudo python3 scripts/benchmark.py --duration 12 --repetitions 3` from this
project directory. A unique directory under results contains environment facts,
raw request CSV, receiver flow bytes, controller telemetry, qdisc/class/filter
snapshots, process logs, random order and summary.json. Failure retains evidence.
SIGINT and normal exceptions clean up child processes and namespace holders.
Hard-killed runs can leave holder processes; their PIDs are recorded per trial.

Kernel capability checks are actual attach/create operations in the disposable
lab. An unavailable baseline must be marked unavailable with stderr. Do not
silently substitute a different scheduler. Record kernel version, tools, source
hashes, offload flags, TCP congestion control and cgroup context. Check host
CPU load and repeat if the VM is CPU saturated. Claims are restricted to the
measured bottleneck and workload.

## References

* https://man7.org/linux/man-pages/man8/tc-htb.8.html
* https://man7.org/linux/man-pages/man8/tc-fq_codel.8.html
* https://man7.org/linux/man-pages/man8/tc-cake.8.html
* https://docs.kernel.org/admin-guide/cgroup-v2.html
