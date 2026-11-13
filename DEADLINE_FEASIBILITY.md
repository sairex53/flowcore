# Deadline Feasibility Model

## Decision vocabulary

| Feasibility | Meaning | Cooperative recommendation |
| --- | --- | --- |
| `FEASIBLE` | Guarded P95 estimate fits and required evidence is verified. | `ACCEPT` |
| `AT_RISK` | A usable estimate exists, but it does not meet the guarded objective or pressure suggests waiting. | `ACCEPT_WITH_RISK` or `DEFER` |
| `INFEASIBLE` | Deadline is already expired, or a verified hard lower bound exceeds remaining time. | `REJECT_RECOMMENDED` |
| `INSUFFICIENT_EVIDENCE` | A necessary input is missing, stale, unscoped, or contradictory. | `UNKNOWN` |

The recommendation is not an enforcement decision. Applications retain responsibility for admission.

## Inputs and estimates

`RequestCost` contains an absolute `CLOCK_MONOTONIC` deadline, remaining request bytes, optional response bytes, application processing P50/P95, optional application processing minimum, optional app-observed request/response transfer rates, and a reliability preference. Observed rates can lower the predicted rate when they are below the available class rate; they never establish a hard capacity bound. `Evidence` contains freshness and scope, per-direction service rate/backlog, RTT P50/P95, service concurrency and queue depth, plus optional verified capacity/fairness fields.

The P50/P95 estimates account for both byte directions and app work:

```text
network_estimate = max(RTT, upload_serialization + upload_queue_wait
                            + response_serialization + response_queue_wait)
request_estimate = network_estimate + app_queue_wait + app_processing
```

`app_queue_wait` uses the number of queued concurrency waves and the supplied processing estimate. If concurrency or queue information is absent, that term cannot be presented as measured. Bytes/second and queue values must be class-scoped and current. The model never labels a remote service as network-limited solely from end-to-end latency.

The hard lower-bound check is separate from the P95 prediction. It uses only a verified maximum transfer service rate, optional verified minimum RTT, and an application-declared hard minimum processing time. If that sum is greater than remaining time, infeasibility is proven under those assumptions. If it is lower, feasibility is not proven.

## Evidence quality and conservative behavior

- An expired request is infeasible regardless of telemetry.
- Stale evidence is not reused for a new feasibility claim.
- Unknown response size yields `UNKNOWN` because a completion-cost estimate cannot account for the response.
- An unverified reverse path, unverified service concurrency, or unverified fairness policy keeps a fitting P95 estimate at risk instead of returning `FEASIBLE`.
- A measured P95 overrun does not trigger a hard rejection. It returns `AT_RISK`.
- Missing or malformed evidence must not terminate application work or trigger an automatic privileged action.

The live evidence collector is restricted to verified FLOWCORE lab state. It records why evidence is unavailable (for example, stale sample, unsupported qdisc, or class-rate readback mismatch). The current benchmark attempt reported an empty evidence set; its exact cause remains unverified until a privileged rerun can read the new diagnostic field.

## Interpretation

The predicted P95 is a bounded engineering estimate based on observed quantiles. It is not a statistical confidence interval or SLA guarantee. Accuracy must be measured against application-instrumented ground truth for each service and workload shape before the estimate is used to drive production admission behavior.
