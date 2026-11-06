# FLOWCORE privilege and deployment model

| Component | Required privileges |
|---|---|
| Host control daemon and rtnetlink/INET_DIAG inventory | Ordinary UID, empty effective and ambient capabilities. The packaged monitor runs as DynamicUser. `flowcore interfaces --local` needs no daemon and no capabilities. |
| SDK and application timing reporters | Ordinary UID, no capabilities. Unix peer and daemon policy determine access to registration and reports. |
| Network helper | Non-root UID with exactly CAP_NET_ADMIN effective, permitted and bounding, plus NoNewPrivileges. It has no CAP_SYS_ADMIN, CAP_NET_RAW or CAP_BPF. CAP_NET_ADMIN applies to the namespace provisioned by systemd. |
| Service manager | Trusted system service manager prepares the private network namespace and inherited descriptors. Namespace setup requires CAP_SYS_ADMIN in the owning user namespace. |
| Trusted integration runner | Explicit sudo invocation. It uses CAP_SYS_ADMIN for creating/joining test namespaces, CAP_SETUID, CAP_SETGID and CAP_SETPCAP to start fixture UIDs and set their capability sets, CAP_NET_ADMIN for its deliberate foreign-object fixture, and privileged proc access when inspecting the helper. The helper is executed after dropping UID and all other capabilities. |
| Legacy lab telemetry | INET_DIAG inside the current namespace needs no capabilities for the supported basic socket snapshot. A trusted runner needs CAP_SYS_ADMIN to enter another network namespace. The optional kernel TCP tracing collector uses CAP_BPF and CAP_PERFMON on a kernel that supports split BPF capabilities. The legacy root lab has not been decomposed into independently capability-bounded BPF, Podman and experiment services. |

## Deploy the isolated helper

The package ships `flowcore-network.socket`, `flowcore-network.service`,
`/etc/flowcore/helper.json` and a sysusers definition for
`flowcore-operator`. The socket is opt-in. Provision the group using
`systemd-sysusers`, grant it to selected users, and add their numeric UIDs
to `operators` or `readers` in the root-owned configuration. The shipped
ACL admits host root only. Restart the helper service after changing the ACL.
Activation requires systemd with `OpenFile=` support and a kernel with
network namespaces, veth and the FIFO qdisc. The service uses read-only
filesystem protection, namespace and address-family restrictions, a seccomp
denylist for unrelated syscall groups, and process, descriptor, CPU and
memory limits.

An admitted user can issue the following commands without sudo after the
administrator starts `flowcore-network.socket`. Substitute the epoch and
revision returned by the preceding command.

```sh
flowcore network status
flowcore network create --epoch "$epoch" --revision "$revision"
flowcore network queue-set --epoch "$epoch" --revision "$revision" 128
flowcore network queue-clear --epoch "$epoch" --revision "$revision"
flowcore network remove --epoch "$epoch" --revision "$revision"
```

The regular monitor remains independent. For immediate host read-only use,
run `flowcore interfaces --local`. To query a user-owned monitor, set
`FLOWCORE_RUNTIME` to a private path under your user runtime directory and
launch `flowcored --monitor-only` with that same environment. Application
processes do not join the
helper namespace automatically. The historical root laboratory commands are
for controlled experiments and are not enabled by the packaged services.

## Verification

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
python3 -B tests/install_test.py
sudo python3 -B tests/helper_integration.py
```

The privileged test launcher reproduces the separate namespace, named
activation FDs, unprivileged identities, capability bound and NoNewPrivileges. It installs
the unit’s syscall deny groups with libseccomp for the live fixture.
It exercises real veth and qdisc operations and checks that host links remain
unchanged. Staged installation verifies unit syntax. Actual activation under
PID 1, SELinux policy integration and the complete systemd seccomp/mount
sandbox require deployment testing. See [systemd.socket](https://www.freedesktop.org/software/systemd/man/latest/systemd.socket.html)
and [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#OpenFile=).

## Controlled deployment review

The read-only monitoring boundary is retained. Deployment operations do not add host network authority. Configuration reload is limited to log and sampling timing; checkpoint recovery never replays kernel object IDs or reactivates controllers. Private state uses validated regular files and fsync/rename with explicit post-commit uncertainty. Live transient systemd validation of the extracted RPM showed zero effective capabilities and recovery after SIGKILL. See RELEASE_VALIDATION.md for exact evidence and unresolved package-transaction, reboot and installed-active-helper validation limits.
