# Benchmark validation and operating procedure

## Audited paths

The Research Lab's core TCP workload is scripts/workload.py. It implements open-loop scheduled request release and an opt-in bounded closed loop, TCP request/response connections, background TCP streams, optional UDP background streams, and the legacy request ledger. scripts/research_runner.py constructs the two-namespace veth experiment, configures the isolated qdisc/network scenario, samples kernel and host state, and saves each run. scripts/benchmark.py provides namespace ownership and network setup; scripts/benchmark_v2.py accounts request failures, deadlines, throughput, host CPU ticks, PSI, and runnable-task counts. scripts/research_stats.py compares independent run-level blocks; scripts/research_report.py renders JSON, CSV and HTML.

Other tools and older workload examples remain separate measurement paths. Unless they are run through a manifest with the integrity profile and their own stated instrumentation, their measurements do not gain the timing, pressure, or checksum guarantees described here. The new profile is not a blanket retrofit of every HTTP, gRPC, PostgreSQL, MPTCP, multihost, or legacy benchmark.

## Topology and traffic evidence

Each trial uses the existing owned network namespaces and veth pair. The manifest declares the configured bandwidth and impairment; per-run network.json and timeline.jsonl retain actual tc readback, class/qdisc counters, observed link counters, receiver counters and transitions. Configured rate and observed rate are separate fields. The v3 analysis checks that successful request payload does not exceed observed sender wire bytes beyond the declared counter-gap allowance. An absent sender counter or a broken forward-counter relationship invalidates a run. The qdisc's configured capacity is not presented as a measured physical NIC capacity.

The matrix in scenarios/measurement-v3-paired.json fixes an open-loop rate, payload, deadline, background-stream count, veth capacity, RTT impairment, random seeds, 12 paired repetitions, mode ordering, and warmup/cooldown. Static HTB uses the configured static critical share; FIFO and fq_codel retain their actual HTB/leaf-qdisc hierarchy; CAKE is configured as the sender root qdisc. The actual hierarchy and rate are available in each run's network.json. These are comparable on the same owned bottleneck and workload cohort, but they are distinct scheduler configurations. A generic fq_codel label must not be read as a claim that every fq_codel topology was tested.

## Validity rules

The v3 profile is intentionally strict. It invalidates a block for timestamp schema/order/conversion errors; an incomplete or duplicate request ledger; incomplete open-loop schedule; missed event-loop probe accounting; excessive scheduled-release lag; excessive event-loop lag; CPU PSI or guest steal above their predeclared limits; missing sender counters; payload/wire counter inconsistency; or request-generator ceiling hit. The exact numeric limits are in the selected manifest and are copied into experiment.json. A completed workload with a predeclared excessive failure fraction is FAILED_WORKLOAD. A client process crash preserves its partial, line-buffered timestamp ledger and records only a known lower bound of completed attempts. It does not guess the total offered cohort.

Missing PSI is a recorded limitation, not a fabricated zero. Context switches and CPU use are process-specific; CPU PSI and CPU ticks describe host-wide pressure. Runnable-task counts are snapshots, not evidence of sustained oversubscription. Guest steal time is not a measurement of QEMU host scheduler queues. Linux packet scheduling time is not available in this profile. The submit-to-response interval includes server work and userspace scheduling and is not attributed wholly to the network.

## Paired inference and gates

For serious initial comparisons, configure at least 10 independent paired blocks; the main supplied comparison uses 12. Candidate modes share the block seed and open-loop schedule fingerprint. All blocks remain listed. Invalid blocks invalidate their comparison, rather than being removed until a favorable subset remains. Workload failures remain explicit and successful-only P99 is marked censored when either paired trial had failures.

The v3 report computes run-level medians and paired differences. Deterministic bootstrap resampling operates on paired run-level effects. The declared family-wise interval correction spans candidate arms and gates. The one-sided sign-test power estimate is conditional on the manifest's assumed direction probability; it is not a promise of sensitivity. Thresholds are saved in the manifest before running. The outcome is VALID, INVALID_INFRASTRUCTURE, INCONCLUSIVE, FAILED_WORKLOAD or COMPLETED_WITH_REGRESSION. A result with insufficient independent pairs, missing outcome metrics, or censored successful-only tails cannot pass that metric's gate.

There is no automatic comparison to arbitrary old revisions. A cross-revision paired comparison requires instrumented summaries with identical workload and integrity profiles, equal block seeds, same kernel, same boot, same host and CPU topology, same tc version, matching host networking sysctls, and matching build configuration. Legacy summaries without this evidence are rejected as non-comparable. Different source hashes/commit IDs are expected for a revision comparison; they are retained as provenance.

## Commands

Run an experiment in the isolated lab:

    sudo ./build/flowcore bench replay scenarios/measurement-v3-paired.json

Inspect run states and check raw hashes:

    ./build/flowcore bench verify results/research-<run-id>/summary.json

Render a report:

    ./build/flowcore bench report results/research-<run-id>/summary.json

Compare eligible modes from one paired campaign:

    ./build/flowcore bench compare results/research-<run-id>/summary.json results/research-<run-id>/summary.json --baseline-mode static --candidate-mode fq_codel

Run the closed-loop and UDP evidence fixtures separately:

    sudo ./build/flowcore bench replay scenarios/measurement-v3-closed.json
    sudo ./build/flowcore bench replay scenarios/measurement-v3-udp.json

The UDP fixture verifies actual receiver UDP bytes and send-error accounting under a bounded capacity change. Its two repetitions are not a comparative study. The dispatch-delay and client-exit fixtures intentionally produce INVALID_INFRASTRUCTURE and FAILED_WORKLOAD states. Those are self-tests, not performance evidence.

## Test coverage

tests/measurement_integrity_test.py uses synthetic fixtures with known timestamps and known run-level outcomes. It checks nearest-rank percentile consistency, stage ordering, conversion boundaries, missing and duplicated samples, counter mismatches, checksums, report state, deterministic paired bootstrap gates, sample sufficiency, test power, counterbalancing, invalid block retention, and closed-loop manifest rules. The Fedora namespace runs exercise actual TCP, UDP, dispatch-delay and client-crash paths.

Run unit/integration tests after configuring the project:

    cmake -S . -B build
    cmake --build build -j2
    ctest --test-dir build --output-on-failure -j1

Raw run directories are retained under results/ and ignored by Git. Copy a complete directory elsewhere before deleting or cleaning it. checksums.json verifies file integrity but is not signed provenance; keep the environment metadata and the source hashes with the artifacts.
