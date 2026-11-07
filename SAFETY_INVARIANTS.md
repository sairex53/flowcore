# FLOWCORE safety invariants

These properties describe configuration, authority and recovery. Latency, throughput, deadline attainment and fairness remain performance objectives. A verified HTB allocation certifies readback of its rates and ceilings at a point in time.

| Property | Runtime check or observed boundary |
| --- | --- |
| A class write requires the owned sender | The daemon checks the pinned namespace inode and device, veth ifindex and per-lab alias. It checks ownership at transaction entry, before each class write, before verification and before compensation. The lab's own holder must still be alive. |
| A class allocation must fit the parent and operator limits | The actuator checks the exact class set, positive integer rates, rate ≤ ceil ≤ parent, total rates ≤ parent, per-class operator ceilings and configured floors. Controller writes check the admitted background minimum. Reductions run before increases. |
| Precedence must be explicit | Policy resolution returns the source of each field. Effective values use built-in defaults, then global, device, interface, service, container and cgroup. Commit checks inherited bandwidth conflicts and compares the caller's expected revision. |
| Only one admitted controller writes a lab | The daemon holds exclusive runtime and state locks and dispatches requests serially. The controller requires an explicit authorized activation, live owned HTB and verified starting allocation. Shadow and observe lack write authority. The separate helper uses a per-namespace exclusive socket and authenticates peers. |
| A planned allocation grants no verified state | The controller publishes the new allocation only after class and ownership readback. A failed write clears pending, allocation and confirmation, revokes write authority and records whether compensation was verified. Link and telemetry changes invalidate the cached confirmation. |
| Failed lifecycles revoke stale ownership | Lab detach clears authority before cleanup. Candidate admission closes resources on failure. Cleanup still attempts independent releases when one fails. Helper failure revokes its queue and veth records and terminates the helper. Restart reads policy and recovery metadata and creates new lab ownership on demand. |
| An MPTCP endpoint ID requires matching identity | Update and removal compare the pinned namespace plus address, device and signal flag against the admitted endpoint. A mismatch or failed identity read drops the entry. Ambiguous cleanup is left to disposal of the isolated namespace. |

HTB checks apply to the controller transaction and the explicit research reservation step. Laboratory fault injection is a distinct authorized operation with deliberate link and capacity changes. Tenant traffic contracts remain class-wide. The model does not prove actual service shares under scheduling, borrowing or congestion.

A netlink read and a later mutation are separate operations. An actor with capability to change the same namespace can race that interval. MPTCP exposes no generation token for two indistinguishable endpoint records with the same ID, address, device and flags. The supported deployment keeps other writers out of the lab. External change detection, fail-closed behavior and disposable cleanup limit the consequences of observed drift. See the upstream [RTNL link attributes](https://github.com/torvalds/linux/blob/master/include/uapi/linux/if_link.h) and [MPTCP path manager API](https://github.com/torvalds/linux/blob/master/include/uapi/linux/mptcp.h).

The [state specification](STATE_MACHINE_SPEC.md) and [test results](MODEL_TEST_RESULTS.md) identify the bounds of the evidence.
