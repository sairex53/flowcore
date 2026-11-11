# Reproducing FLOWCORE evidence

## Verify a publication candidate

The verifier does not require FLOWCORE, a working network, root, or project-local imports:

    python3 -I tools/evidence_bundle.py verify flowcore-evidence-v1.tar.gz --replay

The command validates archive paths and bounds, requires every member to appear in both the internal inventory and SHA256SUMS, checks sizes and hashes, compares duplicate-content groups, runs privacy detectors, and recalculates replayable campaign statistics. A successful verification reports checksum and statistical replay status. Invalid and interrupted records remain; non-replayable invalid records appear with their reasons and are excluded only from statistics that cannot be recomputed from missing raw inputs.

The verifier checks archive-internal hashes; it cannot authenticate who built the bundle. Compare the archive's external .sha256 sidecar over a trusted channel. A sidecar shipped beside an untrusted archive is not a signature.

## Rebuild FLOWCORE

Each campaign.json records the experiment source commit and build/kernel environment. To build that revision on a compatible disposable Fedora Linux VM:

    git clone https://github.com/sairex53/flowcore.git
    cd flowcore
    git checkout SOURCE_COMMIT_FROM_CAMPAIGN_JSON
    cmake -S . -B build -G Ninja -DFLOWCORE_BPF=OFF -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j2

Build options, installed tool versions, kernel version, architecture, qdisc readback, and available measurements are recorded per campaign. Missing upstream environment fields are marked unrecorded; the bundle does not invent them.

## Replay the stored statistics

Use verify --replay above. It runs entirely on normalized bundled ledgers and compares the result with analysis.json. It does not recreate packet timing or touch a network namespace.

For a source project checkout, the Research Lab analysis command is:

    sudo ./build/flowcore bench replay EXPERIMENT_MANIFEST_JSON

The normalized predeclared plan is stored in the corresponding campaign.json. To export that plan locally for a new run:

    python3 - <<'PY'
    import json
    from pathlib import Path
    campaign = json.loads(Path("campaign.json").read_text())
    Path("build/reproduce.json").write_text(
        json.dumps(campaign["predeclared_plan"], indent=2) + "\n")
    PY
    sudo ./build/flowcore bench run build/reproduce.json

Run a new experiment only in a disposable Linux environment with owned namespaces and the needed privileges. A new run will not reproduce exact packet timing. Linux scheduling, kernel versions, CPU contention, and VM noise can change results.

## Bundle builder

From a clean FLOWCORE checkout containing the result artifacts listed in config/evidence-bundle.json:

    python3 -I packaging/evidence_bundle.py inventory --root . --spec config/evidence-bundle.json
    python3 -I packaging/evidence_bundle.py build --root . --spec config/evidence-bundle.json --output build/evidence-candidate/flowcore-evidence-v1.tar.gz
    python3 -I packaging/evidence_bundle.py verify build/evidence-candidate/flowcore-evidence-v1.tar.gz --replay

The builder requires declared free disk space and output/runtime budgets. It writes only under the repository, refuses any output path with a results component, never modifies source artifacts, and does not publish.

## Scope and known gaps

The current adapter replays versioned Research Lab summary schemas with request CSVs and receiver counters. It inventories wider benchmark references in release documents, but custom UDP, MPTCP, HTTP/PostgreSQL, and other lab formats remain listed outside the replay adapter unless a normalized run ledger exists. Missing or custom-schema evidence is not evidence of success or failure.

The source summary and raw experiment manifest are represented by provenance hashes and a normalized plan, rather than copied verbatim, to avoid redistributing identity-bearing values. Those source hashes are provenance references and cannot be recalculated from the privacy-filtered archive. Archive hashes only cover bundled files.
