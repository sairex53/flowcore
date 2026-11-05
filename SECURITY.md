# Deployment threat model

The protected assets are host network configuration, the local telemetry and
policy socket, persisted policy state and the identity of an admitted lab.
The packaged service is a read-only observer with a dynamic UID, an empty
capability set, restricted address families and namespace creation disabled.
It does not run with CAP_NET_ADMIN or CAP_SYS_ADMIN. The systemd unit imposes
memory, task, descriptor and CPU limits. Structured stdout is collected by
journald. The daemon emits readiness and watchdog datagrams.

The local socket lives in a private runtime directory. SO_PEERCRED authenticates
the peer before executing a command. The daemon UID is the writer in lab
mode. Root may read an unprivileged monitor's endpoint. Monitor-only mode has
an explicit read allowlist and rejects policy and kernel mutations. The
maximum frame is 64 KiB, with a receive deadline, bounded JSON shape, strict
operation names, no duplicate keys and no nonfinite numbers. Replies are
bounded. Tests mutate 2,000 deterministic parser inputs and send malformed
requests through an installed monitor. The design does not rate-limit a
trusted root writer and a same-UID peer can still occupy a serial handler up
to its bounded timeout.

The privileged lab currently uses a trusted Python daemon and external tools.
It is not a split-capability production actuator. Lab setup needs network
namespace privileges and CAP_NET_ADMIN, and optional BPF needs kernel BTF
and the applicable tracing privileges. Commands pass validated argv arrays.
The nft classifier validates its cgroup path and verifies the sender namespace
before a transaction. It refuses to replace an existing unknown table.
Research faults target pinned owned namespace descriptors. Daemon and
namespace holder termination release lab state, and restart does not replay
a stale kernel allocation. External processes intentionally joining a lab can
hold it alive and should be stopped before teardown.

Profile commits are staged and revision-checked. State directories must be
private and owned by the daemon. The persistent policy and audit store uses
atomic replacement after writing and flushing the file. This is not an
append-only security ledger or a power-loss durability guarantee. Live HTB
class changes have verified readback and bounded rollback. They are not a
multi-object atomic kernel transaction.

Treat the project tree, Python scripts, tool binaries and configuration as
trusted when invoking the root lab. Do not grant arbitrary users sudo access
to this tree or its experimental commands. Run only trusted Podman workloads
in the controlled integration. A shared namespace is not an isolation
boundary against malicious traffic from another member. Trusted-runner
workflows should be restricted to protected branches and configured with an
approval environment. Public pull requests run unprivileged.
