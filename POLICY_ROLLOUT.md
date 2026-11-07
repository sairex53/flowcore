# Fleet rollout lifecycle

| State | Evidence |
|---|---|
| Repository configuration | Full commit ID, exact policy blob hash, optional signer fingerprint and Git author metadata. |
| Validated desired state | Strict schema, owner and enrollment scope, feasible full and intermediate cap maps, selected metrics, and current capability and readback readiness. |
| Planned changes | Baseline and target, deterministic stage cohorts, estimated cap impact, and approval digest. |
| Committed generation | The coordinator durably published the authorized quota map. |
| Agent state | An enrolled agent accepted a scoped generation and reported a local acknowledgement. |
| Kernel-verified state | Every enrolled egress participant has a fresh exact-generation and exact-rate acknowledgement, backed by its actuator readback. |

Fleet persists the deployment journal and desired map in one atomic replacement of the Fabric state file. Only one rollout can be active. Owner revisions remain consumed after rollback. An exact paused bundle can be promoted without changing its revision. A restarted coordinator reloads the stage and waits for new sessions and current acknowledgements. It discards in-memory guard streaks and uses the remaining phase deadline, with a short reconnect allowance. A persistence failure stops publication.

## Stages and measurements

The canary is explicitly named. Other hosts are ordered by a deterministic hash of policy ID, revision and host ID. Each percentage rounds up to a host count and retains the canary cohort. A full deployment covers all selected hosts. Each cumulative stage preserves every host outside its cohort at the original baseline. Admission rejects infeasible intermediate capacity sums even when the final allocation fits.

During a mixed increase and decrease, the first generation takes the component-wise minimum of the old and new cap. Fleet publishes the increases only after exact fresh kernel acknowledgement of that lowering generation. Hosts can temporarily operate on different generations. The resource bound assumes the configured choke point, exclusive owned queues, a single coordinator lineage, and honest agents.

After verification, Fleet fences the first observed service window and only accepts subsequent snapshots whose local window end advances by at least the reported window duration. It compares each service with its declared thresholds. Relative P99 requires a sufficiently sampled predeployment baseline. Error and miss thresholds use successes plus failures as the denominator. Throughput comes from local interface counters. Jain's index is calculated across the current cohort, not across applications. The controller guard checks the local action counter and controller session for discontinuity. A cohort can advance only after the configured number of clean windows. Repetition of the same violation triggers rollback. These thresholds are operational guardrails rather than a statistical test of causality.

A paused deployment keeps its approved caps and requires explicit promotion. It does not continue evaluating performance guardrails. A completed deployment is also left to normal Fabric health and watchdog behavior. A later deployment can supersede it. Fleet retains rollback to the most recent deployment's initial baseline.

## Failure and recovery

A failed or inconsistent actuator acknowledgement triggers rollback. Missing verifications or observations time out. Loss of a participant during observation aborts the rollout after its heartbeat becomes stale. Missing or insufficient evidence cannot promote a stage.

Emergency and automatic rollback first publish caps that can only decrease. Restoration of higher baseline caps waits for all enrolled egress participants to verify that safe intermediate generation. An unreachable agent eventually falls back to its local protected floor if the actuator remains alive. If recovery cannot be verified within the verification interval, status reports `rollback_blocked`. The coordinator rechecks the barrier as agents return. It does not claim completion based on a timeout. An actuator that was killed may leave its last cap installed. Recover its owned namespace under operator supervision before admitting further increases.

The coordinator records approval, stage publication, readback, guard evidence, failures and attempted, blocked and verified rollback. The state file retains 32 recent events. Page through `fleet history` with `--offset` and `--limit`. Structured stdout logs can be forwarded to a longer-lived audit sink. Kernel details are available in existing agent and actuator logs linked by the Fabric generation.
