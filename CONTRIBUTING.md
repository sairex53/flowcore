# Contributing

FLOWCORE is an experimental research project. Project-wide licensing and contribution terms remain pending the owner's decision; no license grant is inferred from repository visibility. Preserve attribution and identify third-party code/dependencies in proposed changes.

## Scope and build

Keep Linux TCP/MPTCP/QUIC semantics accurate. Prefer small, measured changes. Python orchestration remains appropriate; native migration needs evidence or a concrete maintenance reason.

```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=OFF
cmake --build build -j2
ctest --test-dir build --output-on-failure
python3 -B scripts/check_style.py
```

Optional BPF needs clang, libbpf, bpftool and kernel BTF. Kernel support must be tested, not inferred from version alone.

Sanitizer tests:
```sh
cmake -S . -B build-review-asan -DFLOWCORE_BPF=OFF -DFLOWCORE_SANITIZE=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build build-review-asan --target telemetry_native_test sdk_lifetime_test intent_parser_test -j2
ASAN_OPTIONS=detect_leaks=1 ctest --test-dir build-review-asan -R '^(telemetry_native|sdk_lifetime|intent_parser)$' --output-on-failure
```
This checks selected native paths. Python-hosted C ABI tests and a complete runtime race/leak campaign remain separate.

## Evidence in pull requests

Describe the defect or capability, its ownership/privilege boundary, the actual change and tests executed. Report skipped/failed tests. For performance changes provide frozen configuration, source/build identities, raw artifact paths, independent repetitions, failure/deadline accounting, uncertainty and overhead. Preserve negative and inconclusive results. Pilot/tuning seeds must not overlap evaluation seeds.

New privileged operations need typed validation, ownership verification, bounded work and cleanup tests. Reproduce them only on a trusted disposable VM with a separate runtime. Privileged CI is manual and restricted to the main branch/trusted runner; never run untrusted fork code there.

Do not commit credentials, private keys, runtime state, downloaded tools, build output or raw local experiments. Include a concise reproduction command and document limitations. SDK flush means queue drainage, not guaranteed delivery; tests must account for observable loss.

## Reporting

Use issue templates for reproducible bugs and scoped research requests. Follow SECURITY.md for vulnerabilities and avoid including secrets or live application traffic in public reports. There is no guaranteed support/response SLA.
