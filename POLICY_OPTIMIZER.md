# Network Policy Optimization Engine

The optimizer is opt-in for the owned Linux HTB egress lab. The daemon prepares the model from its authorized registry and measured telemetry. A bounded C++23 solver returns candidate rates. The existing controller remains responsible for safety gates, kernel writes, post-change verification and rollback.

```sh
flowcore policy optimize
flowcore policy optimize --algorithm max-min
flowcore policy optimize --algorithm proportional
flowcore policy optimize --algorithm static
flowcore controller shadow --strategy optimizer
```

Use `flowcore controller start --strategy optimizer` for active experiments on a privileged owned lab. The four preview commands read and validate actual HTB state and always perform a dry run. The monitor and unsupported schedulers report a structured rejection. Only the adaptive algorithm is wired to the actuator. [CLI](CLI.md)

## Resource model

| Symbol | Meaning and unit |
| --- | --- |
| \(C\) | Verified parent shaping rate in integer decimal kbit/s, from 100 to 1,000,000. One kbit/s equals 125 bytes/s. |
| \(x_i\), \(b_i\), \(r_i\) | Proposed, authorized baseline, and currently verified guarantee for class \(i\). |
| \(l_i\), \(u_i\), \(c_i\) | Hard lower guarantee, upper guarantee, and kernel HTB ceiling for a class. |
| \(d_i\) | Demand proxy, bounded by parent capacity. It uses observed class delivery, the queued-byte equivalent over the observation window, desired service throughput and a bounded step of exploration under sustained pressure. |
| \(p_i\), \(w_i\) | Operator class priority weight (4, 2, 1, 1) and the adaptive utility weight. Class order is critical, interactive, standard, background. |
| \(S\), \(m\) | Maximum per-action transfer and minimum useful step in kbit/s. |
| Lease | Existing peer-authorized identity and monotonic expiry from the daemon registry. Ownership remains attached to the pinned lab and verified egress topology. |

Each solution must satisfy \(0 < l_i \le x_i \le u_i \le c_i \le C\) and \(\sum_i x_i \le C\). The foreground floors keep the operator baseline and check the sum of admitted minima for each class. The background floor is the highest admitted or configured protected share, rounded down to a whole kbit/s. Foreground guarantees can rise at most to the operator boost budget or the effective ceiling, whichever is lower. The background guarantee can donate surplus above its floor and is capped at its baseline. Shared-class ceilings take the tightest admitted cap. HTB enforces these settings on the class aggregate. Services sharing a class have no independent per-service guarantees or caps.

Rate describes a reservation. A class may borrow up to ceil, while the parent controls total departures. Ceilings can sum to more than the parent capacity. A demand proxy describes observed and requested local pressure. End-to-end P99, desired throughput and deadlines remain soft objectives. Oversubscribed soft demand is reported independently of hard feasibility. [Linux HTB manual](https://github.com/iproute2/iproute2/blob/main/man/man8/tc-htb.8)

## Algorithms and objective

- **Static** returns the authorized baseline, including effective ceilings.
- **Weighted max-min** progressively allocates surplus above protected floors in proportion to priority weights, saturating classes at their estimated demand or guarantee limit. The remaining fractional shares use largest-remainder rounding in fixed class order.
- **Proportional** uses the same bounded water fill with weights equal to priority times capped demand headroom. It favors classes with more room to use additional reservation.
- **Adaptive** maximizes a fixed-point concave surrogate over hold and all admissible single transfers. The donor can be a class above its floor or unused parent budget. The receiver needs fresh local pressure, class evidence and enough successful application samples for evaluation.

Let \(z_i=\max(0,\min(x_i,d_i)-l_i)\). The integer score is

```text
sum_i [ w_i * z_i * (2*C - z_i) - 40*C*abs(x_i - r_i) ]
```

The useful reservation term rewards throughput demand with diminishing marginal utility. The base weight is 100 times class priority. The service adjustment adds up to 500 points using capped relative P99 excess and deadline-miss fraction. The strongest eligible service drives its shared class weight. Backlogged or busy background traffic retains utility as well as its hard floor. The transition term penalizes changed rate. The solver only takes a positive improvement of at least one meaningful step, with ties resolved by fixed class order and smaller movement within each pair. This is an exact optimum over the bounded one-transfer neighborhood. Its value does not predict milliseconds or packet scheduling fairness.

Four classes admit at most 16 directed class/spare pairs. Each pair needs a discrete binary search of at most 20 iterations over the concave score. The water fill can remove at most four saturated classes before final rounding. All arithmetic is signed 64-bit, with inputs checked before arithmetic and outputs checked again before returning. No external solver is required. [Solver verification](OPTIMIZER_TESTS.md)

## Plan, apply and recover

1. The daemon checks peer authority, owned topology, configured capacity, policy bounds and admitted leases. Expired requests are excluded. An infeasible model carries conflict codes and cannot produce an applyable proposal.
2. The planner exposes input constraints, demand provenance, objective weights, selected allocation, comparator allocations and ordered class diff. Static fallback is available only if it satisfies all hard bounds. The actual verified allocation is separate from desired and proposed values.
3. Active control additionally requires fresh observer and directional bottleneck evidence for transfers. Its cooldown, consecutive eligible windows, action budget, outcome window and reversal guard bound configuration churn. A change in the registry cancels pending evidence and re-evaluates the authorized baseline.
4. The actuator checks the exact HTB root, parent rate and class inventory, then applies decreases before increases. It commits in-memory allocation only after readback matches. Partial failure is compensated only when observed state matches the known intermediate state. An external edit, changed topology or capacity drift halts control without blindly replacing the state. Multiple tc operations remain individually visible to the kernel.
5. Post-change samples compare request volume, latency, deadline misses, errors, peer harm and background service. Inconclusive or harmful probes roll back through the same verified path. Sustained recovery releases boosts. Research convergence requires two healthy nonoverlapping windows at the same allocation, with no pending probe or error. The optimizer preserves admitted class ceilings when restoring a feasible baseline.

Preview reports the proposed diff even when the separate directional gate blocks a transfer. Policy history records verified changes and their evaluation. [Bottleneck gating](BOTTLENECK_ENGINE.md) · [Optimization results](OPTIMIZATION_RESULTS.md)
