# Fedora SELinux validation

## Environment and result

The validation host is a disposable Fedora 45 ARM64 QEMU VM, tested on 2026-11-10. The host reported kernel 7.2.11-300.fc45.aarch64, systemd 262-3.fc45, the targeted SELinux policy, and Enforcing mode before and after the tests.

| Check | Result | Scope |
| --- | --- | --- |
| RPM build and package check | Passed | Fedora ARM64 build; BPF disabled for RPM |
| Staged install and SDK consumer | Passed | Unprivileged relocation test |
| RPM payload verification | Passed | rpm -V flowcore in the Fedora package transaction |
| Real DNF transaction | Passed | Pinned Fedora OCI image, networking disabled, stock container SELinux labeling |
| SELinux-enforcing transient systemd runtime | Passed | Actual RPM payload, transient units, separate UIDs, private helper network namespace |
| Shipped DynamicUser unit in nested OCI PID 1 | Not validated | Dynamic user setup failed in the nested container |
| Full booted installed-guest suite | Not run | No separate disposable Fedora booted guest is available |

The tested RPM SHA-256 is 69ea00f0f76ebdc8783f2cc38f02f9a8870ace81d6baad53dbe7126e26a73582. The DNF transaction ran the RPM scriptlets, installed the checked Fedora dependencies, verified the installed file manifest, and confirmed that an installed root research command is refused. The local FLOWCORE RPM is unsigned. Its digest is useful for identifying this build but does not authenticate the publisher. Fedora dependency RPM signatures were checked separately on the host.

The transient runtime test extracted the RPM into a temporary RootDirectory and ran its real executables under systemd while SELinux remained Enforcing. Observed labels included bin_t on the staged executables, etc_t on configuration, and var_run_t on test sockets. Test processes ran in the generic unconfined_service_t domain. This is evidence that the tested operations worked with enforcement enabled; it does not establish a dedicated FLOWCORE SELinux domain or MAC confinement from other unconfined services. No custom SELinux policy was installed and no enforcement setting was changed.

## Runtime checks

The transient test confirmed:
- The read-only monitor and control daemon ran with distinct non-root UIDs, NoNewPrivileges enabled, and no capabilities in effective, permitted, inheritable, ambient, or bounding sets.
- The helper ran under a distinct non-root UID with only CAP_NET_ADMIN, in a private network namespace different from the host namespace.
- Socket activation, configured SDK UID/service authorization, forged identity rejection, reader-versus-operator authorization, and concurrent helper revision checks.
- Owned veth/FIFO create, readback, clear and removal; helper SIGKILL and restart with a fresh epoch; config reload; and controlled publication.
- Symlink, traversal, and existing-target publication rejection.
- An unchanged normalized host network snapshot covering links, addresses, IPv4/IPv6 routes, and available TC qdisc/class/filter configuration.
- SELinux stayed Enforcing after cleanup.

The snapshot excludes counters and expiring route lifetimes, which can change from unrelated host activity. The fixture's staged output directory receives default_t; the service processes use the generic unconfined_service_t domain. Therefore this test must not be described as a least-privilege SELinux policy test.

The stock Fedora container transaction used container_t for its container process and container_file_t for installed package files. It validated the transaction and RPM file integrity, but it did not prove that the shipped systemd services can start in an OCI container.

## Nested container limitation

A nested PID 1 started and listened on the packaged SDK and helper socket units. The installed flowcore-monitor.service failed while systemd was creating its DynamicUser identity. The log reports repeated permission failures opening /dev/mqueue followed by “Failed to update dynamic user credentials: Device or resource busy” and a USER-step spawn failure. This is a container runtime limitation observed in this setup; the log does not establish that SELinux caused it.

The test did not remove DynamicUser, add capabilities, mount host paths, loosen SELinux, or run the container privileged to force the unit through startup. The test stopped at this boundary. Consequently, operation of the exact shipped units after a normal Fedora boot remains unverified.

## Reproduction

On the tested Fedora 45 ARM64 host, from the repository root:

~~~sh
python3 -B tests/install_test.py
sudo bash tests/installed_rpm_transient.sh   "$(realpath build/installed-rpm/RPMS/aarch64/flowcore-0.2.0-3.fc45.aarch64.rpm)"
sudo bash build/installed-oci-rpm.sh
~~~

The transient script requires sudo from an ordinary operator account, SELinux Enforcing, and inactive fixture units. It snapshots host networking before and after. The OCI transaction uses a pinned Fedora image digest and network=none. Local logs and package artifacts are under results/installed-validation and build/installed-rpm; those paths are excluded from Git.

For full installed-service validation, boot a separate disposable Fedora guest, install the RPM there, and run:

~~~sh
sudo python3 -B tests/installed_runtime_integration.py --disposable-guest
~~~

That suite requires SELinux Enforcing, a real installed RPM, inactive FLOWCORE units, and separate fixture users. It was not executed in this environment. Do not infer its result from the transient-unit or OCI checks.

[Installed runtime](INSTALLED_RUNTIME.md) ·
[Helper security](PRIVILEGED_HELPER_SECURITY.md)
