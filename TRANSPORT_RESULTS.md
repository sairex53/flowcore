# Transport integration results

Fedora 45 ARM64, kernel 7.2.11-300.fc45.aarch64, Python 3.15 and aioquic 1.7.2. These are experimental single-run functionality observations, not transport rankings or a demonstration of FLOWCORE performance gains.

The initial suite artifact results/transport-1794005535178112844 confirmed TCP, negotiated MPTCP and QUIC handshakes. Inspection found two benchmark defects: whole-session goodput included inactive gaps, and the interface fault happened after the short workload. Its interface case is invalid as a recovery experiment.

The revised artifact results/transport-1794005645163917402 adds phase-specific metrics and a fault gate. Three mixed-phase requests (one per transport) overlapped the confirmed outage. All 40 requests per transport per scenario completed, with failure ratio zero.

| Scenario, mixed phase | TCP P99 ms | MPTCP P99 ms | QUIC P99 ms |
|---|---:|---:|---:|
| Stable | 0.661293 | 0.608168 | 1.206419 |
| Configured loss | 0.451376 | 0.431126 | 1.456711 |
| Configured reordering | 17.452782 | 17.520991 | 20.719830 |
| 1 Mbit/s shaping | 41.935536 | 77.293643 | 108.402492 |
| Interface outage/recovery | 665.880856 | 665.611231 | 478.507718 |

At 1 Mbit/s, mixed-phase payload goodput was 0.314005/0.309130/0.303956 Mbit/s for TCP/MPTCP/QUIC respectively. Mixed-phase 50 ms deadline attainment was 100%/0%/0%. In the interface case it was 95% for each transport. Zero failures do not mean deadlines were satisfied.

MPTCP was positively negotiated, but the topology had one path; no multipath benefit or migration is claimed. QUIC used a verified TLS handshake and stream echo, not HTTP/3. The same process handled all transports, and their I/O implementations differ. The short fixed-order suite is INCONCLUSIVE.

SDK telemetry was lossy in some runs: the existing nonblocking SDK deliberately drops events under contention. Raw application request outcomes are recorded separately and never derived only from accepted SDK events. A complete request in the raw ledger is not proof its SDK start/end pair was accepted.

No production QUIC mobility or HTTP/2/HTTP/3 scheduling was tested.


## Final SDK stream-metadata run

Artifact: results/transport-1794005748216990511. All five scenarios completed, with 40/40 application requests completed per transport and zero application failures in each scenario. Every MPTCP workload positively reported negotiation; every QUIC workload confirmed its handshake. No fallback was accepted as MPTCP success. Client/server logs contained no traceback or assertion errors.

Mixed-phase P99 (TCP / MPTCP / QUIC), milliseconds:
- Stable: 0.391 / 0.412 / 0.852.
- Configured loss: 1.615 / 1.695 / 2.067. Netem recorded one dropped packet in the sender qdisc; configured loss alone is not treated as observed loss.
- Configured reordering: 12.328 / 12.790 / 26.739. Packet reorder events were not independently counted.
- 1 Mbit/s: 42.403 / 76.731 / 108.392; payload goodput 0.314564 / 0.309198 / 0.304561 Mbit/s.
- Interface outage: 670.714 / 670.460 / 470.350; three requests overlapped the confirmed fault.

QUIC requests used begin_stream_request with actual aioquic stream IDs and application priority 4. The daemon rejected zero queued events in all scenarios. SDK dropping remained nonzero in some runs and is preserved in raw statistics. In the loss and bandwidth cases all 82 QUIC lifecycle events, including the cancellation start/end pair, were accepted. The adapter requested RESET_STREAM for a partially written stream; wire-level reset acknowledgement was not independently measured.

All 25 CTest targets passed on ARM64 in 14.21 seconds. New tests cover transport provenance, foreign lease rejection, bounded declarations, malformed inputs, logical stream metadata and real SO_PRIORITY readback without root. Existing SDK/C ABI/IPC tests passed. Source build and installed optional worker imports/help also passed. The final code has not yet been validated as a QUIC workload on x86_64.

The suite remains INCONCLUSIVE for comparative performance: fixed order, one repetition, 20 requests per phase, one VM, different transport implementations and no confidence interval. No transport superiority or FLOWCORE latency improvement is claimed.
