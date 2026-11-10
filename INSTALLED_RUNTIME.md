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
