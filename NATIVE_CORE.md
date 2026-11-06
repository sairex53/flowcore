# Incremental native C++23 core

## Ownership and inventory

| Boundary | Current modules | Long-term owner |
|---|---|---|
| C++ CLI dispatch, legacy controller | src/main.cpp, policy.cpp, netlink.cpp; policy.hpp | Native CLI dispatch and netlink; legacy controller remains isolated compatibility code |
| SDK and request lifecycle | client.cpp, intent_v2.cpp, intent_transport.*, intent_c.cpp; client.hpp, intent_v2.hpp, intent_c.h | Native reusable SDK, one bounded worker per client, RAII request scope |
| Deterministic controller primitives | intelligence.cpp, predictive.cpp, predictive.h | Native pure decision/constraint kernels; Python retains state machine until separately justified |
| BPF transport | observe_bpf.cpp, flow_engine.cpp; bpf/* | Native libbpf ownership and bounded transport, optional at build/runtime |
| Histogram summaries | telemetry.cpp, telemetry.hpp, native_telemetry.py | New native pure aggregation primitive, opt-in; Python compatibility implementation retained |
| Daemon lifecycle and IPC | daemon.py, intent_session.py, ipc_security.py | Python event loop and authenticated JSON IPC retained; native replacement requires a separate event/compatibility audit |
| Controllers and intent leases | adaptive.py, adaptive_tc.py, intelligence.py, predictive.py, intents.py, intent_v2.py | Python policy/state orchestration calling native pure kernels |
| Privileged helper | network_helper.py, helper_netlink.py, helper_cli.py | Existing narrow authenticated helper; native aggregation has no helper or network authority |
| Telemetry | observability.py, app_metrics.py, telemetry_windows.py, telemetry_export.py, tcp_diag.py, network_state.py, platform_network.py, flow_snapshot.py, flow_engine.py, connection_lifecycle.py | Python collection/provenance and rolling-window lifecycle; batch decoding/netlink reads are future candidates |
| Policy and discovery | service_config.py, observability_config.py, metric_catalog.py, platform_policy.py, workloads.py | Python validated configuration and identity tracking |
| Experimental orchestration | benchmark*, research_*, adaptive_experiment.py, adaptive_diagnostics.py, observer_perf.py, static_pilot.py, runtime_profile.py, native_migration_benchmark.py, flow_engine_benchmark.py, predictive_suite.py, predictive_report.py, multipath*, containers.py, service_lab.py, namespace_holder.py, workload.py, report.py | Python remains appropriate for tools, artifact generation and isolated lab lifecycle |
| CLI and paths | cli.py, platform_cli.py, research_cli.py, paths.py | Python management surface; C++ executable dispatch remains compatible |
| Build/install | CMakeLists.txt, packaging/*, scripts/flowcored*.in, build-*.sh, setup-tools.sh, bpf_source.py, check_style.py | CMake reusable native targets, systemd security profiles and Python installation maintained together |
| Verification/examples | tests/*, examples/*, scripts/integration_test.py, .github/workflows/* | Architecture-specific tests; ordinary CI separated from privileged research |

No Python daemon, helper, workload identity tracker or experimental runner was rewritten. The existing controller duplication remains an explicitly documented compatibility boundary, not a second newly introduced controller.

## First migration and justification

PERFORMANCE.md already identified aggregation as runtime work. A fresh profiled fixture showed 21,600 quantile calls consuming 0.091 seconds within a 0.306-second instrumented fixture. The selected migration batches P50/P95/P99 computation for one bounded 257-bin logarithmic histogram through a stateless C ABI.

The reusable C++ API takes a borrowed std::span of typed bins and returns std::expected<Summary, Error>. std::array and std::optional express fixed storage and absent quantiles. No heap allocation, file descriptor, thread, network syscall or shared mutable state is introduced. The C ABI validates lengths, duplicate indices, invalid totals and arithmetic overflow before writing results. Errors do not cross the ABI as C++ exceptions.

Library ABI version 1 is checked before use. Python owns the ctypes buffers through each synchronous call; the shared library is held for module lifetime. No pointer is retained after a call. Concurrent independent callers are supported by stateless code and tested with eight std::jthreads. Existing JSON keys, histogram boundaries, overflow behavior and SDK/daemon protocols are unchanged. Counters at or above 2^53 deliberately retain the Python reference path to preserve its floating-point rank behavior.

Activation:
```sh
FLOWCORE_HISTOGRAM_BACKEND=native ./build/flowcored
FLOWCORE_HISTOGRAM_BACKEND=python ./build/flowcored
```
Python remains the default because measured synthetic CPU gains came with increased idle RSS and slower empty-observe IPC. Explicit native mode fails visibly when its library/ABI is unavailable. Auto mode is available for opt-in deployments that want Python fallback when the library file is absent. It does not hide ABI mismatches.

## Runtime boundaries

The daemon owns event-loop lifetime, select readiness, bounded IPC parsing, connection admission and shutdown. Native pure components borrow snapshots and return results; they do not own the daemon. Privileged network changes still pass through existing owned-lab/helper authorization. Aggregation cannot bypass verification or trigger network changes.

Future native work should keep one event-loop owner using poll/epoll where profiling justifies it, explicit cancellation and fixed queue limits. Do not add one thread per service or request. Persistence stays with the revisioned policy store. Socket collectors, TCP_INFO decoding and netlink readers remain separate from kernel actuators.

Priorities after this migration: quantify repeated tc subprocess cost; implement one verified native counter decoder if worthwhile; consolidate native-library discovery/install conventions; only then consider daemon lifecycle or IPC migration. Moving all JSON or orchestration to C++ is not justified by these measurements.

## Build and rollback

The flowcore_telemetry target is exported as flowcore::telemetry with the SDK's CMake package. Header installation supports direct C++23 reuse. Its shared library follows the existing native intelligence loader convention under the binary directory; a broader FHS library-path migration remains separate work.

CMake option FLOWCORE_SANITIZE enables ASan/UBSan on the telemetry library and native test only. Existing BPF enablement stays independent. Sanitized shared objects are tested through a sanitized executable, rather than loaded into an unsanitized Python process.

```sh
cmake -S . -B build && cmake --build build -j2
ctest --test-dir build --output-on-failure
cmake -S . -B build-native-sanitize -DFLOWCORE_BPF=OFF -DFLOWCORE_SANITIZE=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build build-native-sanitize --target telemetry_native_test
ASAN_OPTIONS=detect_leaks=1 ctest --test-dir build-native-sanitize -R '^telemetry_native$' --output-on-failure
python3 -B scripts/native_migration_benchmark.py results/native-migration.json --pairs 10 --max-regression .10
```

The benchmark pins the prior implementation to commit 01959b2, alternates order and records independent process CPU measurements with a predeclared local budget. Its PASS gate is not statistical proof or an application network performance claim.
