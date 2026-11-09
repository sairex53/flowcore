# Integrated engine

`integrated` is an opt-in Research Lab arm for the owned sender veth with classified HTB, fq_codel ECN leaves and a sender capacity up to 12 Mbit/s. Short request/response clients report application timing and deadlines. Multiple bulk senders are application-owned TCP sockets. The daemon sets no socket options on external processes.

| Action | Authority and verification |
|---|---|
| Hold | Stale, repeated, incomplete or unqualified observations, no applicable improvement, cooldown or exhausted budget. |
| Socket pacing cap | Application-owned descriptor and cookie, stable cohort and generation, complete app sample, current HTB readback and finite operator bounds. Native handles read back each write. |
| Socket compensation | Restore the previous verified cap after a failed outcome. Restore the nonbinding trial cap on lost scope, topology transition or changed cohort. Normal socket teardown restores the original option before close. |
| Scheduled class rebase | Research daemon only. Runner locks the transition, advances generation, runs existing namespace-owned TC updates, verifies the result and publishes fresh scope. |
| Classification, socket priority/low-water mark, MPTCP and admission | Separate existing authorities. Setup installs and verifies port classification. Other actuators remain disabled in this planner pending a common ownership and compensation protocol. |

The class baseline defines the aggregate minimum background reservation. The per-socket lower bound uses the initial reservation divided by the number of live managed bulk sockets, rounded up. It stays conservative across a bounded capacity decrease. Cohort membership is checked on the next pacing sample, so a closing socket can temporarily change the aggregate cap before compensation. This is a bound on configured caps. It does not guarantee that TCP or the receiver can deliver that rate.

## Observation and decision

The runner publishes one private bounded frame with the existing sample RPC. It adds namespace-lab quality, a generation, the observer sample timestamp and a check of the actual class rates/ceilings, qdisc hierarchy and flower mapping against the configured tree. TCP_INFO and pacing caps are read from the sockets. A sample needs at least twelve successful critical reports, no critical failures, a live cohort and timely verified scope. Repeated kernel timestamps do not build decision streaks. The SLO target comes from the validated manifest. The deadline signal requires at least one quarter of the current critical reports to approach or exceed 80% of their declared budget. ECN feedback is qualified by the existing AQM and TCP estimator.

A congested local queue with sustained pressure can authorize an action. Qualified ECN or queue-backed deadline pressure can trigger earlier. The candidate sets the aggregate cap to at least 75% of current shaped capacity and 85% of observed bulk throughput, subject to the background floor and per-socket bound. The initial 1.5 MB/s cap per socket is nonbinding at the admitted link rate. A one-second minimum cooldown limits writes. Evidence loss and scheduled transitions can restore the cap during cooldown.

The next eligible sample checks aggregate bulk progress and successful request P99. A P99 increase over 10% or bulk throughput below the background reservation can trigger rollback. These short samples are operational signals. Workload changes can trigger conservative compensation. Controlled comparisons use independent paired runs. Kernel confirmation time and actuation durations are measured separately from network stabilization.

## Commands

~~~sh
./build/flowcore nextgen capabilities
./build/flowcore nextgen capabilities --fabric-config operator.json
sudo ./build/flowcore bench replay scenarios/nextgen-smoke.json
./build/flowcore nextgen report results/<experiment>/summary.json
~~~

Capabilities include an explicit scope and evidence level. A local socket probe or native estimator fixture does not attest to another namespace, connection or workload. The planner retains static HTB if fresh feedback becomes unavailable. A rejected group update compensates accepted writes and latches native actuation failure. An unverifiable compensation fails the experiment and closes the owned workload. Namespace removal and daemon restart terminate the old lab authority.

The static, queue-aware, SLO-aware, predictive and deadline-aware strategies retain their existing registry paths. This experiment combines the validated observation and actuator paths in one domain. Promotion requires new workloads, safe handoff testing and measured value.
