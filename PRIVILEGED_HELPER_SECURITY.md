# Privileged helper security

The helper accepts a bounded version 1 SOCK_SEQPACKET request.
The systemd listener admits members of the operator group, then
SO_PEERCRED and per-message SCM_CREDENTIALS identify the process.
The root-owned configuration allowlist grants status access to
readers and typed mutation access to operators. Each mutation
requires the current random epoch and expected revision.

The helper receives a network namespace witness and refuses to
operate in the witness namespace. Its private network must start
with loopback alone. The helper records and checks owned link
identity, peer linkage, alias and queue properties before changes.
Native rtnetlink applies the fixed operations. Verified results
advance the revision. Ambiguous or foreign state quarantines the
scope. Restart destroys the old namespace and allocates a new epoch.

The helper receives no output path. The authenticated response
is the handoff to the ordinary operator. The publisher walks the
destination without following symlinks, pins the checked directory,
and requires caller ownership and mode 0700. A bounded private
temporary file is linked into the destination without replacement.
Existing entries and race-created targets survive. The publisher
runs with the ordinary user's permissions.

Control daemon SDK authorization is separate from the helper.
Application telemetry does not authorize namespace, qdisc or policy
operations. The helper receives only the named production activation
descriptors in installed mode. It starts with a non-root UID,
NoNewPrivileges and CAP_NET_ADMIN as its sole capability.

[Installed runtime](INSTALLED_RUNTIME.md) ·
[SELinux validation](SELINUX_VALIDATION.md)

## Installed RPM adversarial checks

tests/installed_rpm_transient.sh runs as root on the disposable Fedora host, extracts the built RPM into a private RootDirectory, and creates transient systemd socket and service units using the package's actual executables and configuration. It requires the host's SELinux mode to be Enforcing and does not alter enforcement or install policy.

The recorded run verified:
- The monitoring and control daemons have distinct non-root identities, NoNewPrivileges=1, and empty effective, permitted, inheritable, ambient, and bounding capability sets.
- The helper has a distinct non-root identity, all capability masks limited to CAP_NET_ADMIN, and a network namespace distinct from the host namespace.
- Authenticated application registration succeeds only for the configured numeric UID and service name. A forged service and an unlisted UID are rejected. Parallel registrations are bounded.
- A read-only helper identity can query status but cannot mutate. A supplied namespace identity is rejected. Two requests racing against the same helper epoch and revision produce at most one successful state transition.
- A helper-owned veth and FIFO qdisc are created, read back, cleared, and removed. After a helper SIGKILL, activation creates a new epoch with no stale lab.
- Publication rejects symlinked directories, traversal names, and an existing destination. A successful file is owned by the unprivileged operator with mode 0600.
- A normalized host snapshot of links, addresses, IPv4/IPv6 routes, and available TC qdisc/class/filter configuration is unchanged after the test. Dynamic counters and route lifetimes are excluded from the comparison.
- An ordinary privileged FLOWCORE research entry point is refused in the installed root context.

The successful run is an installed-payload test with transient systemd units, not a booted installation of the shipped units. Systemd and SELinux observations are recorded in [SELINUX_VALIDATION.md](SELINUX_VALIDATION.md).

### Remaining security limits

The helper is constrained by its typed protocol, peer credentials, per-operation allowlist, private network namespace, ownership checks, generation/revision compare-and-swap, systemd sandbox, and capability boundary. The test processes still ran in the generic unconfined_service_t domain. This does not prove MAC isolation between FLOWCORE and other services. A production SELinux policy module and a full native-boot validation have not been established. Local root and administrators who can edit the root-owned allowlist remain trusted.
