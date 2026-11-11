# Installed FLOWCORE runtime

The RPM installs application files under /usr/bin, /usr/lib and
/usr/share/flowcore. Operator-owned configuration lives under
/etc/flowcore. systemd manages private runtime and state directories
and the journal. Package installation leaves service activation to
the administrator.

| Role | Entry point | Authority |
| --- | --- | --- |
| Monitoring | flowcore-monitor.service | Dynamic UID, private local socket, no capabilities |
| Application intent control | flowcored.service and flowcore-sdk.socket | Dynamic UID, authorized ephemeral SDK telemetry, no capabilities |
| SDK client | /run/flowcore-sdk/intent.sock | Member of flowcore-app and explicitly named UID for its service |
| Network operator | /run/flowcore-helper/control.sock | Member of flowcore-operator and approved in helper.json |
| Network helper | flowcore-network.service | Dynamic UID and CAP_NET_ADMIN in a systemd private network namespace |
| Installed experiment and publisher | flowcore network exercise or publish | The invoking ordinary UID; helper receives typed commands only |

The two daemon admin sockets live under /run/flowcore and
/run/flowcore-monitor with mode 0700 directories. Persistent state
uses systemd StateDirectory under /var/lib with mode 0700.
The SDK and helper sockets are root-created 0660 sequenced-packet
listeners. The daemon and helper use ProtectSystem=strict, private
temporary directories and NoNewPrivileges. The directory directives
provide the daemon's writable paths.

Configure intent_api.applications as a service-name to numeric UID
object in /etc/flowcore/service.json. The default is empty. Grant the
selected accounts membership in flowcore-app and start flowcore-sdk.socket.
A group member without an explicit mapping cannot use the protocol.
The installed role accepts standard telemetry and ephemeral leases.
It does not grant privileged classification or share admin data.

For an installed helper experiment, configure operators and readers
in root-owned /etc/flowcore/helper.json and start flowcore-network.socket.
The account must use a private mode 0700 output directory. Run the
following as that account.

~~~sh
mkdir -m 700 "$HOME/flowcore-results"
flowcore network exercise --directory "$HOME/flowcore-results" \
  --name isolated-queue.json --limit-packets 128
~~~

The exercise creates an owned veth, installs and clears a FIFO,
removes the veth, then publishes bounded verified observations.
An application can still use the read-only monitor separately.
Privileged legacy source-lab entry points are refused from an installed
prefix. Keep full research benchmarks in an administrator-controlled
disposable source environment.

[SELinux validation](SELINUX_VALIDATION.md) ·
[Helper security](PRIVILEGED_HELPER_SECURITY.md)

## Starting the installed roles

The RPM does not enable services on installation. For read-only host monitoring, start the separate monitor and point the CLI at its private runtime directory:

~~~sh
sudo systemctl enable --now flowcore-monitor.service
sudo env FLOWCORE_RUNTIME=/run/flowcore-monitor /usr/bin/flowcore ops status
sudo journalctl -u flowcore-monitor.service
~~~

Application intent is a separate service. Add only approved service-name-to-UID entries to root-owned /etc/flowcore/service.json, add those users to flowcore-app, then start flowcore-sdk.socket. The socket activates flowcored.service; start the socket rather than invoking the service without an activation descriptor. SDK clients connect to /run/flowcore-sdk/intent.sock. The control daemon accepts only standard, ephemeral telemetry leases. Changing the ACL requires a controlled service restart.

The monitor uses /run/flowcore-monitor/control.sock. The control daemon uses /run/flowcore/control.sock for its local admin API and /run/flowcore-sdk/intent.sock for applications. Run the CLI with the matching FLOWCORE_RUNTIME. The network helper remains a separate, explicit operator activation.

## Verification record

The final 0.2.0-3.fc45.aarch64 RPM was built on Fedora 45 ARM64 and exercised in three distinct ways: its RPM payload passed the staged install/SDK test, DNF installed it in a pinned Fedora container with networking disabled and rpm -V flowcore clean, and its extracted payload ran under transient systemd services while the host SELinux policy remained Enforcing. The transient test exercised distinct service and client UIDs, zero daemon capabilities, a helper with only CAP_NET_ADMIN inside a private network namespace, authenticated intent registration, helper authorization and revision races, a FIFO set/readback/clear cycle, output path attacks, reload, and SIGKILL recovery. A normalized before/after host snapshot covered links, addresses, IPv4/IPv6 routes, and available TC qdiscs, classes, and filters.

Reproduction and evidence are in tests/install_test.py, tests/installed_rpm_transient.sh, build/installed-oci-rpm.sh, and local results/installed-validation artifacts. The RPM is a local unsigned build, so verify its published digest through a trusted channel before use. These disposable checks do not establish production readiness or a dedicated SELinux domain.
