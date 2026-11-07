# Incident Engine

## Event schema 1

Each event has `schema`, `seq`, `scope`, `time_ns`, `service`, `kind` and `data`. `seq` orders receipt within a daemon run. `time_ns` uses the local Linux monotonic clock. Application completions may arrive after later events. `scope` changes when observations move between a lab and the host, a lab is replaced, or the observed host interface generation changes. A stream ID in each bundle isolates daemon lifetimes. Monotonic values are comparable only within the same clock domain.

| Kind | Provenance and limits |
| --- | --- |
| `window` | Four seconds of application histograms and directional counter summaries. Rates are in kbit/s, P99 in µs, drops and retransmits are interval counts. |
| `queue` | Root and class TC/readback counters and local link fallback. Fields identify direction, class and optional capacity. Backlog is bytes. |
| `tcp` | Scoped TCP_INFO/INET_DIAG deltas, RTT and receive-window pressure, with an explicit coverage flag. |
| `request` | Sampled terminal event with start/end and explicit outcome, including timeout and cancellation. Optional stages and socket metadata. |
| `interface` | RTNL link, address and route notifications received by the daemon. |
| `policy`, `controller` | Lab policy activation, verified class changes, controller decisions and evaluations. Policy generations are daemon-local. Verified allocation changes include the new per-class guarantees and ceilings. |
| `resource` | Daemon CPU over the interval, Linux process peak RSS and optional system PSI some/avg10 (CPU, memory and I/O). System stalls give context. Per-tenant saturation requires separate instrumentation. |
| `mptcp_path` | Opt-in application degradation reports labeled by source. The standalone MPTCP lab also emits kernel-confirmed link state in the common event format. Link state does not attest subflow availability. |
| `fault` | Explicit research operations or scheduled validation stimuli. Scheduled time and applied time are recorded separately when both are available. |
| `quality` | Collector failures, truncation or intentionally missing telemetry in a lab fixture. |

The observer remains responsible for histogram accounting. Request detail is sampled up to once per 125 ms slot per service and consumes a separate 128-event ring. The general ring retains 383 events. High-rate clients cannot evict interface events by flooding request details. A retained service incident has up to 16 seconds of pre-trigger evidence and the adjacent comparison window, if available. The recorder retains the 16 latest incident bundles in memory, freezes them at trigger time, and reports evictions. A scope has one active episode per service plus an independent network episode. Two quiet windows rearm an episode. Restart clears the recorder. Export before shutdown if durable retention is required.

## IPC and replay

`diagnose`, `incident_list`, `incident_inspect` and `incident_export` are read-only operations under the existing Unix peer credential policy. The monitor service remains capability-free. The daemon neither opens export paths nor accepts replay files. Export writes JSON to stdout. It omits raw socket cookies, addresses, lease tokens and payload. It includes service names, interface names and numeric kernel identities. Apply operator access controls to exported artifacts.

A bundle carries the event schema version, daemon run ID, incident ID, anchor event, optional baseline event and retained events. The client accepts up to 512 flat events in a regular file of at most 768 KiB. It rejects symlinks, unknown fields, non-finite numbers, duplicate keys, invalid comparisons and unsupported versions. Replay applies the same rules to the retained input. This reproduces the diagnostic report offline. It does not replay TCP traffic, recreate kernel state or authenticate an externally supplied bundle.

## Explicit interventions

Diagnostics queries and replay have no actuator. A separate opt-in operation is available with a privileged daemon started using `--research` and an owned HTB lab.

```sh
flowcore diagnose --lab-intervention freeze
flowcore diagnose --lab-intervention restore
flowcore diagnose --lab-intervention disable
```

Freeze requires an active settled controller with a verified allocation. It holds the current allocation for up to 15 seconds. Expiry or an intent revision ends the freeze and invokes the existing controller stop/baseline restoration path. Restore checks the baseline against kernel readback. Disable uses the controller stop path. Kernel drift is refused. A failed restoration is handled by the existing controller fallback path.

The standalone diagnostic experiment compares lower offered bulk load, an alternate verified shaping rate, removal of path delay and removal of a measured application delay. Its fixture owns the interfaces and traffic sources. Run it on a disposable VM. The production monitor has no access to these actions. [Experiment protocol and results](DIAGNOSTIC_VALIDATION.md)

[Linux pressure stall information](https://docs.kernel.org/accounting/psi.html)
