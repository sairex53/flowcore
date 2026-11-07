# Safety model test results

Validation ran on Fedora 45 ARM64 with Linux `7.2.11-300.fc45.aarch64`. The ordinary test suite also runs in unprivileged GitHub CI. The trusted Linux workflow runs the separate namespace integration.

| Check | Observed coverage |
| --- | --- |
| Executable finite state exploration | 210 reachable states, 2,762 edges and 28 action labels. Every reachable state and enabled edge in the abstraction was checked. |
| HTB differential tests | 900 deterministic generated allocations and injected outcomes. The oracles checked untouched state before rejected writes, legal capacity after each successful write, verified compensation, and refusal to overwrite an unexpected external allocation. |
| Durable revision and policy precedence | 100 generated operations checked effective field provenance, revision conflicts, idempotence, serialization and restart. File sync and directory sync errors tested the boundaries before and after rename. Revision exhaustion rejects further changes. |
| Helper permissions and sequencing | 500 generated requests across root, operator, reader and unknown identities. The independent reference checked accepted state transitions, stale epochs and revisions, and exact mutation counts. |
| Service registration | 140 generated registration, owner conflict, removal and monotonic expiry steps matched an independent lease ledger. |
| Controller and endpoint rejection | Failed verification revokes active write authority and clears pending state. Endpoint tests exercise missing and reused IDs, changed addresses and wrong namespace identity. |
| Cleanup and shutdown | An injected watcher close error still permits independent lab cleanup. A checkpoint write failure before rename preserves the unclean state. |
| Live isolated Linux | Two simultaneous revision updates produced one winner. A disconnected client still committed its accepted CAS update. The second daemon was denied. SIGKILL after a real class write left no surviving holder and restarted with the durable revision, controller off and no lab. Deleting a veth during a paused collection detached it. Changing the alias on the same interface stopped the controller and preserved its class rates. Host link and qdisc identity matched at exit. |

The normal CTest suite includes actual concurrent Intent V2 SDK processes and existing quota, parser and ownership tests. Privileged regressions exercise the optimizer's real HTB transfer and restoration, the separate capability-limited helper, MPTCP endpoint readback and the daemon API. A live MPTCP test reuses an endpoint ID for another address and verifies that both update and delete preserve the replacement. The combined chaos exercise checks recovery and the controller action budget. It also exposed a v3 transition timestamp compatibility issue in the convergence report, now covered by a fixture using the kernel-confirmed timestamp.

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
python3 -B tests/safety_model.py
sudo python3 -B tests/safety_integration.py
sudo python3 -B tests/optimizer_integration.py
sudo python3 -B tests/helper_integration.py
sudo python3 -B tests/multipath_integration.py
sudo python3 -B tests/research_controller_chaos.py
```

Local logs are retained under `build/safety-*.log`. The generated workload and kernel traces remain in `results/`.

The results establish properties of a finite abstraction and selected observed runtime executions. The HTB operation can still race a privileged external writer between identity lookup and mutation. Exact MPTCP endpoint reincarnations can be indistinguishable in kernel readback. Full process and kernel refinement, every possible shutdown schedule, power-loss behavior on all file systems, data-plane scheduling guarantees and production performance remain unproven. [Runtime checks](SAFETY_INVARIANTS.md) · [State specification](STATE_MACHINE_SPEC.md)
