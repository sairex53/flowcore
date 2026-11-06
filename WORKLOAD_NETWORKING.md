# Workload networking

## Implemented scope

FLOWCORE now has an owner-scoped workload registry and multiple concurrent cgroup classifiers in the existing privileged research laboratory. The C++23 executable dispatches the Python daemon/orchestration layer; this change does not introduce a native C++ workload manager or host-network actuator.

Ordinary users can discover and register explicitly selected processes, local Podman containers and systemd services through their own daemon. Registration records the immutable process lifetime, actual UID, boot ID, cgroup path/inode and network namespace inode. Service names and container names are metadata, not privilege credentials. Duplicate names do not merge identities.

Commands:
```sh
./build/flowcore platform workload-register --pid 12345 --service api
./build/flowcore platform workload-register --container api-container --service api
./build/flowcore platform workload-register --unit api.service --service api
./build/flowcore platform workloads
./build/flowcore platform resolve --cgroup /system.slice/api.service --service api
./build/flowcore platform workload-forget WORKLOAD_ID
```

Non-root systemd discovery queries the user manager; root queries the system manager. MainPID and ControlGroup must agree with procfs. Podman discovery uses local inspect and records its full container ID plus the current process lifetime. Remote Podman discovery and automated fleet scanning are not implemented.

The registry is bounded to 128 total entries and 32 per owner; events are bounded to 128. Views are filtered by authenticated UID. Lifecycle refresh detects process exit, PID reuse, cgroup replacement/migration and network namespace changes, marks registrations stale and never silently transfers authority to a new process. Restart recovery currently requires explicit re-discovery and reattachment. Entries can be forgotten to release capacity.

## Operator-authorized classification

The existing daemon SO_PEERCRED authorization applies to all new operations. Registration grants no network authority. Only the privileged research daemon can classify a registered workload, and only when its process belongs to the owned sender namespace:
```sh
sudo ./build/flowcore platform workload-attach WORKLOAD_ID
sudo ./build/flowcore platform workload-classifiers
```

The selected workload must be active, owned by the caller and still match its recorded identity. Policy inheritance is built-in defaults, global, device, interface, service, container, then exact cgroup path. Provenance is returned for each field. Existing version-1 files remain valid without a cgroup section. Unknown fields, traversal paths and incompatible inherited bandwidth bounds are rejected.

Workload attachment resolves the class from this policy. Individual min/max/desired bandwidth contracts are explicitly rejected because the current backend shares one HTB class among workloads. It would be incorrect to advertise a class-wide guarantee as a per-container guarantee.

Each attachment owns a separately named nftables table with a socket-cgroupv2 output rule that sets skb priority to the configured HTB class. Random table names prevent collisions. Up to 32 attachments may coexist. Equal cgroups and ancestor/descendant overlaps are rejected, even with identical class requests, so rule order cannot silently resolve conflicts. Exit or identity change removes only that attachment's table. The latest attachment remains available through the legacy container-status API.

Only local egress from sockets created in the selected cgroup is supported. Socket creation before process migration can retain its original socket cgroup association. Forwarded bridge traffic, host ingress, rootless userspace-network helper traffic and arbitrary external namespaces are not classified by this backend. Shared network namespaces are supported when workloads have separate non-overlapping cgroups. A shared cgroup cannot provide independent workload classes.

HTB minima, ceilings and background protection remain aggregate class properties of the existing verified lab policy. The existing adaptive controller can redistribute class allocations within its constraints; there is no independently validated workload-level optimizer. SLO telemetry continues through the opt-in intent SDK and is not inferred from packet counters.

## Deployment examples

1. Podman host: run the ordinary daemon as the Podman owner, register containers by inspect, use workload views for observation. Host container QoS needs a future constrained helper backend; lab classification requires explicit owned namespace membership.
2. Multiple native services: create separate systemd units, register each --unit, configure service and exact cgroup policy overlays. Ordinary service SDK registrations continue to work without network capabilities.
3. Database + API + backup: map API to critical, database to interactive and backup to standard in platform policy; retain the configured background HTB floor. These map classes, not individual bandwidth contracts.
4. Container stack: separate container cgroups may share the owned sender namespace. Use the existing isolated Podman store and container-lab runner; do not grant containers CAP_NET_ADMIN.

For a native unit, the application can invoke the existing intent SDK itself, or an operator can discover it with --unit. User services use systemctl --user; system services require the operator's system daemon. Mutable unit labels alone never authorize a host network change.

## Validation and remaining work

```sh
python3 -B tests/workload_test.py
python3 -B tests/workload_ipc.py
sudo python3 -B tests/workload_integration.py
sudo ./build/flowcore platform container-lab
```

The new privileged test creates only disposable namespaces and two uniquely named cgroups, sends concurrent UDP traffic, checks nftables and TC packet counts, rejects conflicting registration and verifies stale-rule cleanup. Results are in CONTAINER_RESULTS.md.

Production host management, automatic container restart rebinding, rootless QoS, multi-container congestion/restart experiments, namespace-destruction recovery tests, dedicated per-workload HTB classes and workload adaptation benchmarks remain unimplemented or unvalidated. This is not native Kubernetes support. A hostile root actor changing an owned namespace concurrently is outside the lab ownership boundary.

References: [nftables socket cgroupv2](https://www.netfilter.org/projects/nftables/manpage.html), [Podman inspect](https://docs.podman.io/en/latest/markdown/podman-container-inspect.1.html).

Lifecycle observation is polling-based: transient migrations between samples may not be seen. It does not pin namespaces or cgroup lifetime through kernel handles. Registration forget removes registry metadata; classifiers remain independently owned by the lab until process exit, identity change or lab teardown. Remote Podman connection environment variables are rejected for registry discovery.
