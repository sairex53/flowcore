# FLOWCORE Fleet management

Fleet is an optional policy delivery mode for an existing Fabric coordinator. The agent and local actuator still verify their own authorization and kernel state. Enable Fleet only after any pending Fabric reconcile finishes. Fleet takes over write reconciliation and the legacy Fabric CLI remains available for inspection. Active deployment targets need an agent with the fleet_metrics_v1 capability and access to local daemon metrics. Older agents can continue regular Fabric reporting.

## Configure trust and ownership

Add a `fleet` object to the coordinator's existing configuration. It has three maps.

- `repositories` maps a repository alias to an absolute local Git checkout `path`, a relative JSON `bundle` path, `require_signature`, and `keys`. Keys map operator-selected signer IDs to base64 raw 32-byte Ed25519 public keys. An unsigned repository uses an empty key map and explicitly sets the signature requirement to false.
- `operators` maps existing Fabric admin certificate pins to a stable `name` and allowed `owners`.
- `owners` maps an owner ID to its allowed `repositories`, `hosts`, `services` and `classes`. Assign each host to at most one Fleet owner. Every other enrolled service on a target host must also fall within that owner's scope because the cap shapes the entire interface.

The configured checkout and Git metadata are maintained locally by a trusted operator. Give the coordinator read access. The repository author cannot expand owner permissions, change enrolled host limits, add signing keys, or bypass review. See [FLEET_SECURITY.md](FLEET_SECURITY.md).

## Define a policy

Copy [the example bundle](config/fleet-policy.example.json) into the configured Git path. Set the cluster and owner to enrolled values and increment the revision for each deployment under that owner. This is the schema version 1 format.

| Field | Meaning |
|---|---|
| `version`, `policy`, `revision`, `owner`, `cluster` | Schema version, human-readable policy identifier, strictly increasing owner revision, authorized owner and Fabric cluster. |
| `host_groups`, `service_groups`, `target` | Named group maps and the selected host group, service group and traffic classes. |
| `caps_kbit` | Complete cap map for the selected hosts in decimal kbit/s. The enrollment sets immutable bounds and resource capacity. |
| `controller` | Only the existing `fixed-cap` controller and `protected-floor` loss behavior are available. |
| `slo` | Successful-request P99 limit and optional increase from predeployment baseline in microseconds, error and deadline-miss limits in parts per million, minimum samples, throughput and fairness floors, and maximum local controller actions. Zero disables throughput and fairness floors. Null disables the relative latency and controller action checks. |
| `stages` | Explicit canary hosts, increasing percentage cutoffs, kernel verification timeout, observation timeout and the number of distinct windows required for confirmation. |

Traffic classes select the enrolled service metrics to observe. The actuator still applies a single cap per interface. Services sharing that interface can experience collateral impact. The plan lists those enrolled services, the rate difference, and whether the proposed cap is below the observed local rate when a fresh sample is available. That comparison is an estimate of where shaping may bind. It does not predict end-to-end latency.

## Review and deploy

Use the Fabric operator TLS configuration and a full commit object ID from the configured checkout. The coordinator reads the committed blobs and checks the signature if present or required.

```sh
flowcore fleet validate --config operator.json --repository release --commit "$commit"
flowcore fleet plan --config operator.json --repository release --commit "$commit" --mode canary
flowcore fleet rollout --config operator.json --repository release --commit "$commit" --mode canary --approve "$digest"
flowcore fleet status --config operator.json
flowcore fleet history --config operator.json
```

Copy the approval digest from the reviewed plan for the exact mode. The digest binds the operator identity, immutable source, baseline generation, target and stages. A concurrent state change can require a new plan. Validation and dry-run publish no caps. Shadow mode records an approved read-only observation. It does not consume the policy revision. Canary and percentage deployments pause after their approved limit. Plan and approve the same commit again to promote a paused rollout to a later stage. `--mode full` runs any remaining declared stages and waits for each one.

To roll back the latest rollout, use the generation and active approval from status.

```sh
flowcore fleet rollback --config operator.json --generation "$generation" --approval "$active_digest"
flowcore fleet status --config operator.json
```

An attempted or blocked rollback needs verification. The deployment is restored only when status reports `rolled_back` and the enrolled agents confirm the baseline caps in the kernel. See [POLICY_ROLLOUT.md](POLICY_ROLLOUT.md) and [ROLLOUT_RESULTS.md](ROLLOUT_RESULTS.md).
