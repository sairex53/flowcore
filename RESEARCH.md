# Research Lab

FLOWCORE Research Lab reuses the managed daemon, observer, TCP workloads,
adaptive controller and owned veth namespaces. A version 1 manifest fixes
the topology, link conditions, offered workload, class mix, seed, policy
and repetitions. See scenarios/chaos-smoke.json for scheduled impairments
and scenarios/bulk-intermittent.json for finite transfers and intermittent
requests.

```sh
sudo ./build/flowcore bench run scenarios/chaos-smoke.json
sudo ./build/flowcore bench suite --quick
sudo ./build/flowcore bench replay results/RUN/experiment.json
./build/flowcore bench report results/RUN/summary.json
./build/flowcore bench compare results/BASE/summary.json results/CANDIDATE/summary.json
sudo ./build/flowcore chaos run scenarios/chaos-smoke.json
sudo ./build/flowcore chaos stop
```

Stop any management daemon before running. Research mode has a workspace
lock and pins namespace file descriptors before applying faults. The runner
records scheduled, begun and verified monotonic timestamps. Capacity changes
rebase the owned shaper and explicitly re-arm the adaptive mode when it
was running. The CLI aborts a failed trial and preserves artifacts.
Replay restores configured stimuli and seeds, not packet scheduling.
The minimizer checks a declared request failure fraction twice per attempt
and reduces fault steps and trailing duration within its budget.

The Linux reference uses the default HTB leaf under the same bottleneck.
FIFO and fq_codel use the same HTB rate. CAKE uses its integrated shaper.
The static allocation is declared in the manifest. Each repetition starts
with new namespaces and randomized mode order. Phase timing and application
latency use CLOCK_MONOTONIC. The impairment is on receiver egress, so the
configured base RTT adds delay in one direction. Real TCP latency also
includes scheduling, handshake, buffering, loss recovery and application
time. An outage can result in explicit failed requests or long successes.

Application P99 is conditional on success. The comparison excludes paired
P99 samples when either run has request failures and compares failure rates
separately. The paired sign test operates on independent repetitions with
matching seeds and environments. Bonferroni corrects for the planned metrics
and mode pairs. Median intervals use run-level order statistics when the
sample size permits. Two repetitions are exploratory. Negative and
inconclusive comparisons are retained.

Reports embed charts for request tails, background goodput and fairness,
queue backlog and observed sender/netem drops, plus actual faults and policy
writes. Jain fairness is aggregated over phases with at least two active
background streams. Receiver counters measure delivered bytes rather than
sender buffer writes. Queue counters are sampled and can miss short bursts.
TCP retransmissions are observed deltas or optional BPF events, with known
coverage limits. Controller convergence requires two nonoverlapping eligible
windows at the same allocation after the last fault or write. Null means
that criterion was not observed.

The ARM64 VM shares CPU scheduling and virtual interfaces. Clock, scheduler,
observer, tc and Python sampling overhead can affect results. CPU seconds
include daemon and helpers, while RSS is sampled. Keep full profiles and
comparison order, and repeat on dedicated hardware before attributing small
differences to policy.

The original 14-run fault study is in
results/research-1793812489565060495/report.html. Final platform regressions
and the most recent quick suite are identified in PLATFORM_RESULTS.md.
