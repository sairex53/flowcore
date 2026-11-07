# Controlled Fedora deployment

FLOWCORE is experimental and is not production-ready. The supported deployment profile is a read-only monitor on Fedora 45 ARM64. Host-wide network modification is not enabled. Root-run research commands remain explicit, isolated namespace experiments.

## Build and inspect

Build prerequisites: GCC >=13 with C++23 std::expected, CMake, Ninja, Python >=3.12, RPM build tools. Runtime dependencies include iproute, iproute-tc, util-linux and ethtool. nftables and Podman are optional lab integrations. The RPM intentionally disables BPF; QUIC is an optional project-local environment, not a default daemon dependency.

Use packaging/source-archive.sh with SOURCE_DATE_EPOCH set to the source commit timestamp, then use python3 -B packaging/build-local-rpm.py, or rpmbuild -ba packaging/flowcore.spec with an isolated _topdir on a fully provisioned build host. The spec builds binary RPM and SRPM, runs CTest and packages all native libraries. Inspect package checksums, rpm -K --nosignature, rpm -qpl and packaging/verify-rpm.py before trusting a local build.

The local RPM is unsigned and lacks a settled project-wide license. Digest checks do not authenticate its publisher. Public Fedora submission and production deployment remain blocked.

## Installation and activation

After inspecting a trusted local RPM:
```sh
sudo dnf install ./flowcore-0.2.0-2.fc45.aarch64.rpm
sudo /usr/bin/flowcored --check-config --config /etc/flowcore/service.json
sudo systemctl enable --now flowcored.service
sudo FLOWCORE_RUNTIME=/run/flowcore flowcore ops status
```

Installation deliberately does not preset, enable or start services. The standard flowcored.service uses DynamicUser, zero capabilities, read-only monitoring, private runtime/state directories and watchdog/restart limits. The separate flowcore-monitor.service uses /run/flowcore-monitor and /var/lib/flowcore-monitor/state. Usually select one monitoring profile.

The optional flowcore-network.socket/service retains its isolated private network namespace and CAP_NET_ADMIN only there. It does not gain host-network authority. Enabling this socket is a separate operator decision. Do not broaden its sandbox to make unsupported host operations work.

Configuration is /etc/flowcore/service.json, marked config(noreplace). Files must be regular, not symlinks or hardlinks, owned by root or the daemon UID, not group/world writable, and at most 64 KiB. Intermediate systemd private-state path indirection remains supported.

## Upgrade and removal

Archive /etc/flowcore and persistent state while the monitor is stopped. Review release notes, verify the new RPM and run dnf upgrade on the trusted local package. Modified configuration is preserved by RPM. Review any .rpmnew/.rpmsave files, validate configuration and check journal/ops status.

Upgrade scriptlets reload systemd definitions and try-restart only already-active monitor services. They do not activate the helper or start disabled services. Removal stops FLOWCORE units and reloads definitions; it does not delete operator state. For rollback, stop the units, install a previously validated package, restore a compatible configuration/state backup, validate and restart explicitly. Do not downgrade opaque state without checking its schema.

The scripts use explicit systemctl operations so package build does not depend on absent systemd RPM macros. See [systemd package lifecycle guidance](https://www.freedesktop.org/software/systemd/man/daemon.html). Fresh install/upgrade/erase file transactions and extracted runtime tests are separate from actual host service/reboot validation.

## Safe lab mode

Run privileged experiments separately from the monitor, with a different FLOWCORE_RUNTIME and FLOWCORE_STATE directory. Only explicitly created owned namespaces/interfaces are admitted. Avoid running experimental root daemons under the monitoring service unit. No startup checkpoint reactivates a controller, BPF program, service lease or lost network namespace.

## Review toolchain limitation

The HTB laboratory requires tc class JSON readback, verified with Fedora iproute2 7.0. The Ubuntu x86_64 review runner had iproute2 6.1, which returned text despite -j: full laboratory operation was rejected. No fallback class counters are fabricated. Read-only monitoring, native helper and held-connection scale checks have separate evidence; see FEATURE_MATRIX.md and REVIEW_FINDINGS.md.
