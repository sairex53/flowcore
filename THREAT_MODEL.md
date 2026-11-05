# FLOWCORE threat model

## Trust boundaries

The host system manager, kernel, package files, root and the administrator
who provisions numeric UIDs are trusted. Ordinary local users, SDK service
names, timing samples, request payloads and paths supplied by clients are
untrusted. The production helper is separate from the monitor and has no SDK
registration endpoint. The operator sends explicitly authorized requests
directly to the helper. Registering or spoofing a service name in daemon IPC
cannot acquire an operator UID or submit a helper mutation.

The system manager owns the pathname socket and controls its group and mode.
The helper compares SO_PEERCRED with kernel per-message SCM_CREDENTIALS.
Reader UIDs may query status. Operator UIDs and host root may also mutate.
Group membership permits opening the endpoint, subject to the additional UID
check. A connected FD sent to another PID or UID does not confer authorization.
Operators are trusted for the admitted changes, including queue limits that
may drop traffic in the private lab.

The packaged helper reads one fixed configuration under `/etc/flowcore`.
It traverses directory descriptors with no-follow semantics, checks each
parent for root ownership and rejects group/world write access. The leaf
must be a root-owned regular file with one hard link and no group/world
write permission. The versioned schema permits only numeric reader and
operator lists. The test launcher passes an already-open test fixture with
the same leaf checks. The packaged unit supplies no fixture FD.

## Protocol and ownership

- Version 1 uses one JSON message per Unix seqpacket connection. The request
  maximum is 2 KiB. Duplicate keys, non-finite numbers, unknown fields,
  unsupported operations and ancillary descriptors are rejected. Replies
  contain a version and a structured result or error code.
- The listener backlog is eight. The helper processes one connection at a
  time, bounds receive and send by 250 ms each, bounds netlink reads, and
  admits at most one mutation attempt per 50 ms. Journald records validated
  mutation attempts, commits and failures with peer UID and revision.
  A hostile process with socket access can still reduce availability.
- Each process uses a fresh epoch and an optimistic revision. Every mutation
  requires both. A namespace-local abstract socket serializes helper
  instances. The host namespace FD is an identity witness and is closed
  before activation. The process owns no API to enter another namespace.
- The helper starts only with loopback visible. It exclusively creates
  `fch0` and `fch1` in the same private namespace and verifies mutual peer
  indices, veth type and generation-specific link aliases. Before changing
  or removing a queue or veth it checks both link identities and refuses
  unexpected qdiscs. It accepts the kernel's default noqueue and its own
  fixed-handle FIFO with matching limit. Kernel readback confirms every
  committed change.
- Filter, nftables and MPTCP endpoint state is outside the helper's
  allowlist. Their legacy test paths still use dedicated lab namespaces.
  Extending the helper requires separately defining ownership and teardown
  for each new object. The helper supports no physical-interface mutation.

## Failure behavior

Malformed, unauthorized, stale or rate-limited requests have no effect.
Unexpected topology, a kernel error or inconsistent readback fails closed:
the helper reports failure and exits. It makes no speculative delete.
The private namespace and its veth state disappear when their final kernel
reference is released. The systemd unit has no restart or replay policy.
Socket activation after failure starts with a new epoch and an empty lab.
Clients reconcile a missing reply with status because a request admitted
before disconnect can still commit.

SIGTERM stops admission and lets an in-flight bounded operation finish.
SIGKILL or a system restart relies on kernel namespace lifetime. The monitor
and application clients remain usable independently of helper failure.
A trusted administrator that joins or pins the private namespace can retain
it beyond helper exit. A host administrator with concurrent NET_ADMIN can
also race ownership readback and kernel updates. Root and hostile kernel
behavior are outside this boundary.

The kernel audit/journal storage and retention policy are administrative.
The service does not provide tamper-proof audit retention or a complete
sandbox against a compromised kernel or host root.
