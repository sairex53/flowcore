# Independent reproduction findings

Reviewer: repository-maintainer agent. No external reviewer or certification body participated. Fresh execution/data partitions provide reproduction evidence; they do not make this a third-party security audit.

## F1: privileged source configuration rejected

Severity: HIGH for laboratory functionality, no demonstrated privilege escalation. Root-owned installed state/config checks introduced in deployment preparation also rejected the user-owned source project's config during sudo lab tests. tests/flow_v2_integration.py failed in durable_state.read_json before testing the engine.

Fix: service_config.load explicitly trusts the source root owner only for configs resolving inside that source root. Installed mode and generic state reads retain root/daemon-UID ownership validation. Regression test fakes a different daemon UID and checks source acceptance, installed rejection and unchanged state rejection. Evidence: build/review-config-owner-test.log and build/review-flow-v2.log. Commit 84d8655. The initial failing flow-v2 log was superseded; the retained regression and corrected run establish the fix, not an archived original traceback.

Reproduce: sudo python3 -B tests/flow_v2_integration.py; python3 -B tests/deployment_test.py.

## F2: live SDK test assumed lossless flush

Severity: MEDIUM validation defect; SDK observability reliability remains PARTIAL. The clean rebuild failed intent_v2_live at dissociate_socket(cookie)==0. flush waits for queue drainage, while the bounded try-lock producer can drop a terminal event, leaving an orphan.

Fix: the test bounds abandonment to at most one for its single request, requires a matching increase in dropped-event accounting, and verifies idempotent cleanup. Existing deterministic ledger tests cover explicit abandoned starts. Runtime behavior was not changed to conceal loss. Initial failure: build/review-ctest.log; corrected full run: build/review-ctest-fixed.log. Commit 84d8655.

Reproduce: ctest --test-dir build -R intent_v2_live --output-on-failure. Long-lived high-rate orphan saturation/recovery is a release gap, not a demonstrated safe production behavior.

## F3: stale public architecture/environment claims

Severity: MEDIUM documentation integrity. ENVIRONMENT.md still said no BPF program existed, and README defaulted to root execution despite the implemented ordinary-user monitor. ARCHITECTURE.md described the original controller without identifying it as historical.

Fix: current quickstart defaults to zero-privilege monitoring; historical sections are labelled; the feature matrix and fresh report are authoritative for this review. Historical measurements remain available and are not rewritten as new evidence. Reproduce the stale claims with git show 7e801a6:README.md and git show 7e801a6:ENVIRONMENT.md; compare the current quickstart and feature matrix.

## Interpretation constraints

The held-out comparison uses a separate static pilot. Active/shadow controllers start from the configured default allocation, while the static comparator uses the selected pilot allocation. Main strategy comparisons are not pure one-feature ablations. Predictive versus predictive-shadow shares the starting policy and isolates application of predictive decisions more closely.

The frozen plan was amended before any pilot/evaluation data: the high-load window and critical-service sample count must cover the controller's source-defined sample/evaluation requirements. All versions of that preregistration remain in Git. No evaluation fitting was performed.

## F4: admission accepted unreadable TC class telemetry

Severity: HIGH for safe controller activation, no host-network modification observed. On the separate Ubuntu x86_64 runner, iproute2 6.1 accepts -j but prints HTB classes as text. The daemon accepted the new lab before counters failed with an unqualified JSON error. The failure was independently reproduced twice: GitHub runs 42325128325 and 42325363242, kernel 7.0.0-1017-azure. Helper and 100/1,000/10,000 connection fixtures passed there.

Fix: ServiceLab.counters identifies the failed TC object kind and bounds diagnostic output. Service.replace now requires successful counter readback before accepting a candidate or discarding the current lab. Regression test injects a failed readback into the admission path and checks candidate cleanup, unchanged existing lab, no watcher admission and no verified capability. No text-counter heuristic/fallback was introduced. Full HTB lab operation with that tc build remains UNSUPPORTED; ordinary-user monitoring and tested native helper operations are a narrower scope.

Reproduce: sudo FLOWCORE_TC=/usr/sbin/tc python3 -B tests/service_integration.py on the recorded x86 toolchain; python3 -B tests/service_unit.py for admission regression. Logs: build/review-x86-diagnostic/ and build/review-service-final.log. Initial failed artifacts remain on GitHub; a toolchain requirement is not a successful portability test.

## F5: legacy workload silently omitted background transfers

Severity: HIGH for measurement validity in the non-research workload path. background() dereferenced research.get when research was None; task gathering with return_exceptions=True discarded the resulting AttributeError. The privileged service test found zero background progress (build/review-service-admission.log, results/service-test-1794015811030392340).

Fix: use the documented default destination for non-research workloads, collect unexpected worker exception types into client.json, and fail the workload after preserving its request ledger. Real loopback regression checks two background streams and successful requests; fault injection verifies unexpected worker failure is surfaced. A subsequent full test exposed another input defect: an unused UDP port was incorrectly required to differ numerically from a TCP port, preventing the interactive TCP port 9002. TCP and UDP may use the same numeric port; only the two TCP listeners must differ. Fixed validation; the existing privileged service matrix exercises 9000/9002/9003. Initial second failure is preserved in build/review-service-admission-fixed.log.

Reproduce: python3 -B tests/workload_legacy_test.py; sudo python3 -B tests/service_integration.py. The held-out primary/baseline experiments use a non-null research plan and verified positive background counters, so the None-path defect does not apply to their measurements. No historical legacy-background result should be relied on without rerunning it. Worker failure propagation changes future invalid-experiment handling, not retained observations.
