# FLOWCORE API compatibility contract

This document identifies the interfaces that have an explicit compatibility promise and those that remain rebuild- or version-dependent.

## Compatibility window

The stable binary boundary is the versioned C ABI library libflowcore_intent.so.2, exposing the fc_v2_* symbols under ELF symbol version FLOWCORE_INTENT_2.

Within ABI major 2:

- Existing exported fc_v2_* functions are retained with compatible signatures and semantics.
- New functions may be added.
- Existing C struct layouts are fixed; version/size fields are used by extensible result and option structures.
- Removing or changing an existing symbol or struct layout requires a SONAME/ABI-major change.
- The checked-in symbol baseline and compile-time C layout assertions run in CI.

The C++23 SDK, including the static libflowcore_sdk.a, does not promise a stable C++ ABI. Applications should rebuild against the matching headers and library when updating FLOWCORE.

## Local IPC

The application SDK uses a versioned local protocol with capability negotiation, bounded frames, peer credentials, and explicit unsupported-version errors. Compatibility tests cover the prior supported client surface against the current daemon. The wire format is not a public remote protocol and must not be exposed over a network socket.

Protocol revisions must preserve a safe failure mode: incompatible or unsupported features return an explicit error and do not apply network policy.

## Configuration

service.json and helper.json are versioned JSON schemas. Schema v1 is strict: duplicate keys, unknown fields, and unsupported version values fail validation. RPM upgrades preserve modified files via config(noreplace). There is no automatic schema migration in this release; configuration is never silently rewritten during package installation.

A future schema migration must be a separate staged operation: validate old input, preserve a recoverable copy, produce a candidate, validate the candidate with the target daemon, and commit only after validation. Any error must preserve the original.

## CLI and metrics

The CLI command groups are source-compatible only where covered by documented examples and regression tests. Human-readable output is not an ABI.

JSON output is stable only when its command documents a schema version. Metrics names and units are defined by the metric catalog; adding metrics is compatible, while changing a name or unit requires a new metric/schema version and a deprecation period.

## Verification

Run ctest to execute the C ABI symbol/layout and local IPC compatibility checks. For an installed package, use rpm -q --provides flowcore to confirm the C ABI SONAME dependency and compile a small C consumer against the installed header and -lflowcore_intent.
