# Audit fixes and reproduction

Run commands from the project root. Privileged tests require a stopped
FLOWCORE daemon and create disposable veth namespaces.

| ID and severity | Root cause and evidence | Fix and reproduction |
|---|---|---|
| F1 High | `Lab.ns()` looked up numeric PIDs after liveness checks. Veth peer placement, setns and BPF inode selection had separate PID lookups. Research pinning ran after topology creation. | Open and verify distinct non-host network namespace descriptors during startup, then use them for commands, peer placement, setns and BPF identity. All base holders now exit when their owner dies. `sudo python3 -B tests/platform_integration.py` checks distinct descriptors and live recovery. Service, legacy, MPTCP and Podman tests exercise the same base path. iproute2 accepts a namespace file for peer placement ([upstream source](https://github.com/iproute2/iproute2/blob/main/ip/iplink.c)). |
| F2 High | `flowcore --config` could apply HTB changes directly from the initial namespace. The shipped lab config named one developer checkout. A broken telemetry pipe also bypassed rollback via SIGPIPE. | Require a matching lab namespace path and reject the PID 1 network namespace before actuation. The benchmark supplies its pinned namespace and selected tc executable. Broken telemetry triggers the controller error rollback. `sudo python3 -B scripts/integration_test.py` tests host rejection, real class readback and broken-pipe rollback. |
| F3 High | Research lock and predictable marker temp file were opened beneath the shared result directory. Daemon lock opens followed symlinks. | Keep research control files in the private runtime directory. Publish the marker atomically from an exclusive temporary file. Check directory ownership and permissions, and reject symlinks, hard links and non-regular lock files. `python3 -B tests/platform_test.py` and `sudo python3 -B tests/research_integration.py` cover rejection and scoped cancellation. |
| F4 High | A failed research update could leave some HTB, netem or link changes applied while the lab stayed active and the cached profile was stale. | Keep input validation outside the mutation boundary. Close the disposable lab and surface a failed update if a mutation or its readback fails. `python3 -B tests/research_test.py` injects a kernel-operation failure and checks that a bad mode request leaves the lab intact. |
| F5 Medium | Receiver interpolation accepted counter resets, changing flow vector lengths and duplicate timestamps. A run with no successful latency samples could fail while printing its summary. | Reject invalid samples and intervals. Print an unavailable P99 without replacing the recorded failed-run evidence. `python3 -B tests/research_test.py` covers correct two-flow rates and rejects counter regression, NaN, duplicate timestamps and mismatched stream counts. |
| F6 Medium | Native RTM_GETLINK used recv without checking sender or truncation, and required the newest full stats structure. The SDK reply cap could be exceeded by its last read. | Check kernel sender and truncation, accept the stable stats prefix actually used, and limit every SDK receive to the remaining budget. Run `./build/flowcore --link lo` and `sudo python3 -B tests/adaptive_integration.py` for live coverage. Malformed native datagrams and oversized SDK replies were not injected end to end. |
| F7 Medium | The container identity used multiple numeric proc paths and no task start time. Reads could span PID replacement. | Anchor reads to one proc directory FD and include the task start ticks in the identity checked around nft setup and on subsequent samples. `sudo ./build/flowcore platform container-lab` exercises attachment, counters and removal. Forced PID reuse was not injected. |
| F8 Medium | The source launcher accepted Python environment injection that the installed launcher ignored. The systemd unit pointed strict leaf validation at a DynamicUser-managed state path that can be a symlink. | Use `-E -s` in both launchers. Give the daemon a private leaf below the manager-provided state directory. `python3 -B tests/install_test.py` exercises a staged monitor with an equivalent symlinked parent and a real private state leaf. Actual unit activation remains open. [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#RuntimeDirectory=) |

Local logs are in `results/audit-*.log`. They contain raw validation
evidence and are deliberately ignored by Git.

## Validation on Fedora 45 AArch64

- `results/audit-clean-on.log` and `results/audit-clean-off.log` record
  separate builds and 6/6 CTest passes in each configuration. The ON
  configuration produced the native helper and CO-RE object.
- `results/audit-install-on.log` and `results/audit-install-off.log`
  record staged monitor, SDK consumer and manifest-based uninstall checks.
  The installed monitor ran with zero effective capabilities.
- `results/audit-live.log` records the legacy controller, daemon service,
  adaptive controller and SDK, platform recovery, and research replay,
  minimization and cancellation. The five-mode benchmark smoke completed
  one trial each for FIFO, fq_codel, CAKE, static HTB and legacy FLOWCORE.
  The host link, route and qdisc JSON snapshots matched byte-for-byte.
- The same live log records 4,587,520 verified MPTCP bytes across a
  connection with two reported subflows and no fallback. Podman reported
  40 successful requests, 249 matched egress packets and classifier removal.
- The final BPF sample in
  `results/chaos-1793842788283452683/r0-fq_codel/timeline.jsonl`
  recorded three attached TCP hooks and 2,232 processed events with zero
  reported kernel-reserve, pipe-transport and parse drops in that run.
- `results/audit-legacy-final.log` and `results/audit-platform-final.log`
  cover the added broken-pipe and dead-holder regressions.
