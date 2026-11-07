# Fleet rollout validation

Run the reproducible signed policy fixture on a Linux machine with the FLOWCORE native intent SDK, iproute2, Git, OpenSSL and Python cryptography installed.

```sh
cmake -S . -B build
cmake --build build -j2
sudo python3 -B scripts/fleet_lab.py --output results/fleet-reproduction
```

The fixture uses three sender namespaces, a router and an HTTP receiver. It uses real TLS identities, a temporary Git checkout and Ed25519 signing key, the standalone flowcored daemon and SDK on each sender, and actual HTB class readback. The router's data egress has a 6 Mbit/s cap. Control traffic uses separate veth links. Each sender runs forty HTTP requests per second for sixty seconds. No host interface is changed.

The checks cover validation and dry-run, approved shadow, canary and percentage limits, completion, stale revision rejection, coordinator restart during canary observation, a control partition during an active stage, a blocked rollback that can safely resume, and a repeated application latency guardrail failure. The fixture retains raw request records, policy source metadata, controller logs and the rollout history. The temporary Git and PKI material is removed.


## Measured fixture

The final run completed on 2026-11-07 on the Fedora 45 ARM64 QEMU VM with four vCPUs and Linux 7.2.11-300.fc45.aarch64. All 7,200 HTTP requests completed, with zero deadline misses. All three SDK clients connected. The SDK reported one dropped telemetry event. The test used live service metrics rather than injected application P99 values.

| Case | Kernel-observed caps in kbit/s (A/B/C) | Observed outcome |
|---|---|---|
| Validate, dry-run, shadow | 500 / 500 / 500 | No new cap generation. Shadow recorded observed service windows and zero violations. |
| Canary with coordinator SIGKILL and restart | 1200 / 500 / 500 | Generation 2 paused after 7.03 s total. Restart to pause took 5.99 s. |
| Approved 50% promotion | 1200 / 500 / 900 | Generation 3 paused after 5.00 s. Half of three hosts rounded up to two. |
| Approved full promotion | 1200 / 900 / 900 | Generation 4 completed after 5.15 s. Reuse of the completed policy revision was rejected. |
| Management partition during next canary | 500 / 900 / 500 | Host C fell back to 500. Rollback reported blocked after 10.21 s. Host A stayed at 500 while raising its baseline was unsafe. An HTTP probe from C returned 200. |
| Partition recovery | 1200 / 900 / 900 | Baseline was verified 2.74 s after link restoration. |
| Confirmed latency guard violation | 1200 / 900 / 900 after rollback | Two distinct API windows exceeded a deliberately impossible 1 µs limit. The final violating window reported P99 2,656 µs with 22 samples. The rollback reached verified state after 5.84 s from rollout start. |

The journal distinguishes operator approval from automatic rollback and records the reason and readback. These timings include observation windows and CLI polling on the test VM. They are neither rollout SLAs nor evidence of a performance improvement. The scope is one VM and disposable Linux namespaces.

The eleven Fleet regression cases also cover signed, tampered and unsigned bundles, invalid configuration and intermediate budget, collateral ownership, simultaneous approvals, stale revision, partial promotion, blocked rollback after a failed acknowledgement, coordinator recovery, distinct guard windows and legacy agent compatibility. The full local artifacts are under results/fleet-final-20261107/ on the VM and are ignored by Git.
