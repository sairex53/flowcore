# API index and compatibility

- C++23 SDK: include/flowcore/intent_v2.hpp; service lifetime, RAII request/stream scopes, setup-time descriptor association and transport declarations.
- Stable C ABI layouts/opaque handles: include/flowcore/intent_c.h. Additive functions retain existing options/statistics layouts. The overall SDK remains experimental.
- Intent wire protocol: INTENT_API.md, version 2 with feature negotiation and bounded process-bound leases. Clients must handle unavailable/older daemons explicitly.
- Application/ctypes examples: APPLICATION_INTEGRATION.md and examples/python/flowcore_intent.py.
- Transport hints and logical priorities: TRANSPORT_AWARE.md; logical stream priority is reported, not automatically enforced by Linux.
- Workload registry/policy overlays: WORKLOAD_NETWORKING.md; registration grants no network authority.
- Privileged typed helper: SECURITY.md, PRIVILEGES.md and config/helper.json; private namespace scope only.
- Operations: OPERATIONS.md; validation, safe reload subset, checkpoint/status, diagnostics and explicit lab stop.
- CLI commands: CLI.md and --help.

Request delivery is asynchronous best effort. Inspect dropped/rejected/abandoned accounting; flush drains work, not a promise of terminal-record acceptance. Applications retain their own timeout, cancellation and network-library ownership.

Version 2's native C++ Request layout changed with stream metadata and requires rebuilding C++ consumers. No binary C++ ABI stability is promised across experimental snapshots.
