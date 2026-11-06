# Workload and container validation

Fedora 45 ARM64. These results validate classification and lifecycle cleanup, not production host QoS or performance superiority.

## Concurrent native workloads

Command: sudo python3 -B tests/workload_integration.py

Artifact: results/workloads-1794004224998073169/summary.json. PASS.

Two separate cgroups shared one owned network namespace. Critical and background clients each sent 100 UDP datagrams of 1024 bytes. Each nftables rule observed 100 packets / 105,200 bytes. Each corresponding HTB class recorded 100 packets / 106,600 bytes, zero drops. The 1,400-byte difference per class is 14 link-layer bytes per packet included by TC and absent from the IP-level nftables counter.

Both duplicate-cgroup class attempts were rejected. After the processes exited, both attachment records became inactive and both owned nftables tables were absent. Test processes, namespaces and uniquely created cgroups were cleaned in finally blocks.

This short test does not saturate the link and does not prove minimum-share isolation under congestion.

## Podman regression

Command: sudo ./build/flowcore platform container-lab

Artifact: results/container-1794004245239646457/summary.json. Container exit status 0. Critical TC bytes: 186,762. Configured ceiling read back as 7,000 kbit/s. Final cgroup nftables counter: 260 packets / 177,908 bytes. The counters were sampled at different times and layers, so their byte totals are not claimed equal. After container exit, the classifier was inactive with container_exit_or_identity_change. Existing SDK workload SLO reporting completed.

This is one rootful container in the owned sender namespace, not a multi-container congestion or rootless enforcement test.

## Unprivileged and deterministic validation

A real ordinary-user daemon IPC smoke test passed: peer-authenticated self registration, owner-scoped list, forget and rejection of privileged workload attachment. Three new unit cases test PID-generation changes, duplicate labels, namespace migration, quotas, cross-owner rejection, cgroup inheritance and bandwidth conflicts.

CMake configure/build and all 20 CTest targets passed in 13.82 seconds before the final additive workload-attach command. Targeted checks and IPC verification are repeated for that command.

Not measured: workload throughput fairness during saturation, restart recovery during congestion, multiple simultaneous Podman containers, rootless enforcement, shared-cgroup isolation, or automatic adaptation. No claims are made for these scenarios.

Final validation after workload-attach integration: all 20 CTest targets passed in 13.70 seconds. The ordinary-user IPC test passed again. A second privileged run, results/workloads-1794004410909484156, passed using the actual daemon Service.command workload-attach path for the critical workload, including policy resolution and identity checks. Its per-class packet/byte counts matched the first run.
