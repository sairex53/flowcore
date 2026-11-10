# Independent validation of the FLOWCORE candidate

This is a fresh maintainer-run reproduction of the existing tree.
The reviewer started from the published runtime-safety revision
06d0923 and used a separate checkout for fixes. This review is independent
of the earlier measurement runs. It is not a third-party certification.

## Findings remediated

| Severity | Evidence and root cause | Change and regression test |
| --- | --- | --- |
| High | The live daemon interfaces operation could terminate the service. A branch-local import shadowed the rtnetlink snapshot function for every branch of the request handler. The pinned-tc x86 service test reached the crash. | Alias the SLO snapshot import. Exercise the actual daemon interfaces operation in the unprivileged unit suite and in the x86 namespace integration. |
| High | Open-loop in-flight rejects wrote a timing row with one extra column. The terminal stamp landed after the schema and valid failure samples looked like malformed timing. | Emit the correct row width. Reject missing or extra columns during full and partial audits. An actual loopback overload fixture checks terminal timestamps and the complete ledger. |
| High | The comparison guard accepted only version 2, although the runner and report support version 3 events. The evidence verifier also accepted an empty or partial campaign if the available digests matched. | Accept matching version 3 campaigns, distinguish client exit from infrastructure corruption, and require every expected block for verification. Added fixture coverage for all three paths. |
| High | The source archive walked working directories. A local untracked file inside an included subtree could be bundled. | Use the committed Git tree, check the archive epoch and refuse tracked working-tree changes. A Git fixture verifies exclusion and repeatable archive bytes. |
| Medium | Ubuntu's stock tc 6.1 returned text for HTB class readback. FLOWCORE requires machine-readable data before policy admission. | The trusted x86 workflow builds digest-pinned upstream iproute2 7.0.0 and reruns service and recovery tests. The stock-tool failure is retained. |
| Medium | The available optional gRPC environment used grpcio 1.48.4 and protobuf 3.19.6, which matched upstream advisory ranges. | Document and enforce a patched dependency floor and pin the optional example environment. Core RPM and monitoring paths do not require gRPC. |

## Source and trust-boundary review

- The C++23 core provides parsing, histograms, the native optimizer,
  pacing, and the intent SDK. It retains a separate legacy benchmark
  controller. The Python daemon owns the serial policy/telemetry loop,
  configuration validation and lab orchestration. CMake installs both
  entry points and the C/C++ consumer headers and libraries.
- Normal monitoring opens read-only netlink and authenticated local
  sockets. The optional helper restricts commands to its private
  namespace using peer credentials, ACLs, revision checks and
  CAP_NET_ADMIN. The privileged research daemon still requires a
  trusted checkout, toolchain and invoking user.
- HTB checks the current ownership, class topology, filter rules and
  readback. The BPF engine checks the TCX attachment revision and
  relinquishes unpinned resources after an ambiguous policy
  acknowledgement. MPTCP endpoint IDs are checked against namespace,
  address and interface before a scoped preference update.
- SDK telemetry, UDP ancillary priority, socket pacing and FLOWID
  requests have distinct contracts. Application outcomes are recorded
  explicitly. Linux does not infer an application deadline from an
  encrypted stream.
- The secret-pattern scan covered the tracked tree and logged paths
  and match counts only. It found no matches for the scanned
  GitHub/AWS/Slack/private-key/credential-URL patterns. That bounded
  check does not cover host credential stores or every secret encoding.
- OSV evaluated the declared QUIC pins and optional application
  package versions. Advisory matches are upstream version matches.
  Fedora package backports, native dependencies and container-image
  CVEs require separate verification. [OSV API](https://google.github.io/osv.dev/api/)
  · [gRPC advisory](https://github.com/advisories/GHSA-496j-2rq6-j6cc)
  · [protobuf advisories](https://github.com/advisories/GHSA-7gcm-g887-7qv7), [second advisory](https://github.com/advisories/GHSA-8qvm-5x2c-j2w7)

## Separate x86_64 execution

The GitHub-hosted Ubuntu 24.04 runner used kernel 7.0.0-1017-azure. With
stock tc 6.1 the unit, helper and held-connection fixtures passed, but
HTB laboratory admission failed. Pinned upstream tc 7.0.0 exposed the
daemon inventory crash. After the dispatch fix, the source build,
CTest, private helper, owned namespace service and recovery tests,
and 10,000 held TCP connection fixture all passed. This is an actual
x86_64 VM and a bounded connection-scale test. [Stock-tool run](https://github.com/sairex53/flowcore/actions/runs/42727789263)
· [Fault reproduction](https://github.com/sairex53/flowcore/actions/runs/42728288981)
· [Passing retest](https://github.com/sairex53/flowcore/actions/runs/42728933342)
· [Upstream tc archive digest](https://www.kernel.org/pub/linux/utils/net/iproute2/sha256sums.asc)

No independently available physical Linux host or hardware timestamp
setup was identified. The ARM64 and x86_64 results are VM evidence.

## Fedora ARM64 execution

Fedora 45 ran in a QEMU ARM64 VM with kernel 7.2.11-300.fc45.aarch64.
Clean C++23 builds exercised BPF enabled with the optional TLS/FlowID
targets and BPF disabled in the RPM build. Each CTest run recorded
53 passes and one privilege-dependent skip. The skipped UDP fixture
subsequently passed in a private network namespace, including marked
IPv4 and IPv6 sends and eight software TX timestamps per family.
Staged installation, read-only startup, SDK consumer builds and removal
passed. The pinned gRPC loopback passed. The OSV query returned zero
matches for the checked replacement pins and their two resolved
transitive packages at audit time.

All seven scoped privileged scripts passed. They covered the helper,
daemon, recovery and fault injection, both flow-engine integrations,
held-connection scale, and the hybrid lab. The scale fixture held
100, 1,000 and 10,000 TCP connections. At 10,000 it recorded zero
failed requests, 8.18 ms successful P99, 131,100 KiB peak process RSS
and seven descriptors before and after. That run lasted 1.41 seconds,
capped in-flight requests at 128 and limited INET_DIAG output to 128
entries. It provides scale evidence for held sockets and bounded
snapshots.

Two further application lab fixes were necessary. A relative PostgreSQL
schema bind was interpreted as a named volume and invalidated all
twenty starts. An absolute bind with a regression fixture repaired
startup. The next attempt accumulated anonymous data volumes.
A 128 MiB tmpfs and explicit volume cleanup contained the allocation.
The subsequent twenty-trial comparison completed with the Podman
volume inventory unchanged at 144. Both earlier attempts remain
in the local evidence.

## Package verification

The unsigned RPM and SRPM were built from c7dba30. The standard host
build reported a missing OpenSSL development package. The successful
build used staged headers from the Fedora-signed OpenSSL development
RPM with matching host libraries and an explicit rpmbuild dependency
check override. RPM digests, package metadata and the extracted
read-only runtime passed verification. Extracted-runtime checks
covered reload, SIGKILL recovery, a corrupt checkpoint, concurrent
clients, rapid restarts and socket cleanup.

A network-disabled Fedora ARM64 OCI container completed the file
transaction test from 0.2.0-1.fc45 to 0.2.0-2.fc45 and erase.
The modified configuration survived upgrade, RPM retained it as
.rpmsave after erase, the program file was removed, and the operator
state marker survived. The fixture used unsigned local packages and
disabled dependency resolution and package scripts. The host-side
private RPM database fixture could not acquire its initialization lock
under the current policy. Dependency-resolved lifecycle, scriptlets,
systemd startup and installed active SELinux policy remain separate
gates.

| c7dba30 artifact | SHA-256 |
| --- | --- |
| Source tarball | abfb18166157e169dd12969b27b667f71095af243ed8999f2ded0939db41e702 |
| Fedora ARM64 binary RPM | 1073c716dd068f1cfd56801f32f6a0f676b47b52b889805d2fff7cc19cc40281 |
| Source RPM | edb64256983a0b47c0b03849fec569faf9bb02d2d7067a7f64c0c7510fe2da49 |

Execution logs, advisory and secret-scan scope, artifact digests and
the pinned OCI manifest are retained under results/rc-audit-811e70e.
The OCI image digest is
sha256:8dc967a06fc261fd485e44b13fbbad1fea6fcc158a7ae3dcdf7a7b615a4ce4d8.
[Passing source CI](https://github.com/sairex53/flowcore/actions/runs/42732864869)
