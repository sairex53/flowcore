# FLOWID experiments

## Build and run the integration tests

Install a C++23 compiler, CMake, Python with SQLite and TLS support, OpenSSL development headers, and the openssl CLI. Build from the project root.

```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=OFF
cmake --build build -j2
ctest --test-dir build -R flowid_session --output-on-failure
```

Run CTest as a regular user to exercise the loopback TLS cases. With FLOWCORE_FLOWID=OFF the storage and framing tests remain available. This is an opt-in experimental feature and the existing FLOWCORE daemon starts without it.

## Run the client/server demo

The following commands create a disposable loopback configuration and temporary Ed25519 certificates. Choose a fresh work directory and an available unprivileged port. Run as a regular user.

```sh
mkdir -p results
mkdir -m 700 results/flowid-demo
PYTHONPATH=scripts python3 -B - <<'PY'
import json
from pathlib import Path
from flowid_lab_support import endpoint_file, pki, settings
work = Path('results/flowid-demo').resolve()
config = settings(work, pki(work))
config['listen']['port'] = 9443
(work/'server.json').write_text(json.dumps(config))
endpoint_file(work/'endpoints', 9443)
PY

python3 -B scripts/flowid_server.py results/flowid-demo/server.json \
  --state results/flowid-demo/state
```

From another terminal in the project root, run the client. The first three operations send a message, intentionally establish a new transport, and confirm the session cursor and termination.

```sh
printf 'send first\nrecover\nstate\nclose\n' |
  ./build/flowid-client --service payments.test \
  --directory results/flowid-demo/endpoints --ca results/flowid-demo/ca.pem \
  --cert results/flowid-demo/client.pem --key results/flowid-demo/client.key
```

Use --flowcore-socket with an accessible local FLOWCORE intent IPC socket to enable telemetry. The demo registers an ordinary service intent, declares the logical session and selected underlay kind, submits send-to-durable-ACK request duration and outcome, and reports degraded and recovered transport state. FLOWCORE's coalesced notification bits appear in output. Telemetry is optional and has no authority to pick a new endpoint or alter the network. This demo associates observations at service level and does not claim per-message kernel timing or per-socket attribution through a reconnect.

## Namespace recovery and performance lab

Run with sudo from a regular Linux account. The existing namespace harness owns all three namespaces and their veth links. Server, client and FLOWCORE daemon run under the regular account. Use a fresh output directory for each run.

```sh
sudo python3 -B scripts/flowid_lab.py --output results/flowid-reproduction --mptcp
```

1. Send an initial message over TCP, confirm the client veth is down, try one new message and verify a retry still leaves it pending. Confirm the link is back and drain the same outbox.
2. Kill the server with SIGKILL, queue another message through a failed send, and restart at the same address with the same durable state. Check that the same logical session commits the message.
3. Remove the second server's initial IP and assign a new IP on the other link. Move the server there and atomically publish a new discovery generation. Verify recovery targets the new IP and the server's durable cursor, counters and close tombstone match expectations.
4. Open fresh sessions for a stop-and-wait workload of 250 messages of 1 KiB per repetition. TCP runs first. The optional MPTCP series explicitly requests MPTCP and checks the negotiated Linux socket option on both peers.
5. Compare the host link inventory before and after. Verify namespace holders exited. Keep report.json, the command log and stderr files under the output directory. Test TLS material and the temporary SQLite state are removed on normal cleanup.

The report records monotonic phase offsets, client-observed reconnect intervals, flush timings, endpoint addresses, actual link flags, acknowledged counts, CPU time and peak RSS. Flush time starts after the harness issues the client command. Reconnect intervals start when the SDK detects disconnection and include any time the operator or harness leaves the transport unavailable. Both clocks run in the same VM. The tests do not replay Linux scheduling deterministically.

The workload includes a durable commit per message. The throughput figure is acknowledged payload divided by wall time, while the per-message protocol overhead is 32 bytes above the TLS stream (DATA plus ACK). TLS/TCP/IP overhead, fsync cost and scheduling effects are distinct. Server CPU is derived from /proc ticks and has coarse resolution for short runs. Peak RSS is a process high-water mark, so server RSS can carry over across repetitions. See [FLOWID_RESULTS.md](FLOWID_RESULTS.md) for the recorded measurements.
