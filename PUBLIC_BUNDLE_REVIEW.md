# Public evidence bundle review

## Review status

**Local publication candidate only. Not uploaded to GitHub.** The builder does not create or attach a release, and the repository workflow does not publish the archive. Release attachment metadata is in [packaging/evidence-release-metadata.json](packaging/evidence-release-metadata.json).

The archive is generated under the ignored build/evidence-candidate/ directory. Its .sha256 sidecar and bundle.json cover the exact candidate bytes. No raw source logs are uploaded or committed.

## Scope

The source list in config/evidence-bundle.json extracts result references from current benchmark, release, and subsystem result documents. The inventory preserves missing paths, custom schemas, incomplete campaigns, invalid blocks, and workload failures with counts/reason codes. Only selected standard Research Lab campaigns with raw request ledgers are normalized for replay.

The selected set includes the primary strategy and Linux baseline matrices, low-load/default references, the paired static-versus-hybrid follow-up, interrupted timing-ledger investigation, workload-failure studies, mixed-transport overloads, and later strategy/action experiments. One invalid source run lacking a request ledger remains represented as non-replayable with an explicit reason; it is not counted as replayed.

Custom UDP/MPTCP and application-stack artifacts are inventoried but are not folded into standard replay until their schemas have a reviewed adapter. The source project's declared release gates are included as a snapshot and remain separate from replayed benchmark gates.

## Candidate verification record

The first local target-host candidate was generated from the selected raw artifacts and verified by both the repository copy of the validator and a copy extracted from the archive run with Python isolated mode. It contained 17 campaigns and 232 run records; 231 had request ledgers replayed, while one source-invalid run without a ledger remained explicitly non-replayable. Replay matched 2,988 stored metric checks; 15 Jain-index checks were NOT_COMPARABLE because the source did not contain a corresponding value. No metric check mismatched.

Labels overlap: 202 runs were marked measurement-valid, 30 infrastructure-invalid, 77 had request workload failures, 212 were exploratory, and 20 belonged to the report-identified frozen held-out follow-up. The interrupted timing-ledger campaign remains marked interrupted at campaign level. These counts describe this selected archive, not every inventory reference. The analysis retained the existing inconclusive and invalid experiment outcomes; it does not show a general FLOWCORE advantage. The repository gate snapshot still says production_ready=false and performance/reproducibility gates PARTIAL.

The release documents referenced 150 campaign paths. Some references were unavailable or used custom schemas; the inventory reports these counts without treating them as failed measurements. The archive is a candidate, not proof that those unadapted studies can be replayed.

## Privacy review

The builder:
- Copies normalized allowlisted measurements rather than arbitrary logs/directories.
- Removes host, user, process, interface, service, namespace, socket, address, and payload-content fields; replaces request/service identities.
- Checks selected raw artifacts for credential/private-key patterns and scans normalized output for paths, private-network identifiers, MAC addresses, email addresses, and known user/host identifiers.
- Stops on detected credentials, unsafe archive names, unsupported request columns, oversized inputs, insufficient disk, or checksum/replay disagreement.
- Records only finding codes and redaction categories; it never prints matched values.

The scanner uses bounded pattern checks and field allowlists. It cannot prove the absence of every possible secret or re-identification vector. This is a pre-publication candidate review, not a cryptographic signature or a substitute for a human review of the final archive.

## Evidence needed before publication

Before a release maintainer attaches this archive, verify the final candidate with the standalone command in [REPRODUCIBILITY_GUIDE.md](REPRODUCIBILITY_GUIDE.md), inspect privacy-review.json and the generated report, confirm the external SHA-256 through a trusted channel, and review selected campaign scope. This task does not upload or publish the bundle.
