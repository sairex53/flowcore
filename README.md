# FLOWCORE

C++23 experimental controller for local TCP egress latency on Fedora 45 ARM64.
The runnable MVP includes an isolated two-namespace benchmark, application-level
latency measurements and bounded HTB policy adaptation. See ARCHITECTURE.md and
BENCHMARK.md for scope, privilege boundaries and experiment design.

## Build and validate

Run from `/home/elliot/projects/flowcore`.

```sh
./scripts/setup-tools.sh
cmake -S . -B build -G Ninja -DCMAKE_CXX_COMPILER=/usr/bin/g++
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 scripts/integration_test.py
sudo python3 scripts/benchmark.py --duration 12 --repetitions 3
```

The machine already provides GCC, CMake, Ninja, Python, ip, ethtool, setpriv,
nsenter, unshare, rpm2cpio and cpio. The setup script extracts Fedora iproute-tc
under tools/ and uses the existing shared libraries. It does not require a
system-wide tc installation. The benchmark supplies the selected tc path to the legacy controller.

A short functional run is available with

```sh
sudo python3 scripts/benchmark.py --duration 2 --repetitions 1 --no-pilot \
  --modes fifo fq_codel cake static20 flowcore
```

Short runs validate wiring. The default full run takes approximately six minutes
plus setup and records 1,200 requests per held-out trial. For stronger tail
estimates increase duration and repetitions using BENCHMARK.md's protocol.
See RESULTS.md for the measured comparison and its limits.
Evidence appears in a unique results/ directory printed at startup. Run
`python3 scripts/report.py results/<run-directory>` to inspect it.

## Components

* `include/flowcore`, `src` contain strict configuration, hysteresis policy,
  read-only rtnetlink metrics and the tc actuator.
* `config/lab.conf` specifies the service SLO and bounded class reservations.
* `scripts/workload.py` runs the TCP service and request/background generators.
* `scripts/benchmark.py` creates topology, selects static policy on pilot data,
  runs randomized evaluation and records raw evidence.
* `tests/policy_test.cpp` and `scripts/integration_test.py` validate pure policy
  and actual kernel behavior, including stale feedback and rollback.

All scripts and binaries are invoked manually. The benchmark requires root for
its namespace setup and controller, and drops workload privileges to SUDO_UID.
If run directly as root, workloads also run as root. Ctrl+C invokes cleanup.
Source, tools, build outputs and experiment evidence stay under this directory.

## Run the management service

Build as above. Start the service in one terminal and use the CLI in another.
The service stays in the foreground and logs JSON lines to stdout.

```sh
sudo ./build/flowcored --config config/service.json
```

```sh
sudo ./build/flowcore daemon status
sudo ./build/flowcore lab up
sudo ./build/flowcore interfaces --json
sudo ./build/flowcore policy list
sudo ./build/flowcore policy apply fair
sudo ./build/flowcore policy inspect
sudo ./build/flowcore metrics --json
sudo ./build/flowcore lab configure --bandwidth-kbit 16000 --delay-ms 12 --jitter-ms 2 --loss-pct 1 --reorder-pct 5
sudo ./build/flowcore lab inspect
sudo ./build/flowcore lab down
```

`flowcore interfaces --local` performs direct discovery without the daemon.
Every management subcommand supports --help and returns JSON on success. Use
--json for compact output. CLI exit codes are 0 for success, 1 for rejection,
2 for invalid CLI syntax and 3 for unavailable IPC or Linux query failure.
Stop flowcored with Ctrl+C or SIGTERM. Changes to a running profile or policy
replace the disposable namespace pair. Stop external workloads before replacing
or shutting down the lab.

For project-local installation run `cmake --install build --prefix "$PWD/install"`.
The installed launchers use the checked-out Python scripts, local tc and
canonical build directory. Keep this tree in place. System-wide deployment and
relocatable packaging are separate work.

`sudo python3 -B tests/service_integration.py` runs the privileged service
suite. Stop any running flowcored first so the suite can own the socket.
`scripts/workload.py --help` lists payload, port, concurrency and rate flags.
The standalone adaptive benchmark keeps its existing CLI and defaults.

## Observe a running lab

Use `sudo ./build/flowcore observe --interface fc0 --watch 1`,
`connections --csv`, `events --jsonl`, `health` or `metrics --prometheus`.
The optional CO-RE collector falls back to INET_DIAG when unavailable.
OBSERVABILITY.md documents metric sources, application IPC and overhead tests.

## Adaptive intent and SDK

See ADAPTIVE.md to register intents, evaluate a dry run, start shadow or active
control, and read the decision history. The C++23 SDK target is
`flowcore_sdk`, with a TCP example in examples/sdk_request.cpp.
ADAPTIVE_RESULTS.md records the baseline comparisons and live safety checks.

## Deploy and operate

Start with PLATFORM.md for monitoring, device profiles, MPTCP and controlled
Podman workloads. CLI.md lists command groups. SECURITY.md documents the
read-only production unit and privileged laboratory boundary. RELEASE.md
describes the staged install, SDK export and Fedora RPM. RESEARCH.md
describes reproducible experiment reports.

The optional isolated network helper accepts typed operator commands without
root in the client process. See [PRIVILEGES.md](PRIVILEGES.md) and
[THREAT_MODEL.md](THREAT_MODEL.md).

For comparable benchmark matrices, failure-aware metrics and reproducible
paired experiments, see [BENCHMARK_V2.md](BENCHMARK_V2.md).

The adaptive-control investigation, measured limitations and reproduction
commands are in [ADAPTIVE_ROOT_CAUSE.md](ADAPTIVE_ROOT_CAUSE.md).

Application integration: [Intent API V2](INTENT_API.md) and [C++ / C / Python examples](APPLICATION_INTEGRATION.md).
V2 provides process-bound cooperative request telemetry and FD-attested socket correlation; it does not promise per-request packet scheduling.

Opt-in [eBPF flow engine](EBPF_FLOW_ENGINE.md): owned-namespace TCP/UDP classification and [measured performance](EBPF_PERFORMANCE.md). Read-only fallback: `flowcore flows --json`.

Intent V2 now supports persistent authenticated sessions, renewable process leases, UDP socket bindings and explicit dissociation. See [application integration](APPLICATION_INTEGRATION.md) for the shared-connection multiplex demo.

Experimental congestion prediction: [architecture and shadow activation](PREDICTIVE_ENGINE.md)
and [measured results and limitations](PREDICTIVE_RESULTS.md). Predictive operation
is off by default; no advantage over reactive control or tuned static QoS is established.
