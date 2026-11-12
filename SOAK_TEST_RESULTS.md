# Long-duration soak and resource stability

## Harness

`scripts/runtime_soak.py` supervises fixed in-repository profiles: the owned
namespace Research Lab, daemon/service lifecycle tests, UDP workload tests, and
the held-TCP scale test. It does not accept arbitrary shell commands. Run it
from a disposable source checkout as root because the integration profiles
create and inspect owned network namespaces. The monitor records its child
process tree's RSS, CPU ticks, file descriptors and thread count, and samples
network namespaces, qdiscs, BPF objects/pins, container volumes, temporary
directory entries, result-directory bytes and free disk space. A before/after
hash of host links, addresses, routes and qdiscs detects unrelated host-network
changes; a mismatch fails the run.

Example bounded command:

```sh
sudo python3 -B scripts/runtime_soak.py --profile full --cycles 2 \
  --duration-seconds 1800 --sample-seconds 5 --max-minutes 15
```

The runner writes `metadata.json`, progress, raw resource samples, per-step
logs and `report.json` below a private `results/runtime-soak-*` directory.
Output size, wall time, minimum free disk space (1 GiB by default) and tolerated
resource growth have explicit limits. A
timeout or unavailable inventory is retained as incomplete/unverified rather
than reported as a pass. Request summaries retain offered, completed and failed
denominators, deadline misses, conditional P99, goodput, controller adjustments
and rollbacks where the experiment summary exposes them.

## Early/late analysis

Repeated cycles compare first and last peak RSS, FD and thread counts per
profile against the configured growth limits. Research artifacts also report
early/late P99, request failure rate, background goodput and CPU seconds by
mode. These are descriptive observations; they are not a paired performance
regression test. At least two repetitions are required to label resource drift
measured. The release gate requires a complete service/full profile of at least
24 hours, 20 or more samples, passing resource inventory gates and retained
logs. Short smoke runs only validate that the harness executes.

## Current run record

On the x86_64 development host (kernel `7.2.15-1-omarchy`), a fresh Release
build completed with BPF disabled and 13 selected CTest checks passed. The
first bounded service run completed in 28.648 seconds. After the 1 GiB free
space guard was added, a repeat correctly stopped before workload launch when
the copied Fedora root had 82 MiB free. The copied VM's root LV was expanded
from 15 GiB to 40 GiB; the original UTM image was not changed. The current-code
service run then passed in 27.357 seconds with 27 samples. Process cleanup,
host-network fingerprint, namespace, qdisc, disk, temporary-file and output
checks passed. BPF inventory is unavailable and one cycle is insufficient for
early/late drift, so those fields remain `UNVERIFIED`.

A separate Fedora 45 AArch64 guest was built and tested under QEMU TCG. It
passed the same 13 selected CTest checks. Its bounded service run passed in
125.853 seconds (140.459 seconds total, 16 samples); process cleanup,
host-network fingerprint, namespace, qdisc, BPF, disk, temporary-file and
output checks passed. Container-volume inventory is unavailable, and one
cycle cannot establish resource drift. TCG timings are not compared with the
x86_64 host. Sanitized per-architecture summaries are in
`results/prompt52-validation/`.

The x86_64 held-connection scale test passed 100, 1,000 and 10,000 TCP
connections. It completed 11,100 echo requests with zero failures, closed all
connections, returned file descriptors from 7 to 7, and peaked at 128,912 KiB
RSS for the 10,000-connection trial. Its conditional successful-request P99 was
3.239 ms in an isolated namespace loopback test; this is not wire-performance
evidence. Both runtime profile snapshots reported the host network unchanged.
Sanitized local summaries are under `results/prompt52-validation/`.

No 24-hour qualified soak has been completed for this Prompt 52 change. The
existing short research runs and historical campaign evidence are not relabeled
as a long-duration soak. The machine report records `long_soak` as
`UNVERIFIED` until a qualifying run is retained. No leak-free, long-term
stability claim is made here.

## Reproduction

After the harness unit test and bounded integration run, inspect the run's
`report.json`, every step log, and `samples.jsonl`. Preserve invalid, interrupted
and workload-failed runs. The qualification engine consumes a retained report
using `--soak-report results/runtime-soak-*/report.json`; it does not infer a
pass from the existence of files.
