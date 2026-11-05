# FLOWCORE 0.2 platform quickstart

FLOWCORE has a deployable, read-only inventory service and an explicitly
privileged adaptive laboratory. The packaged service discovers interfaces
in its own network namespace, reads routing and queue state, and exports
health. The lab daemon manages only the veth interfaces that it creates.
Host Ethernet and Wi-Fi policy profiles can be resolved and inspected.
Automatic changes to physical interfaces remain experimental work.

## Build and start

On Fedora install the C++ compiler, CMake, Ninja, Python 3.12 or later,
iproute, iproute-tc, ethtool and util-linux. In the source tree the optional
`./scripts/setup-tools.sh` obtains tc without installing a system package.

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
./build/flowcore --version
```

Run the source monitor in a private runtime directory as your ordinary user.

```sh
export FLOWCORE_RUNTIME="$PWD/build/monitor-runtime"
./build/flowcored --monitor-only
```

Use the same environment in a second terminal and run
`./build/flowcore platform interfaces`, `status` or
`metrics --prometheus`. The read-only endpoint rejects lab startup,
policy commits and request ingestion. In an installed unit the root operator
can read these endpoints without gaining write access to the monitor.

For adaptive experiments, stop the prior daemon and use the existing
privileged lab. Running the root lab grants administrator trust to the source
tree, configuration, executable and environment.

```sh
sudo ./build/flowcored
# Separate terminal:
sudo ./build/flowcore lab up
sudo ./build/flowcore platform commit config/platform-examples.json --revision 0
sudo ./build/flowcore platform resolve --device veth --interface fc0 --service api
sudo ./build/flowcore platform apply-intent --service api
sudo ./build/flowcore controller start --strategy slo
sudo ./build/flowcore policy history
sudo ./build/flowcore lab down
```

An existing policy store may have a different revision. Read
`platform policies` before committing. Changes to profile files stage a new
version in the policy store. They take effect on a lab intent only through
explicit admission. The controller performs the existing bounded and verified
HTB transaction. It starts off after any daemon restart.

## Policy precedence and device discovery

Fields resolve from built-in defaults through global, device kind, interface,
service and container overlays, in that order. The resolver reports each
field's source. A higher layer overrides only fields it specifies. The
validator rejects inherited bandwidth conflicts before committing. The store
uses an expected revision and atomic file replacement. An identical commit is
idempotent. Audit records capture peer UID, revision and time. Up to 128 audit
records and controller decisions are retained separately.

Device kinds come from rtnetlink link information, hardware type and wireless
sysfs metadata. Device names are explicit selectors only. A veth name does
not identify a container. The example profiles cover API, database, backup
and mixed services, plus an explicit Podman container overlay.

The interface view shows MTU, addresses, state, route changes, observed byte
rates, available root qdisc counters and link loss/recovery events. Utilization
uses a reported Ethernet medium speed when available. It is not utilization of
a hidden ISP bottleneck. RTT samples are used only for established TCP sockets
with an explicit bound interface index. Local interface drops and errors are
not end-to-end packet loss. Unknown fields stay null. The inventory is bounded
to 256 interfaces, 2,048 routes, 128 diagnostic sockets and 128 events.

## Experimental MPTCP and Podman

Run with the management daemon stopped.

```sh
./build/flowcore platform mptcp
sudo ./build/flowcore platform multipath-lab
sudo ./build/flowcore platform container-lab
```

The MPTCP lab uses two independent veth links between the existing two network
namespaces. Both endpoints create IPPROTO_MPTCP sockets. The server signals
a second address, and the client allows address acceptance and extra subflows.
The lab adds delay and loss on the first path, lowers the link, brings it up
and removes the impairment. The output records kernel MPTCP_INFO, subflow
addresses and TCP_INFO snapshots when supported. The address and TCP_INFO
arrays are separate reads and are not joined by array index. Existing TCP
sessions are outside this mechanism.
[Linux MPTCP sysctls](https://docs.kernel.org/networking/mptcp-sysctl.html)

The Podman lab builds a local workload image from the compiled C++ example
and its shared libraries. It uses a private container store and joins the
owned sender network namespace. Container discovery reads Podman state and
a cgroup v2 ID. The nft output rule matches that cgroup and writes the
existing HTB class priority. The test deliberately sends to the standard port
so the verified critical-class counters demonstrate the cgroup override.
It records nft packet/byte counts, class readback, SDK SLO samples and exit
state. The daemon removes its classifier on container exit or identity change.
This covers locally generated socket traffic visible to the nft socket match
inside that namespace. Forwarded traffic, rootless NAT, remote Podman,
unrelated host interfaces and automatic container enrollment are outside
the verified topology. The lab's SELinux label override is scoped to its
trusted image and local IPC mount.

## Monitoring

`flowcore metrics --prometheus` emits Prometheus text. Put it in a textfile
collector through a trusted scheduled adapter. An example Grafana dashboard
is in `monitoring/flowcore-grafana.json`. Metrics include daemon health,
policy revision, per-interface availability and rates, application SLO
windows, controller pending-probe state and reversal counts. Measured
convergence is reported by Research Lab when its observation criteria hold.
A pending-free controller is not evidence of convergence.

Interface labels are bounded by inventory size. Service/window/class labels
retain the existing observer limits. Socket IDs, PIDs, cgroup paths and
arbitrary client labels are excluded from Prometheus. The daemon writes
structured events to stdout and is compatible with journald log retention.
Decision and audit histories can be exported as JSON.

## Troubleshooting

- **Forbidden** means the daemon's effective UID or monitor role rejected the
  operation. Inspect the runtime path and start the explicitly privileged lab
  for writes.
- **Policy revision conflict** means the expected version is stale. Read the
  current policy and resolve the conflict before retrying.
- **Unavailable qdisc or classifier** means the kernel or tool cannot attach
  the requested feature. Check kernel modules, iproute-tc, nftables and the
  cgroup v2 mount. The lab preserves the kernel error and tears down its own
  resources.
- **MPTCP fallback or missing subflow** requires checking both endpoints,
  their namespace settings and routing. Link restoration does not prove
  that a primary subflow resumed carrying traffic.
- **Missing RTT or queue telemetry** means the relevant socket or qdisc did
  not supply an attributable observation. It is not zero.
- **Stale telemetry or class drift** stops adaptive actuation through the
  existing controller guard. Review readback and decision history before
  starting a new session.

See CLI.md for command groups, SECURITY.md for deployment boundaries,
RELEASE.md for the staged installation and RPM, and PLATFORM_RESULTS.md
for measured validation and remaining work.
