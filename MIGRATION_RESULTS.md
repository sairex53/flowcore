# Native migration measurements

## Migrated component

Opt-in C++23 histogram P50/P95/P99 summaries, reached through ABI version 1. Python still owns sample insertion, sparse merges, rolling windows, telemetry collection, daemon lifecycle, IPC, policy state, helpers and research orchestration.

## ARM64 evidence

Fedora 45 aarch64, kernel 7.2.11-300.fc45.aarch64. Fresh profile: 0.306 seconds instrumented fixture wall time; 0.091 seconds cumulative quantile work. Instrumented timing is separate from unprofiled process CPU comparisons.

Ten alternating independent process pairs against the unchanged implementation from commit 01959b2:
- Reference median aggregation CPU: 137.963704 ms.
- Native median aggregation CPU: 108.916247 ms.
- Ratio of medians: 0.7895, approximately 21% lower CPU for this synthetic aggregation fixture.

Raw artifact: results/native-reference-1794004925521682657.json. Earlier binding-versus-reference-path trial: results/native-aggregation-1794004852369941499.json. The fixture has 60 buckets, eight services, five occupied bins, and 900 window summaries. It includes Python-to-native marshalling cost. This is not a network latency result or general runtime speedup.

Three alternating ordinary-user daemon probe pairs:
| Median across runs | Python | Native |
|---|---:|---:|
| Observe IPC median | 0.504917 ms | 0.583250 ms |
| Observe IPC P99 | 2.191793 ms | 2.429585 ms |
| Idle RSS | 30,814,208 bytes | 33,312,768 bytes |

Artifact: results/native-ipc-1794004949148740094.json. These are short empty-observer runs with 100 IPC requests each. They show deployment cost and noise, not reduced tail latency. Python remains the default. Native is an explicit opt-in for aggregation-heavy workloads.

## Correctness and safety

All 23 CTest targets passed on ARM64 in 14.86 seconds, including existing SDK/IPC integration, new native concurrency tests and two-backend differential tests. The native test exercises eight concurrent callers with 10,000 evaluations each. Randomized differential tests preserve empty/overflow/merged histogram summaries and complete-window boundaries.

ASan/UBSan plus leak detection passed the native executable on ARM64 with BPF disabled. This is not a complete-runtime memory-safety audit. No runtime event loop or concurrency model was replaced.

Build/install, source/installed loading and x86_64 CI evidence are recorded below after their actual completion. Cross-compilation is not counted as runtime validation.

The committed reproduction runner completed another ten pairs: paired native/reference CPU ratio median 0.774980, range 0.711815–0.818742; predeclared maximum regression 10%, local synthetic gate PASS. Artifact: results/native-migration-final.json. The gate is based on a CPU budget, not a confidence interval.

All 23 CTest targets also passed with the daemon histogram backend explicitly set to native (14.34 seconds). Installed-mode native loading and summary computation passed under build/native-install. BPF-enabled ordinary builds and the BPF-disabled sanitizer build both succeeded.
