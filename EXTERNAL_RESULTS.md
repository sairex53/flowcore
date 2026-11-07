# External validation results

## Available equipment

Validation on 2026-11-07 used one Fedora 45 Server ARM64 QEMU VM with four vCPUs and kernel 7.2.11-300.fc45.aarch64. The Linux management NIC was virtio_net with MTU 1500 and one combined queue. The driver reported unknown link speed. TSO, GSO and TX checksum offload were off, GRO and RX checksum offload were on. Ethtool reported software timestamping and no PTP clock. This NIC served management access. Experimental packets stayed on dedicated veth links. No offload or IRQ setting was changed.

| Environment | Status |
|---|---|
| Fedora ARM64 VM, four routed namespaces | Executed with real Linux queues, two HTTP services and a separate bulk generator process |
| SSH worker on the same ARM64 VM | Executed with an isolated OpenSSH server and separate endpoint namespaces |
| Independently booted Linux hosts | Not executed. One Linux boot was available. |
| Linux x86_64 or a different kernel | Not executed. |
| Physical Linux NIC or switch | Hardware validation not executed. |

The shared boot identity and four distinct network namespace identities are in the inventory. These experiments provide no VM versus physical performance comparison.

## Routed VM matrix

The fixture ran 12 forward trials across six modes with two paired repetitions and randomized order. It also ran a reverse static trial. All 13 were measured and all bulk transfers completed. Forward traffic consisted of 315 API requests and 210 interactive requests per trial, plus a 2 MiB upload from a third endpoint through a routed 4 Mbit/s egress. The reverse trial sent a 2 MiB download with response traffic classified by source port. The separate return egress received 12 ms of delay and 0.2% seeded random loss during each trial. The kernel reported the requested parameters. The unshaped control retained this fault and had no 4 Mbit/s shaper.

P99 is computed over completed requests. The table lists each run's P99 in seed order (4711, 4712). Errors and deadline misses are totals across the pair. A request can count in both columns.

| Mode | API P99 ms | API errors / misses (630 attempts) | Interactive P99 ms | Interactive errors / misses (420 attempts) |
|---|---:|---:|---:|---:|
| Unshaped | 33.76 / 33.46 | 0 / 0 | 32.37 / 34.52 | 1 / 1 |
| HTB + fq_codel | 113.61 / 105.09 | 0 / 9 | 77.12 / 104.48 | 1 / 5 |
| CAKE | 35.77 / 68.74 | 1 / 1 | 36.97 / 35.26 | 0 / 0 |
| Static HTB | 41.29 / 31.63 | 0 / 0 | 35.76 / 33.93 | 1 / 1 |
| FLOWCORE queue-aware | 35.22 / 69.84 | 1 / 1 | 33.14 / 34.44 | 0 / 0 |
| FLOWCORE hybrid | 34.31 / 68.81 | 1 / 1 | 34.05 / 32.57 | 0 / 0 |

HTTP preflight traffic incremented both expected class counters in each classified run. A representative forward static trial recorded 105,983 critical bytes, 71,018 interactive bytes and 2,193,416 background bytes in the shaped direction. The shaped transfer goodput was 3.576–3.630 Mbit/s. The short unshaped transfers reached 316–900 Mbit/s on the VM veths. These figures are application goodput for this workload. The reverse static smoke trial measured 31.15 ms API P99, 32.39 ms interactive P99, 3.625 Mbit/s bulk goodput, and positive counters for each expected class.

Both FLOWCORE strategies held their starting allocations in both repetitions. Their observed P99 differences therefore provide no measured effect of policy changes. Two pairs on one VM are insufficient for estimating uncertainty or generalizing a ranking. In a separate one-repeat fixture, a deliberately supplied HTB adjustment passed actuator readback. The critical reservation moved from 1,600 to 1,800 kbit/s and the background reservation from 1,200 to 1,000 kbit/s. The test also rejected a proposed update outside the action budget and verified queue teardown. This exercises the actuator without making a claim about live controller convergence.

## Transport and timing checks

The SSH fixture authenticated with temporary keys and pinned the temporary server host key. The server worker ran through the normal SSH transport. The client completed 36 API requests, 24 interactive requests, and a 128 KiB upload, with zero request errors. This was an unshaped transport smoke test across two veth-connected namespaces on the same VM.

In the four-role matrix, the 1 ms sleep probe observed approximately 25 µs to 5.63 ms of overshoot. Local worker clock exchanges took about 31–135 µs. In the separate SSH smoke the three server exchanges took about 96–145 µs. These are short process and control-channel probes. Request and bulk durations used local monotonic clocks. No packet hardware timestamps or synchronized cross-host timestamps were used.

## Reproduction

```sh
sudo bash tests/multi_host_namespaces.sh results/multi-host-reproduction
sudo bash tests/multi_host_ssh.sh results/multi-host-ssh-reproduction
```

The SSH fixture requires OpenSSH server and client tools and a nonroot login account. For a real deployment follow [MULTI_HOST_LAB.md](MULTI_HOST_LAB.md) and record the topology and link conditions in [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md).

Raw requests, snapshots, manifests, trial logs and order are preserved on the VM under `results/multi-host-vm-final-20261107/`. The verified transaction and additional smoke trials are under `results/multi-host-actuation-20261107/`, and the SSH smoke is under `results/multi-host-ssh-20261107/`. Git ignores these machine-local artifacts.
