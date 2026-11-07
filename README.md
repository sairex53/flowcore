# FLOWCORE

Experimental Linux networking research tools with a C++23 SDK/native core and a Python control daemon.

Use FLOWCORE to run isolated QoS experiments, observe Linux network state without root, and report application latency/deadlines explicitly. Adaptive, predictive, per-flow BPF, MPTCP, container and QUIC integrations remain experimental or partial. A performance advantage over tuned static QoS has not been established. The autotuner searches only owned-lab HTB reservations and writes shadow recommendations; it does not configure host interfaces.

The repository uses Apache-2.0. The two eBPF sources also offer GPLv2 for kernel loading. Build version 0.2.0 is an experimental snapshot subject to the release gates. [License](LICENSE) · [Release readiness](RELEASE_READINESS.md)

## Start with read-only monitoring

Linux, GCC >=13, CMake, Ninja and Python >=3.12:
```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=OFF
cmake --build build -j2
ctest --test-dir build --output-on-failure
./build/flowcore interfaces --local
```

Run the ordinary-user monitor in one terminal:
```sh
FLOWCORE_RUNTIME="$PWD/build/monitor-runtime" \
FLOWCORE_STATE="$PWD/build/monitor-state" \
./build/flowcored --monitor-only --config config/service.json
```

Query it in another terminal:
```sh
FLOWCORE_RUNTIME="$PWD/build/monitor-runtime" ./build/flowcore status
FLOWCORE_RUNTIME="$PWD/build/monitor-runtime" ./build/flowcore metrics --prometheus
```

Ctrl+C or SIGTERM shuts down this monitor. It creates no lab and changes no host network policy. The packaged systemd service also uses read-only mode and zero capabilities.

## Reproduce networking experiments

Privileged tests create owned namespaces/veth devices. Use a disposable Linux VM. They do not require custom kernel modules or a replacement TCP stack.

On Fedora, scripts/setup-tools.sh extracts the distribution tc tool into tools/. Alternatively set FLOWCORE_TC to an installed absolute tc path. HTB labs require actual JSON class readback: Fedora tc 7.0 passed; tc 6.1 on the x86 runner returned text and is unsupported for this lab. Optional CAKE, MPTCP, BPF and Podman need actual kernel/userspace support.

```sh
./scripts/setup-tools.sh
sudo python3 -B scripts/release_validation.py
```

This is a substantial evaluation: a separate static pilot, ten paired primary blocks and exploratory baseline/low-load/default matrices. Frozen seeds/settings are in config/release-evaluation-plan.json. No evaluation-data fitting is performed. [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) records measured effects, regressions, failures and uncertainty. [BENCHMARK_V2.md](BENCHMARK_V2.md) defines accounting.

Linux default is measured separately without a configured bottleneck; it is not a shaped FIFO baseline. Short VM trials do not establish physical-hardware performance or production reliability.

## Application cooperation

The C++23 SDK and additive C ABI let applications report actual request start/completion, timeout, cancellation and deadlines. TCP/UDP descriptor association and explicit transport/stream hints are supported. SDK delivery is bounded and best effort; inspect dropped/rejected events. Linux does not infer encrypted request deadlines or schedule arbitrary HTTP/2/HTTP/3 streams independently.

See [APPLICATION_INTEGRATION.md](APPLICATION_INTEGRATION.md), [INTENT_API.md](INTENT_API.md) and [TRANSPORT_AWARE.md](TRANSPORT_AWARE.md). QUIC uses an opt-in aioquic adapter, not a new protocol stack.

## Documentation and validation

| Topic | Reference |
|---|---|
| Verified feature scope | [FEATURE_MATRIX.md](FEATURE_MATRIX.md) |
| SLO incident investigation | [Causal diagnostics](CAUSAL_DIAGNOSTICS.md), [incident validation](DIAGNOSTIC_VALIDATION.md) |
| Network allocation | [Policy optimizer](POLICY_OPTIMIZER.md), [allocation results](OPTIMIZATION_RESULTS.md), [autotuning](AUTOTUNING.md), [experiment method](OPTIMIZATION_EXPERIMENTS.md), [autotuning results](AUTOTUNING_RESULTS.md) |
| Current architecture | [NATIVE_CORE.md](NATIVE_CORE.md), [ARCHITECTURE.md](ARCHITECTURE.md) |
| Fresh performance evidence | [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) |
| Multi-host experiments | [MULTI_HOST_LAB.md](MULTI_HOST_LAB.md) |
| Deployment and troubleshooting | [DEPLOYMENT.md](DEPLOYMENT.md), [OPERATIONS.md](OPERATIONS.md), [RECOVERY.md](RECOVERY.md) |
| Safety validation | [Invariants](SAFETY_INVARIANTS.md), [model test results](MODEL_TEST_RESULTS.md) |
| Security and privileges | [SECURITY.md](SECURITY.md), [PRIVILEGES.md](PRIVILEGES.md) |
| Release decision | [RELEASE_READINESS.md](RELEASE_READINESS.md), [RELEASE_VALIDATION.md](RELEASE_VALIDATION.md) |
| Contribution and changes | [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md) |
| Engineering priorities | [OSS_ROADMAP.md](OSS_ROADMAP.md) |

Historical result documents are retained for traceability. They are not independent reproductions. Complete local raw artifacts stay under results/ and are excluded from Git. No physical Linux machine has been validated in this review.

For directional bottleneck evidence and the opt-in IFB lab, see [Bottleneck Intelligence Engine](BOTTLENECK_ENGINE.md) and [Directional QoS](DIRECTIONAL_QOS.md).
