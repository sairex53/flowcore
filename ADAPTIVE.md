# Adaptive Intelligence Engine

The existing daemon is the single writer. The controller consumes the
observer's application and queue windows and changes only existing HTB
classes in the owned fc0 namespace. Linux TCP congestion control remains
responsible for transport behavior.

## Intent v1

Register a JSON object using `flowcore intent register FILE`. Names are
1–32 lowercase ASCII letters, digits, underscores or hyphens, starting with
a letter. Priorities map to the configured critical, interactive or standard
TCP destination-port classes. There is one intent per class and up to 64
instances of an identical intent. The registry stores the authenticated peer
UID. PID and cgroup at registration are a snapshot, not packet identity.

| Field | Unit and bounds | Contract |
|---|---|---|
| name | service label | Registered label for explicit application reports |
| priority | critical, interactive, standard | Existing HTB class and classifier selected by operator |
| target_p99_ms | 0.001–600000 ms | Best-effort objective for successful application requests |
| desired_kbit | 0–1000000 decimal kbit/s | Best-effort class throughput objective, 0 disables it |
| min_kbit | 0–1000000 decimal kbit/s | Admission checks the configured class reservation already covers the minimum |
| max_kbit | 0–1000000 decimal kbit/s | Nonzero class ceiling inside operator bounds, enforced and read back by an active controller |
| background_min_pct | 20–100% | Guard on the background class reservation, also subject to operator floor and installed baseline |
| reliability | balanced or prefer_reliable | Advisory, recorded without changing TCP or claiming packet delivery |
| deadline_ms | null or 0.001–600000 ms | Optional explicit request deadline for reporting, not packet scheduling |
| ttl_seconds | 1–86400 s | Service lease from first registration, default 300 s |
| persistent | Boolean | Persist intent and expiry, not instance tokens or PID identity |

Zero bandwidth values mean no additional constraint. The daemon rejects
unsupported keys and bounds that conflict with the active profile. Hard
bandwidth limits apply at the class aggregate. Multiple processes using one
classified port share that class. Neither a registration nor a successful
decision guarantees application latency or end-to-end goodput. Connection
reuse and encrypted streams require explicit application integration.

Temporary registrations expire or disappear when the last instance is removed.
Persistent registrations survive a daemon restart until removed or expired.
The service uses monotonic expiry while running and reconstructs remaining
time from the persisted wall clock expiry. The Unix socket requires the daemon
UID. In the privileged lab that is root. Instance tokens scope deregistration
and SDK samples but do not extend trust beyond the authenticated UID.
The prior trusted workload reporter remains compatible.

## SLO decisions and actuator

The internal strategy interface maps observed objective state and resource
pressure to a Boolean proposal. The static strategy performs only explicit
ceiling enforcement. Threshold mode permits pressure-based adaptation with
sufficient application evidence. SLO mode requires an objective outside its
deadband. Objective state reports P50/P95/P99, deadline misses, error ratio,
throughput, and insufficient data. TCP RTT is separate.

Pressure requires observed root utilization of at least 75% and nonzero
sampled queue backlog. The SLO strategy applies a 10% deadband around P99
and desired throughput, requires consecutive eligible windows, and makes
at most one small transfer per decision. A transfer moves reserved bandwidth
from background to the target without lowering background below the greater
of its operator and registered floors. It does not change ceilings except
to enforce an admitted hard limit. Interactive and standard guarantees are
not donated to other classes. Default step is 5% of capacity. The adjustment
budget, cooldown and post-adjustment observation prevent unbounded retries.
Two consecutive rejected transfers trip the oscillation guard by default.
Sustained healthy or idle windows release retained boosts to baseline while
preserving active hard ceilings.

Before writing, FLOWCORE reads the HTB root and class topology, checks capacity,
ownership, priorities and prior rates. The kernel may also report dynamic
fq_codel flow classes, which are outside the HTB ownership check. The actuator
reduces the donor before raising the recipient, then verifies rates and ceilings
in kernel readback. It does not recreate links, filters or leaf queues. A partial
failure rolls back only while the readback is consistent with its own writes.
External drift causes a refusal and a controller fault.

A trial transfer is kept only with sufficient post-action evidence of lower P99
or improved desired throughput within the latency guard, plus a background
throughput guard when sampled backlog indicates demand. Otherwise it rolls
back. These comparisons are observations across adjacent windows, and changing
demand can confound them. The decision history records proposed and previous
allocations, measured objectives, pressure, and the later keep or rollback.
History is bounded to 128 records and atomically replaced in the private runtime
directory. The verified baseline is restored on stop, intent-set changes or
telemetry failure where safe. Lab replacement detaches the old controller.

Fairness includes measured class rates, Jain index for positive-rate classes,
observed background starvation time, allocation and shaper utilization. Class
Jain is descriptive because classes have unequal demands and priorities.
The benchmark separately calculates Jain across the four equal background TCP
streams. A configured scheduler guarantee is not a measurement of goodput.

## Operator workflow

```sh
sudo ./build/flowcored --config config/service.json
# In another terminal:
sudo ./build/flowcore lab up
sudo ./build/flowcore intent register config/intent-example.json
sudo ./build/flowcore intent list
sudo ./build/flowcore intent inspect demo
sudo ./build/flowcore policy evaluate --dry-run
sudo ./build/flowcore controller shadow --strategy slo
sudo ./build/flowcore policy explain
sudo ./build/flowcore controller start --strategy slo
sudo ./build/flowcore controller status
sudo ./build/flowcore policy history
sudo ./build/flowcore controller stop
sudo ./build/flowcore intent remove demo
```

Dry-run evaluates one proposal without touching tc. Shadow records
hypothetical actions while holding the kernel allocation fixed. Stop an active
controller before switching into shadow mode. Active mode
is opt-in. `config/service.json` can specify an `adaptive` object with
window_seconds, minimum_samples, enter_windows, cooldown_seconds,
background_floor_pct, step_pct, maximum_boost_pct, maximum_actions,
recovery_windows and maximum_reversals.
The shipped defaults are applied to older configuration files.

## C++23 SDK

Link against `flowcore_sdk` and include `flowcore/client.hpp`.
`examples/sdk_request.cpp` shows an actual TCP request to the lab receiver.

```cpp
using namespace std::chrono_literals;
flowcore::Client client("/home/elliot/projects/flowcore/runtime/control.sock");
flowcore::NetworkIntent intent;
intent.name = "payment-api";
intent.target_p99 = 30ms;
auto service = client.register_service(intent);
auto request = service.begin_request();
// Execute the network request over the service's enrolled class.
bool reported = request.complete(flowcore::Outcome::Ok);
```

Service handles share immutable registration state and may be used from
multiple threads. A request guard is move-only and belongs to one caller.
It measures with a monotonic local clock and ends the sample before IPC.
Abandoned guards report failure. Registration throws on failure.
Completion returns false on rejected or unavailable IPC without changing the
network operation. Each exchange has a 250 ms deadline and no retries.
Final handle release attempts instance deregistration. For high-rate workloads
the existing asynchronous reporter provides a bounded queue and drop counts.

## Reproduce

```sh
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 -B tests/adaptive_integration.py
sudo python3 -B scripts/adaptive_experiment.py --duration 16 --repetitions 2 --rates 60 100 135 85 --tune-static
```

Stop flowcored first for privileged tests. Fresh namespace pairs isolate each
run. The Linux default veth control is unshaped and should not be used to
isolate scheduler effects. FIFO and fq_codel use a single-class HTB shaper.
CAKE has its own shaper. The static pilot tests 20%, 30% and 40% critical
reservations on separate runs and selects the lowest eligible P99. Evaluation
runs save raw application, queue and per-flow evidence. See ADAPTIVE_RESULTS.md.
