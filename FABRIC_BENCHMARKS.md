# Fabric benchmarks

## Method

Build FLOWCORE with the native intent SDK and run the lab as an ordinary operator through sudo. The fixture uses real OpenSSL certificates, mutually authenticated TLS, the standalone daemon and SDK, and Linux HTB and fq_codel. It creates a disposable network namespace for each of three sender hosts, a router and a server. A separate management veth from each host to the router carries Fabric traffic. Only the router's data egress toward the receiver has the shared 6 Mbit/s cap. Per-host caps are on sender egress. No preconfigured host interface is used.

```sh
cmake -S . -B build
cmake --build build -j2
sudo python3 -B scripts/fabric_lab.py --output results/fabric-reproduction --repetitions 2 --seed 7107
```

The independent baseline gives each standalone FLOWCORE host a fixed 2 Mbit/s cap and runs no coordinator or agent. Fabric starts at three 500 kbit/s protected floors, pulls a verified generation, and moves to the declared 4/1/1 Mbit/s allocation for the API and two batch hosts. The same app clients run in both cases. Each host offers 240 persistent HTTP requests at 40 requests/s, a 200 ms request deadline and one verified 1 MiB upload. Runs start with fresh namespaces, processes, SDK connections and queue counters. Mode order is randomized per paired seed. This compares a declared capacity allocation against equal local caps. It does not isolate an automatic latency optimization effect.

Latency percentiles use successful application requests. Errors and misses are reported separately. Utilization and Jain's index use per-egress qdisc byte deltas sampled over roughly 1.5 seconds during transfer overlap. Jain's index summarizes equality of measured sender rates. The declared priority allocation deliberately trades equality for higher API bandwidth. CPU deltas and RSS are sampled for the coordinator, agents and actuators. The CPU delta is process CPU time and excludes subprocess CPU and kernel processing. Control traffic is measured as management interface byte deltas, including transport overhead. It excludes operator calls over router loopback.

A separate failure sequence follows the first measured Fabric trial. It disconnects one management veth, checks stale state and local fallback, reconnects, kills the coordinator with SIGKILL, verifies protected floors, and restarts with the same state. It then switches the desired allocation to 1/4/1 Mbit/s, checks the verified lowering barrier and final caps, and restarts one agent. Timings use the fixture's local monotonic clock and include observation and command latency. Authentication checks exercise an incorrect server name, a CA-signed certificate without a pin, a cross-host identity claim and an agent attempting an operator operation.


## Measured results

On 2026-11-07, four trials completed on the Fedora 45 ARM64 QEMU VM with four vCPUs and kernel 7.2.11-300.fc45.aarch64. The paired seeds were 7107 and 7108. Each trial completed 720 HTTP requests and all three uploads. The request table reports P99 and deadline misses by host. No request errors occurred in these four trials. Raw outputs include P50, P95, dispatch times and per-request results.

| Seed | Mode | API P99 ms / misses | Batch B P99 ms / misses | Batch C P99 ms / misses | Shared utilization | Sender-rate Jain |
|---|---|---:|---:|---:|---:|---:|
| 7107 | Independent | 145.50 / 0 | 168.80 / 2 | 164.03 / 1 | 99.1% | 1.000 |
| 7107 | Fabric | 115.33 / 0 | 193.51 / 2 | 111.29 / 0 | 97.8% | 0.674 |
| 7108 | Independent | 155.06 / 0 | 160.41 / 0 | 142.72 / 0 | 97.5% | 1.000 |
| 7108 | Fabric | 186.51 / 2 | 227.13 / 8 | 272.02 / 8 | 98.8% | 0.675 |

The API upload finished in 2.30 seconds in each Fabric trial versus 4.70 and 4.73 seconds with independent caps. The batch uploads took 9.42–9.52 seconds with Fabric versus 4.69–4.74 seconds independently. API P99 improved in one pair and regressed in the other. The observations confirm bandwidth redistribution. They do not establish an SLO improvement. The two pairs on a shared VM do not support a statistical ranking or predict physical-host performance.

All six Fabric client SDK connections registered and both trial snapshots showed service samples for all three hosts. The SDK reported 26 dropped telemetry events across the two Fabric trials. The live service view summarizes local rolling windows, whereas the table summarizes each entire six-second request workload.

The first coordinated generation reached verified readback in 1.19 and 0.94 seconds. Over 11.51–11.66 seconds of measured workload plus startup lead and upload drain, the coordinator used 0.18–0.26 CPU seconds and approximately 34.6 MiB RSS at the end. Each agent used 0.14–0.30 CPU seconds. Each active actuator used 0.18–0.38 CPU seconds, versus 0.00–0.01 in the independent runs. Those actuator figures exclude their tc child processes. Management links carried approximately 142 kB transmitted plus received per agent per Fabric trial, versus 640 bytes per link in the independent trials.

In the first paired Fabric run, the disconnected agent reverted to its 500 kbit/s floor in 2.94 seconds and regained verified state 1.47 seconds after link restoration. After SIGKILL of the coordinator, all three local caps returned to their floors in 3.07 seconds. The restarted coordinator reconciled the same epoch and generation in 3.19 seconds. The following 4/1/1 → 1/1/1 → 1/4/1 Mbit/s rollout took 2.29 seconds with kernel readback at both stages. Agent restart verification took 0.28 seconds. The TLS checks rejected all four unauthorized or misconfigured cases. These are individual observed fault timings, not timeout guarantees.

Artifacts for the paired matrix are under `results/fabric-measured-20261107/` on the VM. The directory holds mode order, environment, request and bulk results, raw counter samples, decisions, and role logs. Temporary keys are removed by the fixture. All test hosts were namespaces on one Linux boot. No independent physical hosts were used.

A separate single Fabric smoke trial verified the final pinned Unix socket path and made successful HTTP application probes while the management link was down and while the coordinator was killed. Its artifacts are under results/fabric-pinned-20261107/. These reachability probes did not measure SLO during the outage.
