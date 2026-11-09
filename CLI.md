# FLOWCORE CLI reference

`flowcore --version` reports the C++ build version. `flowcored --version`
reports the package layout version. Each command group has `--help`. The
daemon uses its configured private socket. Set FLOWCORE_RUNTIME to select
a separate runtime directory. The installed default is /run/flowcore.

| Command | Behavior |
|---|---|
| status, daemon status, health | Daemon, backend and collector health |
| interfaces [--local] | Existing namespace snapshots or direct local discovery |
| platform interfaces | Interface inventory, route and link events, path and queue observations |
| platform policies | Stored revision and bounded profile audit |
| platform commit FILE --revision N | Validate and replace persistent profiles |
| platform resolve --device KIND --interface NAME --service NAME --container NAME | Resolve selected overlays with provenance. Selectors are optional. |
| platform apply-intent --service NAME [--container NAME] | Admit the resolved profile into an owned HTB lab |
| platform mptcp | Local namespace capability and path manager discovery |
| platform multipath-lab | Privileged experimental two-path MPTCP transfer |
| platform containers [--store PATH], platform container-inspect NAME [--store PATH] | Bounded local Podman inventory |
| platform container-lab | Privileged cgroup v2, SDK and Podman experiment |
| platform container-status | Owned live classifier identity and counter snapshot |
| lab up/down/status/inspect | Create, remove or inspect the owned lab |
| lab configure --bandwidth-kbit N --delay-ms N --jitter-ms N --loss-pct N --reorder-pct N | Stage and replace the owned topology |
| policy list/apply NAME/inspect | Configured lab schedulers |
| policy evaluate --dry-run, policy explain, policy history | Adaptive proposals and decision history |
| intent register FILE/list/inspect NAME/remove NAME | Authenticated application leases |
| controller start/stop/status/shadow | Active, disabled and read-only adaptation. Strategy flags are static, threshold and slo. |
| hybrid plan FRAME / hybrid inspect JOURNAL | Read-only Hybrid V3 dry run and decision audit for Research Lab artifacts. |
| slo report SUMMARY / slo shadow | Paired multi-objective evaluation or read-only allocation preview. Both require an operator profile. |
| observe [--interface fc0] [--watch SECONDS] | Lab telemetry snapshots |
| connections [--limit N] [--offset N] [--csv] | Bounded socket snapshots |
| congestion, events [--jsonl], metrics [--prometheus] | Observations, live events and exports |
| report start/sample/complete | Explicit application measurement IPC |
| bench run/replay MANIFEST, bench suite [--quick] | Reproduce and compare owned scenarios |
| optimize plan/run/status/results/recommend/compare | Bounded HTB reservation search in owned namespaces; never deploys a host policy |
| bench compare BASE CANDIDATE, bench report SUMMARY | Analyze existing evidence |
| bench minimize MANIFEST --mode MODE --failure-ratio N | Bounded failure reproduction and reduction |
| chaos run MANIFEST, chaos stop | Run or cancel owned research faults |

Management commands return JSON. `--json` selects compact output where
supported. Research commands publish evidence under FLOWCORE_RESULTS, or
results/ in the source tree. Application, Prometheus, CSV and event formats
are described in OBSERVABILITY.md. See ADAPTIVE.md and RESEARCH.md for
workload examples and phase timing. Autotuning budgets, search scope and uncertainty are documented in AUTOTUNING.md and OPTIMIZATION_EXPERIMENTS.md.

Exit codes are 0 for success, 1 for rejection or experiment failure, 2 for
invalid CLI syntax, 3 for unavailable IPC or a Linux query error, and 130
for an interrupted research run. A confirmed regression also returns 1.
`--config FILE --lab-netns PATH` and `--link IFACE` expose the legacy benchmark controller
and read-only link-stat entry points.

`flowcore lifecycles --limit 128 --offset 0 --json` queries the retained
trace lifecycle table. It also supports `--csv` and `--watch SECONDS`.
Each response carries the collector generation, retention and backend loss
status. Host read-only observation and metric availability are documented in
[OBSERVABILITY.md](OBSERVABILITY.md).
