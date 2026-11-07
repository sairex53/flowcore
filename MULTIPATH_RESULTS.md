# Multipath validation results

> Historical scoped evidence. The independent maintainer review is in [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) and [FEATURE_MATRIX.md](FEATURE_MATRIX.md); older results below are not new independent validation or general performance guarantees.

Fedora 45 aarch64, kernel 7.2.11-300.fc45.aarch64, iproute2 7.0.0. Kernel and userspace path managers were listed; only the default scheduler was listed. Kernel PM was used.

Existing laboratory baseline before the extension: results/mptcp-1793976939035009914, 4,292,608 verified bytes, two subflows, no fallback, 29 completed exchanges during primary outage and 55 after recovery. This is a historical single run.

The new Linux-default primary-failover trial in results/multipath-1793978696362722086 confirmed two subflows, no TCP fallback, connection survival and zero failed requests. Application successful-request P99 was 645.797637 ms. Sixteen requests completed during the primary outage. Outage echo goodput was 1.524218 Mbps. Both veth ends reported unavailable after link-down and available after restoration. Owned endpoint IDs 2 and 1 were removed without cleanup errors; host MPTCP state before/after was equal.

This run exposed the distinction between survival and low latency. It does not establish improvement over static policies.

Two failed development runs are preserved in results/multipath-1793978178941613676 and results/multipath-1793978618400472337. The first read peer state from the wrong namespace. The next incorrectly treated peer administrative UP as path availability. The fix selects the correct interface in each namespace and requires both administrative UP and carrier. Cleanup removed owned endpoints in both failed runs.

The address snapshot after restoration is not evidence of resumed data transfer on that path. The metric is now named primary_subflow_address_reobserved_ms to make its scope explicit.

Reproduction commands and current limitations are in MULTIPATH_ENGINE.md. Raw local results are intentionally excluded from Git; reported values can be reproduced with the documented commands, subject to scheduling and packet-loss variability.

## Stable comparison

Command: sudo ./build/flowcore platform multipath-lab --compare --repetitions 1 --scenario stable --duration 6

Artifact: results/multipath-1793978864205060351. One repetition, seed 20261107; status INCONCLUSIVE. All requests completed and host MPTCP state was unchanged. All MPTCP modes confirmed two subflows without fallback.

| Mode | Successful-request P99 ms | Failure ratio |
|---|---:|---:|
| Single-path TCP | 30.680122 | 0 |
| Linux default MPTCP | 33.498629 | 0 |
| Static endpoints | 36.646090 | 0 |
| Latency preferred | 70.113637 | 0 |
| Reliability preferred | 36.900504 | 0 |
| Throughput preferred | 36.667048 | 0 |
| Experimental SLO aware | 32.292311 | 0 |

There is no demonstrated performance advantage. In particular, latency preference regressed in this single run. Probe noise, short duration, VM scheduling and kernel subflow behavior require repeated investigation before interpreting differences. The workload is not a sustained bulk-throughput benchmark.

The comparison also caught and fixed a duplicate target_latency_ms keyword when the target was included in request metrics. The failed attempt is preserved in results/multipath-1793978811109772319.

Build and validation: clean CMake configure/build succeeded, all 19 CTest targets passed (13.56 seconds), and installed-mode capability probing and lab CLI help succeeded without root. The MPTCP unit suite now has nine cases including carrier loss while administrative state remains UP.

## Active policy and overlapping failures

sudo python3 -B tests/multipath_integration.py passed using latency-preferred, three paths, eight seconds and two overlapping path outages followed by restoration.

Artifact: results/multipath-1793978939346797265. Three subflows were observed, TCP fallback was absent, the connection survived and no request failed. Successful-request P99 was 607.737022 ms. All four link events were confirmed on both veth endpoints, and all four policy updates were applied and read back. Endpoint-update application times were 20.619371, 22.619222, 17.585034 and 19.091141 ms. These times cover endpoint reconfiguration/readback, not end-to-end subflow failure detection. Sixteen requests completed during the primary outage.

The integration assertions also checked unchanged host MPTCP configuration, namespace-local MPTCP disablement preventing new sockets, and cleanup with no remaining owned endpoints or cleanup errors. This validates the controlled failure scenario, not general resilience or performance superiority.
