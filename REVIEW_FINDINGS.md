# Independent reproduction findings

Reviewer: repository-maintainer agent. No external reviewer or certification body participated. Fresh execution/data partitions provide reproduction evidence; they do not make this a third-party security audit.

## F1: privileged source configuration rejected

Severity: HIGH for laboratory functionality, no demonstrated privilege escalation. Root-owned installed state/config checks introduced in deployment preparation also rejected the user-owned source project's config during sudo lab tests. tests/flow_v2_integration.py failed in durable_state.read_json before testing the engine.

Fix: service_config.load explicitly trusts the source root owner only for configs resolving inside that source root. Installed mode and generic state reads retain root/daemon-UID ownership validation. Regression test fakes a different daemon UID and checks source acceptance, installed rejection and unchanged state rejection. Evidence: build/review-config-owner-test.log and build/review-flow-v2.log. Commit 84d8655.

Reproduce: sudo python3 -B tests/flow_v2_integration.py; python3 -B tests/deployment_test.py.

## F2: live SDK test assumed lossless flush

Severity: MEDIUM validation defect; SDK observability reliability remains PARTIAL. The clean rebuild failed intent_v2_live at dissociate_socket(cookie)==0. flush waits for queue drainage, while the bounded try-lock producer can drop a terminal event, leaving an orphan.

Fix: the test bounds abandonment to at most one for its single request, requires a matching increase in dropped-event accounting, and verifies idempotent cleanup. Existing deterministic ledger tests cover explicit abandoned starts. Runtime behavior was not changed to conceal loss. Initial failure: build/review-ctest.log; corrected full run: build/review-ctest-fixed.log. Commit 84d8655.

Reproduce: ctest --test-dir build -R intent_v2_live --output-on-failure. Long-lived high-rate orphan saturation/recovery is a release gap, not a demonstrated safe production behavior.

## F3: stale public architecture/environment claims

Severity: MEDIUM documentation integrity. ENVIRONMENT.md still said no BPF program existed, and README defaulted to root execution despite the implemented ordinary-user monitor. ARCHITECTURE.md described the original controller without identifying it as historical.

Fix: current quickstart defaults to zero-privilege monitoring; historical sections are labelled; the feature matrix and fresh report are authoritative for this review. Historical measurements remain available and are not rewritten as new evidence.

## Interpretation constraints

The held-out comparison uses a separate static pilot. Active/shadow controllers start from the configured default allocation, while the static comparator uses the selected pilot allocation. Main strategy comparisons are not pure one-feature ablations. Predictive versus predictive-shadow shares the starting policy and isolates application of predictive decisions more closely.

The frozen plan was amended before any pilot/evaluation data: the high-load window and critical-service sample count must cover the controller's source-defined sample/evaluation requirements. All versions of that preregistration remain in Git. No evaluation fitting was performed.
