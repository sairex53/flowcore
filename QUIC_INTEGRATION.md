# Experimental aioquic integration

FLOWCORE uses aioquic 1.7.2, installed in a project-local virtual environment. No QUIC stack or in-kernel QUIC dependency was added. The adapter uses public QuicConnectionProtocol callbacks, asyncio transport socket metadata, HandshakeCompleted/ConnectionTerminated events and the QuicConnection reset_stream API supplied at protocol construction.

examples/python/quic_adapter.py retains the library connection passed to its constructor, exposes the UDP descriptor and reports a logical session through the native FLOWCORE SDK. Socket metadata alone never proves QUIC; the echo client separately requires a confirmed handshake. Cancellation requests RESET_STREAM through aioquic and transmits it; it does not implement wire framing.

The server uses ALPN flowcore-echo-v1. The test generates a short-lived local certificate/private key under a temporary result subdirectory, restricts the key to mode 0600, verifies the certificate using an explicit trust file, and removes it on exit. Certificate verification is not disabled. Private keys are never committed or written into raw results.

## Reproduce

```sh
python3 -m venv build/quic-venv
build/quic-venv/bin/pip install -r config/quic-requirements.txt
cmake -S . -B build && cmake --build build -j2
ctest --test-dir build --output-on-failure
sudo build/quic-venv/bin/python3 -B scripts/transport_suite.py
```

The pinned requirements capture the versions actually tested on Fedora 45 ARM64/Python 3.15; other Python versions/platforms have not been validated for this adapter.

The privileged suite creates disposable sender/receiver namespaces using the existing ServiceLab, starts a root-owned telemetry daemon in a private runtime, then compares TCP, negotiated single-path MPTCP and QUIC. Each workload performs 20 individual and 20 mixed-phase 4 KiB echo requests on persistent connections. Setup latency is recorded separately; no resumption/0-RTT is used. QUIC uses new streams; TCP/MPTCP reuse one byte stream. These workload differences must be considered when interpreting measurements.

Scenarios: unshaped stable, configured 0.5% random sender loss, configured 10 ms delay plus 25% reordering/50% correlation, 1 Mbit/s sender netem rate, and synchronized interface-down/up during mixed traffic. Seed 20261107 is recorded through netem readback. Only sender egress is impaired. Configured loss/reordering are not proof each short run actually experienced those events. Interface recovery restores the same veth; it does not test address migration or independent paths.

Artifacts retain per-request monotonic timestamps/outcomes, phase-specific latency/goodput, offered/completed requests, failures, 50 ms deadline attainment, handshake/negotiation confirmation, setup times, SDK event loss, observer output, qdisc snapshots and actual link-event timing. The overall goodput spans idle phases; phase-specific goodput is the valid local comparison. A failed worker invalidates the suite instead of fabricating measurements.

The runner uses one fixed-order repetition and reports INCONCLUSIVE. Twenty requests give a nearest-rank P99 equal to the maximum; independent repetitions, counterbalanced order, more requests and CPU/implementation-matched workloads are needed for performance conclusions.

Public APIs: [aioquic asyncio](https://aioquic.readthedocs.io/en/latest/asyncio.html), [QUIC events and reset_stream](https://aioquic.readthedocs.io/en/latest/quic.html).
