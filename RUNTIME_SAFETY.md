# FLOWCORE runtime safety

The default installed daemon remains read-only. Active HTB writes in
this implementation require a disposable ServiceLab, explicit operator
activation and a verified baseline. The research runner's separate
network stimuli are authorized by the research mode. Neither path
replays host traffic-control state after restart.

| Control path | Ownership and mutation boundary |
| --- | --- |
| ServiceLab and HTB | Pinned network namespace device/inode, live holders, veth ifindex and private alias. Check the HTB root, leaf qdisc identities, flower classifier rules, exact class set, priorities, rates and ceilings. Ordinary HTB writes adjust existing class attributes. |
| Privileged helper | An exclusive namespace-local socket, a witness that rules out the host namespace, matching veth indices, peer indices and alias, and an exact queue handle/kind/limit. A read of active status checks kernel ownership. Drift revokes the epoch and exits the helper without speculative cleanup. |
| Adaptive and Hybrid V3 | Daemon dispatch and its timers share one event loop. The existing mode checks keep the adaptive writer out of the Hybrid V3 experiment. Hybrid commands also compare revision, capacity and observer sample, and remain reserved for the owning runner. |
| TCP socket pacing | The application retains the original socket descriptor. Operations pin it temporarily and compare SO_COOKIE. The in-process registry rejects a second lease on the same cookie and the handle serializes changes, expiry and restoration. |
| UDP intents | An authorized application lease governs bounded, per-message priority. Ancillary data applies to an individual send. Socket timestamp configuration follows the SDK's separate lifecycle. |
| TCX and BPF | Match namespace and sender identity at startup, verify HTB layout before sending a policy, and check the TCX attachment revision in the native owner. Maps and the link are unpinned. Loss of the policy acknowledgement closes the engine and requires a fresh attachment. |
| MPTCP | The existing lab checks namespace identity and endpoint ID, address, device and flags before changes. The path-adaptation engine verifies the endpoint map and compensates only within the lab. |
| SDK registrations | Daemon leases bind peer credentials and process start ticks, with cookie observations on explicitly passed descriptors. The daemon releases expired or dead-process registrations. Socket correlation does not grant arbitrary socket actuation. |
| Persistence and experiments | Runtime/state locks admit one daemon. The experiment lock excludes concurrent lab runners. Temporary namespaces and kernel object IDs are scoped to the current process lifetime. |

Class changes have numeric bounds and preserve configured background
reservations. Existing cooldown, action budgets, oscillation and
starvation checks govern adaptive probes. Truncated socket telemetry
disables adaptive probes while permitting authorized class ceiling
enforcement from valid class readback. Fallback retains admitted
ceilings, including when telemetry is unavailable. These checks describe
configuration and intervention limits. SLOs and throughput remain
measured objectives.

The local daemon authenticates Unix peers and bounds request sizes,
parser shape and session counts. The separate helper authenticates
peer and datagram credentials, applies operation ACLs and uses native
rtnetlink with only CAP_NET_ADMIN in its isolated namespace. It has no
shell execution operation. The trusted laboratory still uses iproute2
with typed argument vectors. See [privileges](PRIVILEGES.md) and the
[threat model](THREAT_MODEL.md).

Ownership comparisons and kernel mutations are separate operations.
A privileged process sharing an owned namespace can race a readback
or deliberately reproduce an identity. The supported model excludes
other writers. The HTTP, SDK and deployment boundaries do not make a
disposable lab an adversarial multi-tenant networking sandbox.

See [recovery semantics](RECOVERY_ENGINE.md) and
[fault injection evidence](FAULT_INJECTION_RESULTS.md).
