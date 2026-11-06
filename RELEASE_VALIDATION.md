# Controlled-deployment validation

Status: experimental; not production-ready.

Target: Fedora 45 ARM64. The safe supported operational profile is the zero-capability read-only monitor. Active host networking remains unsupported. Existing source lab tests do not certify an installed active deployment.

## Required evidence

- Build, CTest and source/installed behavior.
- Checkpoint durability and explicit uncertainty after post-rename fsync failure.
- Installed SIGTERM/SIGKILL, stale socket, partial config, reload, concurrent-client and corruption tests.
- Binary RPM/SRPM build, digest/layout/dependency checks and extracted-package runtime tests.
- Separate install/upgrade/erase file-transaction checks in an isolated RPM database.
- Unit syntax/hardening review; distinguish syntax validation from live systemd sandbox execution.

Actual results and artifact paths are appended after commands complete. No unexecuted item is marked passed.

## Remaining release blockers

No real reboot or power-loss test, no permanent installation/activation through the host package database, no installed privileged controller/helper interruption campaign, no installed BPF-failure campaign, no signed RPM, no settled project-wide license and no complete production workload soak. Disk-full and permission tests use controlled fault injection; they do not simulate every filesystem/storage failure.

The package remains a controlled local evaluation artifact. Successful package extraction is not a dependency-resolved host install, and isolated RPM transactions with scriptlets disabled do not prove live systemd upgrade semantics.


## Executed Fedora ARM64 checks

| Check | Actual outcome / evidence |
|---|---|
| Source CMake build and CTest | 26/26 passed, 16.57 seconds; build/deploy-ctest-final.log |
| State failure injection | ENOSPC before replacement preserves the old file; EIO after rename reports DurabilityUncertain and invokes the commit callback; permission/symlink rejection passed |
| Staged install | tests/install_test.py passed: zero capabilities, watchdog, metrics, systemd syntax, downstream SDK consumer and staged uninstall; build/deploy-installed.log |
| Installed lifecycle | tests/deployment_lifecycle.py passed: validated startup, SIGHUP, malformed reload, SIGKILL, stale owned socket, rapid restarts, concurrent clients, corrupt checkpoint and foreign symlink refusal; build/deploy-lifecycle.log |
| Binary RPM and SRPM | 0.2.0-2.fc45 built with BPF off and all 26 CTest targets passing inside the RPM build; build/deploy-rpmbuild.log |
| Extracted RPM runtime | packaging/verify-rpm.py passed digest/layout, real zero-capability monitor, SIGTERM cleanup and installed lifecycle tests; build/deploy-rpm-verify.log |
| Live systemd sandbox | Transient unit using the extracted RPM payload passed with DynamicUser, empty capability sets, ProtectSystem=strict, ProtectHome, private tmp/devices, AF_UNIX/AF_NETLINK restriction, watchdog and on-failure restart |
| SIGKILL under systemd | New PID, previous_unclean=true, no owned lab activation, CapEff=0000000000000000; runtime and uniquely created persistent test state removed afterward |
| RPM database transactions | BLOCKED: isolated rpm --root --initdb cannot create its transaction lock (Permission denied), including an explicitly created alternative database directory; no host database modified |

Live sandbox artifact: results/deployment-systemd-1794007516662955973.json. It uses a read-only bind of the extracted package into a private temporary namespace; it does not replace or enable the host's FLOWCORE units. tests/deployment_systemd.py is opt-in/root-only. The first harness incorrectly tried preserving a DynamicUser runtime directory across restart, which failed at systemd's RUNTIME_DIRECTORY setup on this platform. A focused /usr/bin/true probe isolated RuntimeDirectoryPreserve=restart plus ProtectSystem=strict as the failing combination. The test now uses the persistent StateDirectory pattern already used by the shipped unit, without relaxing capabilities or sandbox protections.

The first RPM build exposed Unix socket paths exceeding sockaddr_un limits in deeply nested build directories. Three test fixtures now use relative socket paths. Production runtime paths remain short, and no kernel path-limit workaround or test exclusion was introduced.

The RPM toolchain was unpacked locally under tools/rpm/root. This host lacks installed /usr/bin/rpmuncompress and RPM build scripts. The final build uses packaging/build-local-rpm.py with project-local helpers and explicit RPM 6 file-attribute macro loading. No build checks or dependency generation are disabled.

The package is unsigned; rpm -K --nosignature checks digests only. Build artifacts are under build/rpm-deploy/RPMS/aarch64 and build/rpm-deploy/SRPMS. Package config preservation, upgrade/erase scriptlets and clean host uninstall remain unverified because the isolated transaction test is blocked. tests/rpm_transactions.py preserves the intended checks and never uses the host RPM database.

## Privilege review after integration

Monitoring still has zero capabilities. New state/configuration controls do not invoke shell commands or mutate host network state. Diagnostic access follows existing peer authorization; mutating operational IPC is rejected in monitor-only mode. SIGHUP rereads only the operator-selected configuration file and permits only log/poll timing changes. Ephemeral service/workload identities and kernel object IDs are not restored as authority. The optional helper's isolated network capability boundary was retained.


Dependency validation found an additional packaging defect in the first relocated-tool build: RPM 6 had read its startup macro path before _rpmconfigdir was overridden, so the ELF attribute generators were never selected. That RPM was rejected. packaging/build-local-rpm.py now explicitly loads the relocated fileattrs, and packaging/verify-rpm.py rejects missing libc/GLIBCXX dependencies or the telemetry SONAME. The final RPM provides libflowcore_telemetry.so.1 and records versioned ELF runtime requirements. Evidence: build/deploy-rpmbuild-final.log and build/deploy-rpm-requires-final.txt.

All transient validation units were collected after testing: build/deploy-test-units.log lists zero remaining units. The isolated RPM transaction blocker remains unresolved; no SELinux, capability or host package-database restrictions were disabled to force it through.

## Committed release-candidate snapshot

Code snapshot 9f073be was rebuilt from a clean Git tree with packaging/build-local-rpm.py. Its binary RPM and SRPM passed the build checks; the extracted binary passed the updated dependency/digest verifier and installed lifecycle checks again (build/deploy-rpmbuild-committed.log, build/deploy-rpm-verify-committed.log). A second live transient sandbox run using this exact payload passed: results/deployment-systemd-1794008397001537469.json.

SHA256:
- flowcore-0.2.0-2.fc45.aarch64.rpm: b652dc7f54831680fdae8addc971c40682ac9c8353f3d2589e31ca8cb3f1ca9e
- flowcore-0.2.0-2.fc45.src.rpm: f65ab226634661105f61fce3c49bfb0eed6e6b35097c2d54b356fe0e070ac0a2

[GitHub Actions 42312841948](https://github.com/sairex53/flowcore/actions/runs/42312841948) passed for this code snapshot on the existing x86_64 unprivileged runner. It does not validate Fedora RPM transactions or privileged networking on x86_64.

The transaction blocker has supporting SELinux audit evidence: rpmdb_t was denied dac_override/dac_read_search during the project-local RPM database attempt (build/deploy-rpm-audit.log). Host SELinux enforcement was retained. This evidence is not a successful upgrade/erase test, and no automatic security-policy relaxation was added.
