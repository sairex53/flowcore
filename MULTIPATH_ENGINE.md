# FLOWCORE experimental Intelligent Multipath Engine

The existing owned-namespace MPTCP laboratory is extended to 2–4 independent veth paths. This is an experimental Python orchestration and policy layer behind the C++23 CLI, not a production daemon actuator. Host-interface management is not enabled.

## Capabilities and ownership

`flowcore platform mptcp` probes the current namespace: MPTCP socket creation, runtime enablement, path manager, scheduler, available implementations, endpoint/limit dumps, and event-monitor availability. Missing capabilities are reported. Experiments require the kernel path manager and endpoint/limit APIs. Current validation covers Fedora 45 ARM64 only.

The lab uses inherited disposable namespace ownership, process holders, and the existing exclusive experiment lock. Only freshly created namespaces and endpoint IDs registered by this run are modified. Pre-existing endpoint collisions abort. Endpoint backup changes and removals require owned IDs; registration, changes, routes and netem settings are read back. Endpoint cleanup runs before namespace destruction. SIGKILL cleanup relies on the existing namespace-holder parent-lifetime mechanism; no persistent namespace mounts or host endpoints are created.

Endpoint operations use fixed argument arrays through iproute2's MPTCP generic-netlink interface. They do not use shell interpretation. Privilege remains confined to the isolated lab runner; this does not add host MPTCP operations to the privileged helper.

## Policies

| Strategy | Supported action |
|---|---|
| linux-default | All configured endpoints non-backup; Linux selects subflows |
| static-endpoints | Initial path is primary; other endpoints backup; fixed policy |
| latency-preferred | Lowest measured probe RTT P95 primary; others backup |
| reliability-preferred | Lowest observed probe failure ratio, then availability and RTT |
| throughput-preferred | Highest measured per-path echo goodput primary; others backup |
| slo-aware | Experimental probe RTT cutoff and availability heuristic, then latency preference |

Dynamic strategies recompute backup preferences at injected link failures and restoration. Their initial measurements are not continuously refreshed. Endpoint backup flags influence supported path preferences; they do not implement packet scheduling, guarantee immediate subflow migration, or establish a custom kernel scheduler. Kernel PM address signalling establishes subflows.

The SLO strategy's `probe_target_feasible` applies only to tiny echo probes. It cannot establish feasibility of a real service's application P99. An authorized service's configured target can be read using `--service`; this labels the synthetic workload and supplies its target. It does not associate arbitrary service traffic with this experiment. Actual workload deadline attainment and failures are measured separately.

## Measurements

Per-path probes record five TCP echo samples and one 64 KiB echo transfer. RTT, failure ratio, goodput and spread remain separate metrics. These small probes are uncertain estimates, not sustained available capacity or long-term reliability.

The workload uses a persistent explicitly selected TCP or MPTCP connection with 32 KiB verified echo requests and a 60 ms pause. Application latency uses monotonic send-to-verified-response timestamps. Failed requests remain in completion/failure/deadline accounting. It is a closed-loop workload; stalls reduce offered load.

MPTCP_INFO and TCP_IS_MPTCP confirm multipath versus TCP fallback. A trial cannot count as a valid MPTCP result unless a sample confirms at least two subflows. Subflow addresses and TCP_INFO snapshots are separate lists, never associated by index. TCP_INFO exposes supported RTT and retransmission fields; it does not measure application P99. Bytes at MPTCP level are connection-wide, not attributed to individual path addresses.

`primary_subflow_address_reobserved_ms` measures the first post-restoration address snapshot containing the original path. It is not proof of traffic resuming on that path. Request completion after restoration and goodput are reported independently. Subflow failure-detection time and per-path byte attribution are not implemented.

## Running

```sh
./build/flowcore platform mptcp
sudo ./build/flowcore platform multipath-lab --strategy linux-default --scenario primary-failover --paths 2 --duration 10
sudo ./build/flowcore platform multipath-lab --strategy latency-preferred --scenario multiple-failures --paths 3 --duration 8
sudo ./build/flowcore platform multipath-lab --compare --repetitions 1 --scenario stable --duration 6
python3 -B tests/multipath_test.py
```

Scenarios: stable, asymmetric, lossy, primary-failover, multiple-failures (at least three paths). RTT, rate and loss are configured separately on both directions; qdisc readback records actual kernel settings. Experiments preserve JSONL requests, capabilities, topology, probes, decisions, scheduled/actual events, cleanup JSON, per-run summaries, CSV and HTML under results/.

Comparison repetitions use the same seed per policy and rotate policy order. Single repetitions are INCONCLUSIVE; repeated summaries are exploratory and do not establish superiority. No confidence interval or performance acceptance gate is implemented in this lab yet. Independent repetitions and the broader benchmark methodology are needed for performance claims.

## Kernel references

- https://docs.kernel.org/networking/mptcp.html
- https://docs.kernel.org/networking/mptcp-sysctl.html
- https://docs.kernel.org/netlink/specs/mptcp_pm.html

Remaining work includes a native C++ PM client, authenticated daemon/helper integration, continuous quality sampling, broader IPv6 validation, stronger fault accounting and repeated comparative benchmarks.

The opt-in privileged regression test is:
```sh
sudo python3 -B tests/multipath_integration.py
```
It checks three subflows, overlapping failures, policy readback, connection survival, host-state preservation and cleanup. It is deliberately excluded from unprivileged CTest.
