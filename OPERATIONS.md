# Operational controls

The installed monitor can be queried by root through its private filesystem Unix socket. Ordinary users can run their own read-only daemon with separate private paths.

```sh
flowcored --check-config --config /etc/flowcore/service.json
flowcore ops status
flowcore ops validate
flowcore ops diagnostics > diagnostic.json
flowcore metrics --prometheus
```

ops validate validates the daemon's configured file, reports a top-level diff and rejects changes requiring restart. Diagnostics contain bounded operational/kernel inventories and BPF state, not lease secrets. Treat addresses and service metadata as operationally sensitive when sharing exports.

ops reload is available on an owner-controlled non-monitor daemon. The shipped monitor rejects mutating IPC operations; reload it using systemctl reload flowcored, which sends SIGHUP. Only log_level and telemetry_seconds can change live. Network policy definitions, service permission changes, observability/BPF settings, controller parameters and topology changes require a validated restart. The daemon never reads an arbitrary reload path supplied by a client.

Reload validates and checks compatibility, writes the next configuration-generation checkpoint, then adopts the candidate. Failures before replacement retain the old generation/configuration. A directory-fsync failure after replacement is reported as committed but durability unconfirmed, and in-memory configuration matches the replacement. This is not a kernel transaction or a guarantee of survival through arbitrary storage failure.

Existing controller shadow/observe/fallback/stop and policy dry-run commands remain laboratory-only. ops emergency closes the caller's owned lab and detaches its controller. It is rejected by read-only IPC. Stop the monitor with systemctl stop; it has no host policy to undo. There is no global qdisc/nft/BPF reset command.

Metrics include existing daemon CPU/RSS, observer and BPF health/loss, controller state/actions, interface events, policy revision and service-reported SLOs. New metrics record configuration generation, reload errors, previous unclean shutdown and host-mutation enablement. Reload error counts are per daemon lifetime; a failed reload can leave a healthy daemon serving the last valid configuration.

Troubleshooting:
- Startup fails: journalctl -u flowcored; check configuration ownership/schema and private state permissions.
- Restart throttled: fix the cause, validate, then systemctl reset-failed flowcored and restart.
- Corrupt recovery/policy state: stop the daemon and preserve a copy; follow RECOVERY.md. Do not overwrite it with an empty file while running.
- BPF unavailable: the RPM intentionally omits it; monitor mode does not require it.
- Missing interface: monitoring reports current inventory and does not create an interface. Lab loss is handled by owned-holder checks.
- Unsupported reload diff: retain the working daemon until a planned restart.
- Checkpoint write failure: inspect filesystem space/permissions; do not claim a reload succeeded from an old client connection alone.
