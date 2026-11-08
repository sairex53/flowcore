# FLOWID validation results

The experiments ran on a Fedora 45 ARM64 QEMU guest with Linux 7.2.11-300.fc45.aarch64. Raw report and command log are under results/flowid-arm64-02/ on the test VM. The report includes the executable digest and namespace topology. See [FLOWID_EXPERIMENTS.md](FLOWID_EXPERIMENTS.md) to reproduce it.

## TCP recovery

One live C++ client retained its logical session while the lab changed the underlying network and server processes. Each fault left an unacknowledged message pending. The same client drained it after recovery.

| Fault | Flush through durable ACK after restoration | Disconnect detection to authenticated resume | ACK cursor after flush |
|---|---:|---:|---:|
| Client veth down, then up | 45.25 ms | 1,103.02 ms | 2 |
| Server SIGKILL and restart at original address | 35.66 ms | 754.00 ms | 3 |
| Replacement server at new address and discovery generation | 36.14 ms | 742.70 ms | 4 |

The client made two bounded attempts during the link outage and preserved the pending message. The replacement endpoint was 198.18.21.3 in a different namespace. The server restarted with the same authoritative SQLite state. A final message brought the database to five durable commits and 37 payload bytes with association generation four and a closed tombstone. The scenario observed zero missing messages and zero duplicate demo commits after recovery. These are single scenario timings, with planned downtime included in the detection-to-resume column.

The existing FLOWCORE daemon accepted 10 telemetry records with zero SDK drops, including send outcomes and transport reports. The client saw a coalesced SLO-risk notification. Host link inventory matched before and after the run, and all three namespace holders exited.

## Steady stop-and-wait workload

Each row is one fresh logical session with 250 sequential messages of 1,024 bytes and one durable commit per message. TCP and MPTCP each acknowledged all 750 attempted messages. The MPTCP transport negotiated on both peers. These trials used a single available veth path per connection.

| Transport | Repetition | Open ms | Payload Mbit/s | Client CPU ms | Server CPU ms | Client/server peak RSS KiB |
|---|---:|---:|---:|---:|---:|---:|
| TCP | 1 | 12.56 | 1.72 | 11.35 | 120 | 25,372 / 30,756 |
| TCP | 2 | 26.47 | 1.16 | 32.56 | 260 | 25,372 / 30,756 |
| TCP | 3 | 20.25 | 1.04 | 40.64 | 350 | 25,372 / 30,756 |
| MPTCP | 1 | 19.54 | 1.46 | 16.69 | 150 | 25,372 / 30,412 |
| MPTCP | 2 | 14.27 | 0.96 | 36.60 | 320 | 25,372 / 30,412 |
| MPTCP | 3 | 17.06 | 1.16 | 31.12 | 270 | 27,424 / 30,412 |

V1 DATA plus ACK adds 32 framing bytes per message before TLS/TCP/IP overhead. That is 3.125% of a 1 KiB payload. The lab has no matched raw TLS baseline for incremental CPU, RAM or latency overhead. The three fixed-order repetitions per transport, VM scheduling variation and per-message SQLite sync cost leave the performance difference inconclusive.

## Negative cases and boundaries

The ARM64 Debug and Release client builds passed. All 41 CTest entries passed in the Debug tree. Nine focused cases also passed with the staged installed client and server. A separate CMake consumer built and ran against the installed flowcore::flowid target. The RPM specification includes the new files and dependency, but an RPM build was outside this run.

The loopback fault fixture discarded a successful commit's ACK. On resume the client observed the committed cursor and SQLite contained one state update. A separate TLS test replayed DATA verbatim, tried the old association after fencing and submitted changed bytes for an existing sequence. Only the first copy changed state.

A replacement endpoint without the session database authenticated successfully but rejected resumption. An expired token also failed. The SDK retained the original logical identity instead of silently starting over.

Physical hosts, replicated state, client process crash recovery, QUIC and sustained high-load behavior were outside this validation. The MPTCP measurement verifies negotiated transport, with no claim about multipath failover or throughput gains. The wire protocol and deployment remain experimental.
