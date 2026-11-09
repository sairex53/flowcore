# Online experiment method

The Research Lab creates separate sender and receiver namespaces, connects them by veth and verifies class hierarchy, netem and interface identity through kernel readback. Each trial uses a fresh topology. A version 2 manifest fixes the workload, delay, rate, capacity changes, guards and seed. Randomized order and common workload seeds pair the arms within independent blocks. Host noise and invalid runs stay in the evidence.

## Reproduction

~~~sh
cmake -S . -B build
cmake --build build -j3
ctest --test-dir build --output-on-failure
sudo python3 -B tests/strategy_integration.py
sudo ./build/flowcore bench replay scenarios/strategy-calibration.json
sudo ./build/flowcore bench replay scenarios/strategy-holdout.json
sudo ./build/flowcore bench replay scenarios/strategy-ecn-shadow.json
sudo ./build/flowcore bench replay scenarios/strategy-deadline-shadow.json
~~~

Holdout changes seed, rate, RTT, offered workload and capacity from calibration. Fixed static, fixed queue-aware and the canary begin with the same HTB reservations and socket observation quota. A low-load segment is followed by competing bulk flows and a rate and RTT change. The online arm arms the preregistered candidate and compares its guarded behavior with the fixed policies. A scheduled capacity change first returns the online arm to its verified baseline. The two socket runs exercise their matching telemetry feeds in single-arm checks.

Use the summary path emitted by the lab.

~~~sh
./build/flowcore strategy compare results/<experiment>/summary.json
./build/flowcore strategy compare results/<experiment>/summary.json \
  --baseline static --candidate online-select
./build/flowcore strategy recommend results/<experiment>/summary.json \
  --context scenarios/strategy-holdout.json --guards guards.json --eligible queue-aware
~~~

The report includes application P99, failures, deadline attainment and misses, total and background goodput, active bulk flow Jain index, daemon CPU and adjustments. The online arm adds switches, guard regressions and shadow planner CPU in `strategy.json`. `timeline.jsonl`, `decisions.json`, `transitions.jsonl`, request data and kernel topology snapshots show what occurred in each trial. Socket `socket-pacing.jsonl` separates proposed actions, observed cap and verified actuator results.

## Evidence and decisions

- Comparative evidence uses independent paired runs with preregistered P99, total goodput, failure and deadline noninferiority margins. Order statistic intervals adjust for the planned comparisons. Paired P99 excludes runs with failed requests and the failure gate tracks those runs. The checked-in comparisons need at least ten qualifying pairs per gated metric.
- The selector matches the declared network, workload, controller, profiles and transitions. It requires observed actuation in the candidate arm, candidate runs that satisfy operator guards and a simultaneous P99 improvement interval that clears the switching cost. It ranks qualifying candidates by the interval's upper bound, then name. Missing evidence leaves static selected.
- Live guard P99 is the maximum P99 over services with at least ten successful reports each. This can be noisy with small cohorts. Demand and the service mix can shift during a trial. Same-run guard windows provide operational measurements, and paired runs provide comparative evidence.
- Shadow output records a hypothetical action and its justification. The implementation has no off-policy outcome estimator.
- A verified early guard rollback is measured behavior of the supervised policy. A failed restoration invalidates the run. Verified baseline readback can occur after zero class changes.

Treat experiment summaries as local operator inputs. Use independent workload manifests and seeds for validation.

A useful counterfactual would estimate the unobserved result of the same workload under another decision. The current guard can compare adjacent windows and flag degradation, but shifts in demand and capacity affect that comparison. Randomized workload pairs address this at the experiment level. An observational estimator would need logged assignment probabilities and overlapping conditions for each action. The deterministic guard and single active actuator do not provide those inputs.
