# Multi-host experiment lab

Run the coordinator on a Linux machine with the same FLOWCORE checkout as the role workers. SSH aliases, keys and known hosts belong in your OpenSSH configuration. Each worker runs over an SSH stdin/stdout channel. The coordinator opens no remote control listener. The workload servers listen only on the specified test address and should be reachable only from the test network.

## Set up a topology

Use two Linux hosts for a direct test. Put a bottleneck on a dedicated egress of the client for forward tests or the server for reverse tests. The shaper can be a second worker on the same test endpoint. For a routed test use a third Linux host with forwarding enabled in a dedicated namespace and separate test links facing client and server. A separate bulk generator is optional. In reverse tests with a separate generator, its traffic and API traffic must leave through the same shaped router interface.

Prepare addresses, routes and firewall rules on the test network before starting the coordinator. Keep the SSH management interface out of the experiment. Use a disposable network namespace for the shaper. Use a dedicated veth, a test VLAN or a dedicated NIC inside that namespace. Set the link alias on the shaper and optional fault interface to `flowcore-lab:<owner>`. For example, inside an already prepared router namespace:

```sh
sudo ip -n flowcore-test link set dev to-server alias flowcore-lab:experiment-a
sudo ip -n flowcore-test link set dev to-client alias flowcore-lab:experiment-a
```

Use an operator account for remote execution. Ordinary endpoint workers need only Python 3, iproute2 and permission to open high TCP ports. The shaper worker needs root in the dedicated namespace and working `tc` and scheduler support. When a role specifies `namespace`, remote launch invokes `sudo -n ip netns exec <namespace> python3 -B <checkout>/scripts/multi_host_agent.py …`. Provision that exact command via an operator controlled launcher or narrow sudo rules over a root-owned checkout. Do not grant unrestricted privileged `ip netns exec`. A forced SSH command can launch a worker already inside its namespace. Use a role-specific SSH alias in that case and omit the manifest namespace. Local roles omit `ssh` and use the current checkout. The remote and local roles can be mixed.

On Fedora, iproute2 may package `tc` separately. For a source checkout, the agent uses the existing FLOWCORE_TC path when present and otherwise uses `tc` from PATH. Install ethtool for NIC metadata. Missing optional probes are captured with their errors. A worker without CAKE, HTB, fq_codel or netem support will report the relevant trial as invalid. For the adaptive modes, build the FLOWCORE native allocator on the coordinator with `cmake -S . -B build && cmake --build build -j2`.

## Run an experiment

Copy [the example manifest](config/multi-host.example.json) to a private local file and substitute your SSH aliases, checkout paths, interface names and IPv4 data addresses. The example uses documentation addresses. Remove `impairment` to test a stable path. A forward run sends requests and a bulk upload toward the server. A reverse run transfers a bulk download and classifies response packets by source port. The two request services use separate TCP ports and persistent HTTP/1.1 connections. The worker validates each data address against its selected interface and binds outgoing workload sockets to that source.

```sh
python3 -B scripts/multi_host_lab.py my-lab.json --discover-only \
  --output results/external-inventory
python3 -B scripts/multi_host_lab.py my-lab.json \
  --output results/external-matrix --repetitions 10 --seed 4711 \
  --modes fq_codel,cake,static,queue-aware,hybrid
```

Each repetition uses fresh worker sessions, listeners and qdiscs. Mode order is shuffled within each repetition from the recorded seed. Repetition seeds are shared across modes and seed the netem random loss generator when loss is nonzero. Linux packet scheduling and TCP behavior can still vary between runs. With delay or loss configured, a second authorized egress gets a netem qdisc at the requested coordinator offset. The artifact records the actual request/acknowledgment times and kernel readback. The two egress interfaces must be different so CAKE, HTB and fq_codel face the same fault.

| Mode | Queue policy |
|---|---|
| `unshaped` | Leaves the selected egress root unchanged. Treat it separately from rate-matched modes. The optional reverse-path fault still applies. |
| `fq_codel` | Single HTB capacity class with fq_codel. |
| `cake` | CAKE at the same configured rate with best effort and flow isolation. |
| `static` | HTB plus fq_codel leaves. Critical share is operator-configurable. Interactive and standard reserve 20% and 10%. Background receives the remainder with a floor of at least 20%. |
| `queue-aware`, `hybrid` | The same starting HTB policy with the FLOWCORE planner fed from remote queue and application samples. |

Route lookups verify declared egress devices. Separate HTTP health probes verify the two request paths against the actual shaper counters before starting bulk traffic. For a successful bulk transfer the shaped counters must account for at least the delivered payload bytes. HTB classed modes also require traffic in both service classes and the background class. Route checks and counters provide evidence about the instrumented hop. A physical path can have other bottlenecks, so validate available path throughput and isolation independently before making comparisons.

The experiment adapter calls the existing FLOWCORE strategy planner and native allocator. It requires two consecutive matching proposals, permits at most three steps of up to five percentage points each, and enforces a four second cooldown. It changes only existing HTB classes through the shared actuator with readback and compensation. The external adapter owns its observation window and actuation cadence. It does not run the production daemon lifecycle, predictor, or policy evaluation and rollback loop. Inspect `decisions` and `applied_actions` to determine whether the experiment exercised a policy change. Final cleanup removes the trial queue tree.

## Artifacts and recovery

The output directory contains the effective manifest, role inventories, clock exchanges, randomized order, raw request rows, transfer status, CPU and pressure samples, route checks, qdisc snapshots, fault readback, decisions, stderr and cleanup results. The CSV reports attempt counts, errors, timeouts, deadline misses and success latency. Paired JSON reports per-seed differences against static. Review invalid trials and failures before interpreting the median. The harness does not estimate statistical significance.

The shaper checks namespace and interface ownership and refuses preconfigured queues or ingress hooks. A namespace-scoped lease prevents another worker from acquiring the same interface. End of input, an expired idle lease, and handled signals run cleanup. A kernel command can complete before the client observes its result. After an unclean exit or a cleanup error, inspect the named namespace and the queue handles before retrying. Dispose of the dedicated test namespace when it has no other users. Do not run automatic cleanup on an unrelated interface.

To exercise the same coordinator without SSH or extra hosts, use the existing namespace lab for synthetic testing, or run the multi-role regression fixture below. It creates disposable veth links and also tests the router path.

```sh
sudo bash tests/multi_host_namespaces.sh results/multi-host-local
```
