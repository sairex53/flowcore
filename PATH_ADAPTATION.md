# Path adaptation laboratory

The new `flowcore platform path-lab` extends the existing multipath echo
workload, Linux endpoint helpers, capability probes, namespace holders
and exclusive research lock.

```mermaid
flowchart LR
    A["Client namespace"]
    R["Router namespace"]
    B["Server namespace"]
    A -->|"Path 1, routed"| R
    R -->|"HTB + fq_codel, shared with two UDP flows"| B
    A -->|"Path 2, direct 8 Mbit/s netem"| B
```

Path 1 has 4 ms configured RTT and a 12 Mbit/s forward router shaper at
startup. Its reverse path has endpoint netem but no matching router
shaper. Path 2 has 20 ms configured RTT, an 8 Mbit/s rate and 0.1% random
loss configured on each endpoint egress. The router reserves 30% for
TCP destination port 9017 and 70% for its default class. Each leaf can
borrow up to the parent rate. The two UDP flows share the default leaf.
The direct route bypasses that router. Linux IP forwarding is enabled
only inside the router namespace.

| Mode | Endpoint policy |
| --- | --- |
| `static-qos` | Fixed initial path and alternate marked backup |
| `linux-default` | Both owned endpoints signalled as non-backup, with the default kernel scheduler |
| `latency-preferred` | Existing startup probe ranking with carrier-triggered failover and restoration |
| `path-aware` | Fresh observed evidence selects an owned alternate when justified |

All modes use the same router QoS. The Linux comparator uses explicitly
configured endpoints to enable an equivalent two-path topology. It uses
the kernel's default path manager and scheduler. There is no claim that
the backup flag selects packets or migrates a plain TCP connection.
[Linux MPTCP](https://docs.kernel.org/networking/mptcp.html) ·
[MPTCP path manager netlink](https://docs.kernel.org/netlink/specs/mptcp_pm.html)

## Application intent and decisions

Use `--preference latency|reliability|throughput`, `--deadline-ms` and
an optional service label, or supply the versioned
[cooperative intent file](config/path-intent.json). The latter explicitly
declares MPTCP participation. It can carry a snapshot of an existing SDK
service's latency budget, reliability preference or throughput objective.
The operator selects the intent file when starting the isolated lab.
The label does not associate unrelated service traffic or grant privileges.
No live daemon is needed for this command.

Latency ranks paths by an observed echo RTT median plus a payload
transfer time at the configured rate. This is an optimistic proxy.
Throughput ranks by the rate readback with reliability and latency
tie breakers. Reliability ranks by observed recent probe failures and
then the delay proxy. Candidate and current measurements retain their
provenance. A healthy current path is held. With the latency preference,
queue pressure or a capacity change must coincide with recent deadline
risk or a proxy at or above the deadline before a proactive switch.
A 15% deadband, two fresh observations, 1.2 seconds of dwell and four
changes per trial bound proactive churn. Confirmed interface loss can
bypass the proactive dwell, but the alternate must still have fresh
route, probe and subflow evidence.

The workload reports negotiation with TCP_IS_MPTCP and MPTCP_INFO.
MPTCP_FULL_INFO associates addresses and TCP_INFO in the same bounded
snapshot when supported. The decoder uses the kernel-returned record
strides. A missing option leaves per-path TCP counters unavailable.
The controller requires an observed established alternate subflow to
act. The kernel manages subflow establishment and recovery. A carrier
recovery without fresh subflow evidence does not authorize moving back.
[Linux socket UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/mptcp.h) ·
[MPTCP_FULL_INFO implementation](https://github.com/torvalds/linux/blob/master/net/mptcp/sockopt.c)

## Reproduction

```sh
cmake -S . -B build -DFLOWCORE_BPF=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
python3 -B tests/path_adaptation_test.py
sudo ./build/flowcore platform path-lab --repetitions 10 --duration 10 --seed 44139
sudo ./build/flowcore platform path-lab --scenario stable --repetitions 3 --duration 8 --seed 66279
sudo ./build/flowcore platform path-lab --modes path-aware \
  --repetitions 1 --duration 10 --seed 97061 --intent-file config/path-intent.json
```

The dynamic matrix starts a competing traffic burst, reduces the router
limit to 10.8 and 9.6 Mbit/s, collapses it to 1.5 Mbit/s, takes the client
side of the routed link down and restores carrier and the original rate.
The run validates actual qdisc and link readback. A trial becomes invalid
if a scheduled fault takes more than 750 ms to confirm or if negotiated
multipath cannot be demonstrated. Samples, faults, decision and cleanup
journals, and raw per-request outcomes stay in the VM results directory.
