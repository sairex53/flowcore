# FLOWCORE Linux packaging v2

Status: Fedora 45 RPM transactions are exercised by the distribution workflow. This work does not make FLOWCORE production-ready or enable network control.

## Package layout

The RPM follows the configured Fedora FHS paths:

- /usr/bin: CLI, daemon launcher, and runnable examples.
- /usr/lib64 (or the RPM architecture's %{_lib}): C ABI library, static SDK, CMake package, and experiment libraries.
- /usr/share/flowcore: Python runtime, schemas, scenarios, docs, examples, and research metadata.
- /etc/flowcore: root-owned operator configuration. RPM marks service.json and helper.json as config(noreplace).
- /run/flowcore*: systemd-managed sockets and transient runtime state.
- /var/lib/flowcore*: persistent daemon state, created by systemd units rather than populated by RPM payload files.
- Journal: service logs.

The package does not install setuid files or file capabilities. The optional network helper is socket activated and has an explicit operation allowlist. Package installation does not enable or start it.

## Scriptlet behavior

%post reloads systemd's unit database only. It does not preset, enable, or start any unit.

On package upgrade %postun uses try-restart, which affects only services that were already running. On final removal %preun stops FLOWCORE units. Scriptlet errors from an unavailable service manager are tolerated; therefore a package transaction in an installroot can verify scriptlet execution and non-activation, but cannot prove an active PID 1 lifecycle.

Do not install this package to change host qdiscs. Network experiments remain explicitly isolated and operator initiated.

## Build and test

On Fedora 45, with RPM build dependencies installed:

    export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
    python3 -B packaging/build-local-rpm.py
    rpm -qp --requires build/rpm-deploy/RPMS/*/flowcore-*.rpm
    python3 -B packaging/verify-rpm.py build/rpm-deploy/RPMS/*/flowcore-*.rpm

The build script creates a Git archive, builds RPM and SRPM, runs %check, and records build inputs separately. Reproducibility jobs reuse a canonical source/build path across clean rpmbuild invocations while writing outputs to separate roots; this avoids embedding differing absolute paths in SRPM metadata and compiled clients. The result is scoped to the recorded architecture and toolchain. packaging/rpm-dnf-transactions.py is the stronger package transaction test: it uses DNF and Fedora repositories to resolve dependencies in an empty temporary installroot, then installs, upgrades, downgrades, and erases the RPM while checking config and state preservation. It does not modify the host RPM database.

## Upgrade and rollback

Keep the previous signed RPM and its provenance. Install the new package through DNF, inspect the transaction, and run:

    /usr/bin/flowcored --check-config --config /etc/flowcore/service.json
    flowcore status
    flowcore metrics --json

If the operator configuration is invalid for the new daemon, the preflight check fails before the service starts. The RPM preserves modified config(noreplace) files; it does not rewrite an operator file. To return to a prior package, use a trusted repository/archive containing that exact version, for example dnf downgrade flowcore-0.2.0-2.fc45. DNF needs the older package available in a repository or local package path.

service.json and helper.json currently use strict schema version 1. Unknown fields and unsupported versions are rejected. There is no automatic v1-to-v2 migration yet. Before introducing a new schema, ship a migration command that stages a backup, validates the migrated candidate with the target daemon, and restores the original on any failure. Until then, keep the package-owned default and operator-owned config separate and fail closed.

## Validation scope

The distribution CI builds on Fedora 45 ARM64 and x86_64, checks the C ABI, builds RPM/SRPM, resolves dependencies in a clean installroot, executes scriptlets, checks no unit activation, and tests upgrade/downgrade/removal preservation. Staged installation and a live unprivileged monitor are also tested.

The CI installroot has no active systemd PID 1. A booted-system package lifecycle, SELinux relabel after a real RPM install, and physical-host networking remain separate validation gates. An RPM transaction passing is necessary, not sufficient, for a stable release.
