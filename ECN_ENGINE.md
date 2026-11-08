# Advanced Congestion Feedback Engine

FLOWCORE reads ECN configuration and negotiation separately. Configuration is a capability hint. Negotiation is observed on individual sockets. AQM marks and TCP acknowledgment feedback are separate observations. The engine uses existing TCP congestion algorithms and queue disciplines.

## Inspect and reproduce

```sh
./build/flowcore ecn capabilities
./build/flowcore ecn inspect --limit 128
./build/flowcore ecn monitor --count 5 --interval 1
./build/flowcore congestion signals
./build/flowcore congestion signals --artifact /path/to/socket-pacing.jsonl
./build/flowcore ecn lab scenarios/ecn-paired.json --check
sudo ./build/flowcore ecn capabilities --probe
sudo ./build/flowcore ecn lab scenarios/ecn-paired.json
sudo ./build/flowcore ecn lab scenarios/ecn-accurate-ablation.json
```

Capabilities shows namespace TCP sysctls, registered algorithms, relevant kernel config and visible supported AQM. The optional probe creates disposable veth pairs and tests live negotiation, marking and feedback. Inspect and monitor bound socket enumeration and restrict ordinary-user results to their own UID. Interface filtering selects the qdisc view. Socket and qdisc data in a host snapshot are independent. The monitor reports deltas only across matching socket cookies and complete snapshots. Signals can display a scoped experiment ledger with the evidence and verified outcomes.

Use a disposable Linux VM with iproute2 and a built FLOWCORE binary. An explicit probe requires namespace setup, TC privileges and CAP_NET_RAW for the packet socket. Ordinary inspection uses read-only netlink and sysctl queries. Network writes are limited to the pinned Research Lab interfaces and namespaces. The lab takes the existing research lock, checks AQM and HTB readback and removes the lab on failure.

The DCTCP profiles require an already registered stock algorithm. If Fedora supplies it as an inactive module, register it explicitly in the test VM with sudo modprobe tcp_dctcp. This is VM-wide module registration. The runner leaves global defaults and the congestion-control allowlist alone. DCTCP is restricted on this Fedora kernel, so the benchmark selects it per socket on the client and on the server listener and checks the connected socket algorithm. A fallback invalidates the corresponding DCTCP trial.

## Versioned profile

The ECN section belongs to a v2 or v3 Research Lab manifest containing only ECN experiment modes. Its schema version is 1. It specifies fq_codel target and interval in microseconds, a threshold for Classic or Accurate ECN (zero selects ordinary CoDel marking), and a separate nonzero shallow threshold for DCTCP. The optional tcp_ecn field selects Classic (1, the default) or Accurate (3) in both namespaces. The loss control forces 0 on both endpoints. DCTCP uses Classic ECN in this fixture. The profile and mode are fixed after initial setup. CAKE runs as a best-effort shaped root and uses its own AQM configuration.

FQ-CoDel leaves use a bounded queue, verified ECN options, and verified rates from the existing HTB backend. The lab rejects an unexpected AQM tree instead of replacing it. Compare queue and pacing modes with static baselines using the same capacity, workload, and traffic-class reservations. See [benchmarks](ECN_BENCHMARKS.md).

## Capability boundaries

Linux can negotiate Classic ECN or Accurate ECN, or fall back to non-ECN. The probes test all three cases. Inspect uses TCP_INFO flags and the extended mode field when returned by the kernel. A legacy negotiated flag identifies Classic-or-Accurate and leaves the exact variant unknown. The lab parses IPv4/TCP headers and records counts, including capture loss. It retains no packet bytes. [Linux ECN sysctls](https://docs.kernel.org/networking/ip-sysctl.html) · [TCP UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/tcp.h)

DCTCP, Classic ECN, Accurate ECN and L4S are distinct. The VM config listed DualPI2 as a module, but the experiment had no validated scalable L4S endpoint and no registered Prague. The Accurate ECN probe used ECT(0) CUBIC traffic. L4S remains outside these experiments. The wire probe and the controller use maintained sockets, rtnetlink-backed tc and TCP_INFO. There is no additional BPF attachment.
