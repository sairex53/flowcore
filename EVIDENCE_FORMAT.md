# FLOWCORE Evidence Bundle Format

## Purpose and trust model

Evidence Bundle v1 is a portable, privacy-filtered archive for experiment records that have a supported raw-data adapter. The builder runs from a checked-out FLOWCORE tree; the verifier uses only Python 3's standard library and bundle contents. Verification needs no root access, networking, Linux namespaces, project-local modules, or kernel mutation.

The SHA-256 index detects changes relative to the archive's own index. It does not authenticate the publisher: bundles are unsigned. A reviewer must obtain the archive hash through a trusted channel.

## Layout

A v1 tar.gz contains:

- bundle.json: schema version, source checkout commit and dirty flag, bundle ID, size/hash inventory, duplicate-content groups, privacy status, and resource limits.
- SHA256SUMS: SHA-256 for every other member, including bundle.json.
- inventory.json: campaign references discovered in selected release/result documents, run classifications, missing-source counts, and included campaigns. Local result paths are replaced with opaque reference IDs.
- release-gates.json: sanitized snapshot of repository-declared gates; it is not recomputed from benchmark measurements.
- campaigns/<study>/campaign.json: normalized experiment manifest, source provenance, environment fingerprint, tool-version availability, normalized predeclared plan when present, statistical method, seed blocks, and run index.
- campaigns/<study>/runs/<run>/run.json: validity, reason codes, workload seed, source metrics, and raw-ledger references.
- Normalized requests.csv, receiver.jsonl, resource_samples.jsonl, qdisc_states.jsonl, kernel_readback.json, and transitions.jsonl when available.
- analysis.json and report.md: recomputed summaries and paired comparisons.
- privacy-review.json, README.md, reproduction.md, and the standalone tools/evidence_bundle.py.

Monotonic timestamps are rebased to the run's own measurement start. They are not comparable across machines or boots. Request identifiers and service names are replaced with local sequence numbers and service-N references. Application payload bytes are not included; numeric request payload sizes remain because they are needed to recalculate goodput.

## Classification

Each run retains explicit labels and reason codes:

- VALID_COMPLETED: the source marks the run measurement-valid; this can coexist with WORKLOAD_FAILURE.
- INVALID_INFRASTRUCTURE: source validity failed or infrastructure invalidation was recorded.
- INTERRUPTED: the run itself records interruption. An incomplete campaign does not relabel finished runs as interrupted.
- WORKLOAD_FAILURE: the run has unsuccessful offered requests.
- EXPLORATORY, PREREGISTERED_HELD_OUT, or UNCLASSIFIED_SCOPE: scope supplied by the versioned bundle specification.
- A source-invalid run without a timing ledger remains with REQUEST_LEDGER_MISSING or MISSING_RUN_TIME_INTERVAL; it is never treated as replayed. A valid run without a complete request ledger blocks packaging.

The inventory records references outside the initial adapter scope, missing directories, unsupported schemas, and missing ledgers. They are not silently converted into benchmark data.

## Statistics

- P50/P95/P99 use nearest-rank percentiles over successful request latencies.
- Offered, completed, failed, deadline successes/misses, and completion ratio preserve the full request denominator.
- Deadline attainment is successful on-time requests divided by all offered requests when a deadline was declared.
- Request goodput uses successful completions inside the measured window and the declared payload size.
- Receiver goodput and per-stream Jain fairness are recomputed from monotonic cumulative byte counters. Fairness follows the source phase definition and excludes phases with fewer than two configured background streams.
- Paired policy comparisons use independent run pairs, not individual requests. P99 pairs containing a failed request are reported as skipped; failure ratios remain in run summaries. Confidence intervals are exact order-statistic intervals for the paired median. Family-wise confidence is Bonferroni-adjusted using the declared mode/margin count.
- Sample counts below the predeclared minimum are INCONCLUSIVE; invalid blocks yield INVALID EXPERIMENT. Missing or non-replayable invalid runs remain explicit.

These calculations reproduce archived records. They do not establish causal effects beyond the experiment design, imply performance on other systems, or make request samples independent experimental replications.

## Privacy review and limits

The builder uses field allowlists, bounded CSV schemas, identity mapping, local-user/host literal checks, private-address and MAC detectors, absolute-path detection, and credential/private-key token patterns. It scans packaged normalized content before archive creation and scans selected raw source files for credential patterns without printing matches. Arbitrary source logs and application payloads are excluded.

These checks are defense in depth, not a universal secret scanner. Host/user fields and network identifiers are removed when recognized; reviewers should inspect privacy-review.json and the candidate before publication. The builder never uploads or creates a GitHub release.

## Budgets and verification

The current tool limits each member to 64 MiB, an archive to 64 MiB, unpacked content to 128 MiB, source evidence processed to 256 MiB, file count to 12,000, request rows per run to 100,000, build time to 180 seconds, and required free disk space to 64 MiB. Temporary archives use a same-directory temporary file and atomic replacement; failed builds remove their temporary file.

Build and verify commands and current adapter limitations are in [REPRODUCIBILITY_GUIDE.md](REPRODUCIBILITY_GUIDE.md).
