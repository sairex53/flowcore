# FLOWCORE Network Intelligence Dashboard

The dashboard shows live interfaces and queue rates, application SLOs, controller decisions, bottleneck evidence, incidents, MPTCP inventory and recorded experiments. It binds to IPv4 loopback by default and requires a bearer token for every API request. Its HTTP interface exposes no control operations.

## Local development

Build FLOWCORE as usual. Start a dedicated monitor profile in one shell from the checkout:

```sh
mkdir -m 700 -p results/dashboard-local
(
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > results/dashboard-local/token
)
FLOWCORE_RUNTIME="$PWD/results/dashboard-local/daemon" \
FLOWCORE_STATE="$PWD/results/dashboard-local/daemon" \
build/flowcored --monitor-only
```

Start the unprivileged dashboard in another shell as the same user:

```sh
python3 -B scripts/dashboard.py \
  --socket "$PWD/results/dashboard-local/daemon/control.sock" \
  --token-file "$PWD/results/dashboard-local/token" \
  --state "$PWD/results/dashboard-local/history"
```

Open `http://127.0.0.1:8844/` and paste the token into the login form. The browser keeps it in page memory. Locking or reloading clears access. Never place the token in a URL. A monitor profile may have interface data but no instrumented application requests, controlled classes or retained incidents. Those sections show their missing-data state.

For an operating daemon, use the explicit delegation flags `--monitor-socket flowcore.dashboard --monitor-user <dashboard-account>`. Run the dashboard as that separate account with `--socket @flowcore.dashboard --daemon-user <flowcored-account>`. Use the numeric UID `0` only when monitoring an existing privileged lab daemon. Both processes must be in the same network namespace to use an abstract socket. The dashboard process itself refuses root. A same-user pathname source is suitable for the dedicated local monitor profile. It does not isolate the web process from other same-user IPC.

## History and experiments

`--interval` controls polling from 1 to 60 seconds. `--window` requests a daemon rolling window, using the nearest configured one when needed. Each service reports the effective duration. `--retention-hours`, `--max-samples`, and `--storage-mib` bound the local SQLite history. Defaults are 24 hours, 7,200 snapshots, and a 64 MiB database. Whichever limit is reached first determines the retained horizon. Storage pressure can evict older samples. A rollback journal can temporarily add one transaction of disk usage. Keep the private state directory writable and reserve space for the journal.

Enable artifacts explicitly with repeatable flags such as:

```text
--artifact app=results/real-apps-validation-20261107/results.json
--artifact fabric=results/fabric-measured-20261107/results.json
--artifact mptcp=results/multipath-1794038734196497598/summary.json
```

The supported formats are the real application lab matrix, Fabric comparison matrix and MPTCP laboratory summary. Paths are operator configuration. Clients select IDs and run numbers. Explicitly simulated artifacts are labeled and excluded from measured comparison charts. Invalid runs are retained in the table. The explorer plots one successful-request P99 per recorded repetition and shows the run configuration, including capacity differences where recorded. The artifact source is not attested by the dashboard.

## Systemd

The installed unit runs as `flowcore-dashboard` with no capabilities and reads the token through a systemd credential. It is opt-in. Create the account with `sudo systemd-sysusers /usr/lib/sysusers.d/flowcore-dashboard.conf`. Provision a root-owned `/etc/flowcore/dashboard.token` with mode 0600 and a fresh URL-safe token. Use a drop-in for `flowcore-monitor.service`:

```ini
[Service]
User=flowcore-monitor
ExecStart=
ExecStart=/usr/bin/flowcored --monitor-only --config /etc/flowcore/service.json --monitor-socket flowcore.dashboard --monitor-user flowcore-dashboard
```

Then reload the systemd configuration and start the monitor and dashboard units explicitly. Keep the monitor's private runtime directory private. To add artifacts, use an `ExecStart` override and place their read-only copies in a directory the dashboard account can read. The dashboard has `ProtectHome=yes`, so use a controlled location outside users' homes. Rotate the bearer token by replacing the credential and restarting the dashboard.

For remote access, use an SSH port forward such as ssh -L 8844:127.0.0.1:8844 HOST or an operator-managed HTTPS reverse proxy on a private management network. [The example proxy configuration](monitoring/dashboard-nginx.conf) preserves the Authorization header and uses an exact Host header. Add the matching `--public-host` to the dashboard service. Keep the dashboard bound to loopback, avoid logging credentials, and restrict proxy access to approved operators. The HTTP service has one shared bearer credential, so it does not provide per-user audit identity or tenant separation.

## Validation

The API and storage checks are part of CTest. The privileged fixture uses disposable namespaces, real HTTP requests and a verified reverse-path delay to create an incident. It tests delegated socket access, MPTCP inventory when the kernel supports it, and stale-source handling.

```sh
ctest --test-dir build -R dashboard_unit --output-on-failure
sudo python3 -B tests/dashboard_live.py --output results/dashboard-reproduction
```

For browser coverage, install Playwright and Chromium in an ignored test directory and pass that directory as `--browser-root`. The fixture expects `node_modules` and `browsers` subdirectories and can use native libraries under `libs/usr/lib64`. Use `npm install --prefix results/dashboard-browser playwright` and set `PLAYWRIGHT_BROWSERS_PATH` to that directory's `browsers` subdirectory when running its Playwright installer. Pass explicit `--artifact app=...` and `--artifact mptcp=...` for recorded chart and path coverage. The browser test exercises token entry, chart rendering, automatic refresh, all seven tabs, event filtering, mobile width and lock. Screenshots remain in the ignored output directory.

On 2026-11-08, the Fedora 45 ARM64 QEMU fixture completed 720 measured HTTP requests. A verified 45 ms netem delay on the receiver egress produced a retained SLO violation. Chromium rendered all seven views, including real application and multipath artifacts. The test exercised a live MPTCP socket and detected stale telemetry after the daemon stopped. Raw validation and desktop/mobile screenshots are under \`results/dashboard-verified-20261108/\` on the VM. This was a namespace and browser validation on one Linux boot.

See [MONITORING_API.md](MONITORING_API.md) for the response contract and [UI_ARCHITECTURE.md](UI_ARCHITECTURE.md) for data provenance and boundaries.
