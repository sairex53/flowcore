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
