# FLOWCORE Network Research Lab 2.0

The lab adds a duration-bounded scenario format and replay path to the existing research runner. It keeps the existing owned veth-pair experiment and version 1/2 manifests readable.

## Safety boundary

Run only against the lab runner:

    sudo ./build/flowcore chaos run scenarios/chaos-overlap-v3.json
    sudo ./build/flowcore chaos stop
    sudo ./build/flowcore bench replay results/<experiment>/experiment.json
    sudo ./build/flowcore bench minimize scenarios/chaos-overlap-v3.json --mode fq_codel --failure-ratio 0.1 --budget 10

The runner requires the privileged FLOWCORE laboratory daemon and an exclusive lock. The daemon creates two disposable network namespaces and the fc0/fc1 veth pair. The scenario format cannot name host interfaces, namespaces, commands, files, or qdisc handles. Faults are applied only to the veth interfaces owned by this lab. A partial kernel update invalidates the run and tears down its disposable lab.

fc0 sender egress contains the forward HTB bottleneck. fc1 receiver egress carries the reverse path and netem impairments. Version 3 can therefore configure asymmetric forward/reverse bandwidth. Delay, jitter, packet loss, correlated-loss probability, duplication, and reordering currently apply on the reverse egress. The host physical network and its qdisc tree are outside this experiment.

## Version 3 scenario format

Version 3 keeps the version 2 workload, controller, and regression fields and adds:

- stages: an ordered, contiguous partition of the run into warmup, baseline, congestion, fault_injection, recovery, and post_recovery_validation.
- events: up to 64 named faults with an offset, duration, and typed effects object.

scenarios/chaos-overlap-v3.json is the runnable reference. It combines progressive forward capacity reductions, an independent reverse-path collapse, correlated netem loss with high delay/jitter, packet duplication/reordering, and an interface flap. Scenario and workload phases are independently scheduled and both are recorded.

Overlapping effects compose as follows:

- Bandwidth is the minimum active capacity, independently for forward and reverse directions.
- Delay and jitter use the maximum active configured values.
- Independent loss and duplication probabilities compose as 1 - product(1-p).
- Correlated netem loss uses one active loss probability and its loss_correlation_pct. The validator rejects overlapping correlated and independent loss models because their semantics would be ambiguous.
- Reordering uses the maximum active percentage.
- Any active link_up: false keeps the owned link down. It returns to up when the last down event ends.

The Linux netem random-loss correlation setting provides a correlated packet sequence; it is not a calibrated Gilbert-Elliott or application-level burst model. Netem applies packet behavior probabilistically, so a replay does not promise identical packet outcomes.

## Timing, replay, and artifacts

The runner uses CLOCK_MONOTONIC time for schedule deadlines and records scheduled_ns, actual_application_ns, kernel_confirmed_ns, schedule_lag_ns, and qdisc/link readback. Event end records include scheduled duration, duration between kernel readbacks, and the delay between the scheduled recovery and confirmed application. These are actuation timings; they are not a claim about when an application recovered. Failures to apply a transition are written to the transition log before the run is invalidated.

Each run preserves the manifest and seed, stages.jsonl, transitions.jsonl, transitions.csv, request ledger, controller decision history, qdisc and interface snapshots, kernel samples, JSON/CSV/HTML reports, and a reproduction command. HTML timelines mark scenario stages, network transitions, controller actions, application P99, failures, goodput, backlog, drops, and actuation latency.

Replay fixes the manifest, random seeds, event ordering, and target offsets. It does not make scheduler wakeups, qdisc execution, or packet scheduling deterministic on Linux.

## Workloads and current limits

The existing workload supplies short TCP request/response connections, long-lived TCP bulk streams, changing request rates, multiple service classes, concurrency, IPv4 and IPv6. Version 3 also supports paced, one-way UDP background datagrams with a receiver-side byte counter. Its UDP generator is a background load source, not a UDP request/response client, and UDP currently enters the default qdisc class; the report does not claim per-class UDP service. TCP request connections remain one per request; persistent traffic is represented by bulk streams. Asymmetric bandwidth is supported, while delay/loss/jitter/reorder/duplication currently use the receiver namespace egress (the reverse path for request/response TCP, but not the forward UDP stream). These limits are explicit so a scenario does not report a workload it never generated.

The post-recovery stage is a recorded observation window, not a standalone service-level recovery guarantee. Request success/failure, deadlines, class throughput, and controller decisions remain available in the raw artifacts. A successful-request P99 is always interpreted alongside failures and completion ratio.

## Minimization and property checks

The minimizer first reproduces a failure in two independent runs. It can then remove fault events, shorten event durations, remove or weaken event effects, reduce request/background load, and shorten the experiment while preserving all required stages. Every candidate is schema-checked and must still meet the requested failure ratio in both runs. Candidate outcomes and artifacts are retained; the original scenario is never overwritten.

tests/research_properties.py runs deterministic generated cases for network composition, bounded manifest parsing, duration preservation, service reservation constraints, namespace-holder identity, worker/holder teardown order, and all legal/illegal controller-state edges. The suite is dependency-free and runs unprivileged. Privileged veth/qdisc, queue-aware-controller action-budget, and cleanup tests are separate in tests/research_integration.py and tests/research_controller_chaos.py and .github/workflows/privileged-lab.yml; that job is restricted to manual dispatch on main and a labelled trusted Fedora ARM64 runner.

## Optional soak

A bounded repeated-trial soak creates a new disposable lab and daemon for every cycle:

    sudo ./build/flowcore bench soak scenarios/chaos-overlap-v3.json \
      --mode fq_codel --cycles 10 --max-minutes 60 \
      --max-rss-growth-mb 256 --max-fd-growth 32 --max-bpf-growth 8

It records per-cycle RSS, file descriptors, namespace identity, workload outcome, and each trial's normal report artifacts. When `bpftool` is available and readable, it also records system-wide program, map, link, and bpffs path counts and stops if post-cycle counts grow beyond `--max-bpf-growth`; otherwise BPF inventory is explicitly marked unavailable. This global count is a leak signal, not per-process attribution. It stops at the wall-clock limit or a resource/cleanup guard. One in-flight experiment can finish after the time limit; the maximum overrun is bounded by one manifest duration plus cleanup. The trusted workflow offers a five-cycle opt-in soak. This is an optional soak harness, not evidence of thousands of policy changes or multi-day stability.

## Reproduction and tests

Unprivileged checks:

    cmake --build build
    ctest --test-dir build --output-on-failure
    python3 -B scripts/check_style.py

On the trusted Fedora ARM64 machine, run the isolated integration suite:

    sudo -n python3 -B tests/research_integration.py
    sudo -n python3 -B tests/research_controller_chaos.py
    sudo -n ./build/flowcore bench suite --quick

A generated replay should be checked for the same manifest, block/workload seed, ordered scheduled offsets, and kernel readbacks. Actual start/confirmation timestamps can differ. A negative latency result remains a valid experiment when setup and accounting pass.
