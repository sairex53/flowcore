# Scoped flow intelligence engine

Implemented opt-in vertical slice: CO-RE/libbpf TCX egress, bounded flow maps,
typed generation updates, owned-lab HTB selection, and TCP/UDP INET_DIAG fallback.
This is not a TCP implementation, host-wide classifier, or request-stream scheduler.

## Build, activation and ownership

Build normally with FLOWCORE_BPF=ON (default). libbpf >=1.3, BPF-target clang,
bpftool and kernel BTF enable the optional target. Actual load/attach is probed at
runtime; verifier/TCX failures return a reason and leave userspace networking intact.
Tested: Fedora 45 ARM64, kernel 7.2.11-300.fc45.aarch64, libbpf 1.7.0.
BPF compilation uses generated vmlinux.h, BTF and CO-RE relocation of optional
__sk_buff.gso_segs, with BPF ISA v3. No private TCP layout is needed in this program.

Ordinary users can run:
```sh
./build/flowcore flows --limit 128 --json
```
This reads current-namespace TCP/UDP sockets and never loads or attaches BPF.
Existing daemon/SDK functions remain independent of this engine.

Privileged isolated orchestration uses ServiceLab.start_flow_engine(), then
lab.flow_engine.set_policy(generation, rules), dump(), or lab.flow_intelligence().
No daemon or SDK registration automatically activates this path. No setuid binary,
file capabilities, host systemd activation or new privileged IPC socket is installed.
The native loader is an administrative tool, not a multi-tenant privilege broker.

The runner creates anonymous namespaces via existing namespace holders and passes
a held namespace FD. The loader validates NS_GET_NSTYPE, refuses both its initial
and PID 1 network namespaces, enters that explicit FD, and requires exactly lo/fc0.
The lab validates its owned veth topology and all four HTB leaves before launch
and each policy publication; lost HTB admission detaches this engine. These checks supplement the
trusted runner's FD provenance; a hostile root caller is outside this boundary.
Never grant arbitrary callers root access to this executable or its object argument.
Installed privileged deployments must protect object files and parent orchestration
as existing helper configuration requires; the test uses explicitly trusted project
build artifacts.

CAP_SYS_ADMIN is needed only by the isolated launcher/loader for setns.
After entry/opening the object, the loader drops bounding, permitted and effective
capabilities except CAP_BPF and CAP_NET_ADMIN, clears inheritable capabilities and
sets NoNewPrivileges. It bounds FDs to 128 and possible CPUs to 4096. The control
daemon, application SDK and read-only fallback need none of these capabilities.
This is capability reduction, not a claim of complete seccomp/filesystem isolation.

TCX query revision guards prevent concurrent initial attachment. The link FD is
held without pinning. Once per second the loader checks interface identity and TCX
program count/revision; changes terminate this engine and release only its link.
It does not remove another owner's program, qdisc, class or filter. It neither
creates nor continuously replaces a qdisc tree. Interface removal, input EOF,
parent death, SIGTERM and SIGKILL release kernel references; no pinned maps remain.
Held netns FDs deliberately keep a namespace alive until the final owner closes.
Killing a namespace holder alone is not equivalent to destroying that namespace.

## Attachment choice

| Mechanism | Decision and evidence |
| --- | --- |
| TCX egress | Selected and actually loaded/attached on owned fc0; packet metadata and HTB priority |
| Classic TC filter attachment | Existing flower baseline; no unsafe legacy-BPF fallback cleanup |
| cgroup socket / skb hooks | Investigated; no cgroup-wide attachment needed for present scope |
| sock_ops | Not added; TCP-only and unnecessary for TCP/UDP packet classification |
| Raw tracepoints | Existing namespace-filtered TCP state/retransmit/reset collector retained |
| fentry | Not added; no demonstrated metric gap justifies another tracing attachment |

Packet-side socket cookies and cgroup IDs avoid attributing softirq current PID to
the process owning a socket. A zero/missing cookie or cgroup stays unknown.

## Identification and lifecycle

A key contains socket cookie, observed egress ifindex, IPv4/IPv6 addresses, source/
destination ports, TCP/UDP protocol, and up to two VLAN IDs/protocols. Namespace is fixed by the attached interface
and held namespace FD, exported with a loader monotonic session identifier.
The isolated runner must retain the unshifted kernel monotonic time namespace;
time-namespace clock offsets are not normalized. Persisted consumers must also retain boot identity; IDs are not globally eternal.
Cookies are exact socket identifiers where available, not process or request IDs.

Without a cookie, tuples are observational/heuristic, particularly across NAT and
identifier reuse. Tuple-only TCP SYN after an observed FIN/RST starts a new activity
epoch; delayed packets can still make inference ambiguous. FIN/RST are wire flags,
not full kernel TCP state. first_ns/last_ns mean first/last observation, not socket
birth/death. Idle expiry can start another observation epoch even for a live socket.
PID/process information is deliberately absent rather than guessed.

The tuple is exactly what this egress hook observes. NAT original/reply tuple
correlation is not implemented. Forwarded traffic may have no socket metadata.
Only sender egress is attached: this is not automatically a bidirectional session.
TCP/UDP headers are read with bounded bpf_skb_load_bytes calls. IPv4 fragments,
IPv6 fragments, ESP, jumbograms and unsupported EtherTypes remain unclassified.
Up to two VLAN tags (including hardware metadata) and six IPv6 Hop-by-Hop, Routing,
Destination or AH headers, at most 256 extension bytes, are decoded. Transport
headers must fit the IP datagram length, even when Ethernet padding is present.
Unknown or malformed headers preserve priority and increment a reason counter.
This parses header structure, not extension-option semantics or checksums.
TLS contents, HTTP/2 requests and QUIC streams are never parsed or inferred.

## Maps, bounded work and policy protocol

| Map | Type and bound | Meaning |
| --- | --- | --- |
| policies | HASH, one entry | Whole generation plus up to 16 ordered service rules |
| flows_v2 | HASH, configurable 16–4096 entries | Per-key bytes, packets, activity, selected policy and metadata |
| totals | PERCPU_ARRAY | Fifteen fixed aggregate counters |

No packet ring buffer is used. All 16 rule positions are verifier-bounded/unrolled.
The policy HASH value has no spinlock and is replaced using BPF_ANY. One packet
reads one immutable published value; no sequence of separately updated rule maps
can expose a half-installed table. The loader reads back the complete generation
before acknowledgement. Malformed/repeated generations preserve the prior table.
Kernel update/verification failure terminates and detaches instead of pretending
the previous policy is known. Existing queued packets keep their earlier decisions.
Policies and qdisc trees are not a cross-kernel transaction: create/verify the
existing owned HTB hierarchy first, then publish rules. This engine does not alter
HTB guarantees, ceilings or class priorities.

The stdin administrative protocol is bounded to 4096 bytes, unsigned decimal only:
```text
set GENERATION COUNT PROTOCOL DPORT SERVICE CLASSID MARK CGROUP COOKIE ...
dump
page SNAPSHOT CURSOR
```
Initial generation 1 is empty; later generations strictly increase.
Rules allow TCP=6 or UDP=17, services 1..16, ports 0..65535, and only class IDs
65552/65553/65554/65568 (HTB 1:10/1:11/1:12/1:20).
Zero port/mark/cgroup/cookie means wildcard. First matching rule wins; ordering is
the explicit conflict-resolution policy. Service IDs are operator labels, not
trusted SDK service names. Cookie/cgroup/mark restrictions are explicit opt-in
operator mappings; no arbitrary application label grants class control.

A match sets skb priority to an existing HTB leaf in active mode; unknown traffic
preserves its previous priority. Observe-only records matches but sets no priority.
The existing kernel HTB classifier supports direct class selection through priority;
see [Linux HTB implementation](https://github.com/torvalds/linux/blob/master/net/sched/sch_htb.c).
Publication follows ordinary non-locking HASH element replacement semantics; see
[Linux hash-map implementation](https://github.com/torvalds/linux/blob/master/kernel/bpf/hashtab.c).
FD-owned attachment uses the [BPF link interface](https://docs.kernel.org/userspace-api/ebpf/syscall.html).

Per-flow values use a short BPF spinlock for coherent counters/metadata. Userspace
uses BPF_F_LOCK for reads. Cross-flow/per-CPU snapshots are not globally atomic.
Flow memory is preallocated/bounded; on exhaustion, map_full_packets counts packets
whose flow could not be retained. Classification still runs; this never intentionally
drops network packets. GC scans at most capacity entries once per second and expires
FIN/RST activity after 2 seconds, other idle activity after 15 seconds.
Lookup/delete can race new activity: retention is best effort, not a lossless ledger.

Output is a nonblocking atomic pipe, each JSON record below 4096 bytes. EAGAIN counts
output_lost. Protocol 2 freezes at most 4096 observations, with a snapshot identifier,
session, capture-end timestamp and total. Four rows per page keep replies atomic.
A capture expires after ten seconds; new dumps replace it. Cursor and identity are
validated. Pages share frozen values; the underlying cross-flow capture is not atomic.
The Python wrapper assembles all pages within eight seconds, validates identity,
count, uniqueness and timestamps, and returns either a complete retained snapshot
or an explicit failure. It does not return a silently truncated 512-row cache.
last_snapshot exposes completeness and export_failures counts failed exports.
This does not make the bounded kernel flow map a lossless history: idle GC, map-full
and missing metadata remain separate coverage limits. Missing records are not proof
of no traffic. The stream cache stays bounded to 512; one frozen native capture and
one assembled Python result are separately bounded to map capacity.

ABI 2 uses a 64-byte flow key and 72-byte value. The versioned map name flows_v2
and loader size checks reject mixed old/new artifacts before reading keys.

## Provenance and fallback

- packets/bytes: pre-qdisc skb observations, including headers/retransmitted packets.
  GSO means skb counts are not necessarily wire-packet counts; gso_segments is
  separate and not a substitute for a capture. Qdisc enqueue/dequeue/drops differ.
- classified: matched rules, including observe-only. applied: actual priority writes.
- per-flow counters: retained activity only; eviction/expiry/full-map gaps affect coverage.
- TCP state/retransmit/reset: existing raw-tracepoint observer, independently scoped.
- TCP RTT/cwnd/retransmit totals and socket drops: INET_DIAG/TCP_INFO/SKMEMINFO,
  associated only when a nonzero cookie matches in the same namespace/protocol.
- socket_snapshot in ServiceLab.flow_intelligence(): exact socket association, sampled
  separately from packet counters. tuple_relation reports same/different/not_comparable
  versus the connected socket tuple; different alone does not prove NAT. An isolated
  SNAT test verified exact-cookie joining despite changed source address and port.
  Original/reply conntrack reconstruction is still absent. UDP socket counters can cover several peers.
  No fabricated TCP metrics are exposed for UDP or closed/unobservable sockets.

flow_snapshot.py extends INET_DIAG to UDP and labels its reduced capabilities.
It reports kernel cookie, tuple, UID/inode, cgroup where provided and observation
time. A bound socket interface is not claimed to be observed egress. Short flows
between snapshots are missed. No BPF fallback classifier is silently installed:
existing flower/default policy stays in place. FLOWCORE_BPF=OFF, missing artifacts,
insufficient privilege, verifier failure and unsupported TCX all preserve this path.

## Validation and scope limits

```sh
ctest --test-dir build --output-on-failure
sudo python3 -B tests/flow_engine_integration.py
sudo python3 -B tests/flow_v2_integration.py
sudo python3 -B scripts/flow_engine_benchmark.py --repetitions 10 --duration 2
```
The dedicated root test covers exact capability reduction, dual-stack TCP/UDP, cookie/cgroup/mark selectors and socket snapshot
joining, actual counters in four HTB classes, unknown/observe-only behavior,
malformed/stale and concurrent generation updates, unsupported packet headers, concurrent ownership rejection, forced crashes,
bounded-map exhaustion and GC, reload, oversize input, interface deletion and final
namespace cleanup. Host attachment is refused structurally; ordinary read-only
tests require no root. No production high-PPS or long-duration soak claim is made.

Additional ABI-2 validation includes 1200 real short-lived dual-stack UDP sockets,
complete export above the old 512-cache limit, frozen generation pages, invalid
snapshot IDs, SNAT, exporter SIGKILL/reload and explicit incomplete-export failure.
1070 BPF_PROG_TEST_RUN fixtures run the actual loaded program without attachment: TCP/UDP,
VLAN, bounded IPv6 extensions, malformed/truncated lengths, fragments and 1000
invalid length fixtures. These are synthetic kernel executions, not wire traffic
or fuzz-coverage proof. See the [kernel test-run semantics](https://www.kernel.org/doc/html/latest/bpf/bpf_prog_run.html).
Hardware VLAN offload metadata has not been validated on a physical NIC.

Remaining: conntrack original/reply correlation, saturation/soak testing,
persistent operator policy recovery and a
separately reviewed production helper API for deployment beyond owned laboratories.
The generic daemon controller does not automatically activate or optimize these
flow rules. See EBPF_PERFORMANCE.md for measured scope and negative/inconclusive
results rather than assuming acceleration.
