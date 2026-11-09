# Safe Online Experimentation Engine

FLOWCORE runs class strategy previews in the daemon and socket strategy previews in the instrumented Research Lab workload. The supervisor authorizes one expiring class canary on the lab's owned HTB egress interface. The existing controller and TC transaction validate, apply and read back class changes.

| Strategy | Planner | Actuator domain |
|---|---|---|
| static | Fixed HTB reservations and authorized class bounds | Class baseline |
| threshold | Legacy class threshold controller | Owned HTB class |
| queue-aware | Queue pressure and measured service demand | Owned HTB class |
| slo-aware | Queue evidence and application SLO deviation | Owned HTB class |
| predictive | Short horizon queue forecast with reactive fallback | Owned HTB class |
| socket-pacing | Native hybrid pacing | Application-owned TCP socket |
| ecn-aware | Native pacing with qualified ECN evidence | Application-owned TCP socket |
| deadline-aware | Native pacing with cooperative deadline evidence | Application-owned TCP socket |

The registry probes local planners and socket options. Socket sessions require an owned cohort, finite pacing caps, fresh feedback and matching ECN or deadline data. The corresponding backends check negotiation and signal quality.

## Observation and proposal contract

Each shadow entry identifies the strategy, actuator domain, evidence kind, action, proposal, expected direction, constraints and prerequisites. Class previews share one observer window with application reports, utilization and backlog. Socket previews share the current cap, queue, TCP and application observations in the same JSONL row. A missing prerequisite produces a hold with a reason. Separate C++ planners retain observation history without consuming the active controller's write budget or evaluating imaginary outcomes.

Shadow entries use the evidence kind `prediction`. The canary records activation as `same_run_observation`. Randomized paired runs provide evidence for comparative recommendations. The live guard baseline comes from a different interval and can be affected by shifts in workload.

## Class canary

1. A privileged operator authorizes one strategy and an 8–45 second lease for `owned-fc0`. The research daemon checks peer authorization, interface ownership, scheduler, installed allocation and the protected background share.
2. `BASELINE` waits for a complete window that passes guard checks. Activation additionally requires backlog and at least 75% root utilization. The canary requires an idle controller.
3. `CANARY` permits the bounded controller to act. The scope pins the lab, observation generation, capacity and intent revision. The guards track application failures, reported deadlines, worst eligible service P99 and observed background throughput.
4. A hard guard violation, expired lease, invalid telemetry, scheduled network transition or manual rollback ends the test. A relative P99 violation requires two consecutive qualified windows. Two incomparable windows also end the test. Recovery imposes five seconds of dwell before rearming.
5. The supervisor verifies the original allocation through the owned TC backend. If ownership or readback fails, it stops the controller's writes and reports `FAULTED` with unverified restoration. Recreate the owned lab to clear this state. Emergency stop can tear down the owned lab.

The configured background floor is an HTB reservation. The observed background goodput guard also depends on offered demand. The class transaction enforces authorized rate and ceiling limits. [Upstream HTB semantics](https://github.com/iproute2/iproute2/blob/main/man/man8/tc-htb.8)

## Operator CLI

From a built checkout, start `sudo ./build/flowcored --research --config config/service.json` in a dedicated lab. Run `sudo ./build/flowcore lab up` in a second terminal. Stop the manual daemon before using Research Lab replay, which manages its own daemon.

~~~sh
./build/flowcore strategy list
sudo ./build/flowcore strategy inspect
sudo ./build/flowcore strategy shadow static threshold slo-aware predictive --seconds 30
sudo ./build/flowcore strategy shadow
~~~

Save the `online.guards` object from [strategy-holdout.json](scenarios/strategy-holdout.json) as `guards.json`. The privileged CLI requires a root-owned regular file for the guards. Start an application reporting workload with background traffic before arming.

~~~sh
sudo install -m 0600 -o root -g root guards.json results/strategy-guards.json
sudo ./build/flowcore strategy activate queue-aware \
  --scope owned-fc0 --seconds 20 --guards results/strategy-guards.json --confirm
sudo ./build/flowcore strategy rollback
~~~

`strategy activate auto` also takes `--summary` and `--context`. It selects from available class strategies using paired evidence, then checks the initial lab network profile. A static recommendation leaves the canary unarmed. The daemon evaluates actual pressure and runtime guards before switching. The context describes the operator's workload. Socket strategies run through the application-owned Research Lab profiles.

The supervisor is experimental. A daemon restart clears in-memory canary authority. Lab teardown closes the namespace owners and records teardown separately from a verified restore.
