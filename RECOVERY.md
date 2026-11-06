# Recovery semantics and boundaries

Each daemon start writes a private recovery.json checkpoint before READY. It contains schema version, configuration generation/hash, start count, boot ID and clean-shutdown flag. SIGTERM/SIGINT close owned lab resources and sockets, then mark clean shutdown. SIGKILL leaves an unclean checkpoint and stale socket; the next start takes exclusive runtime/state locks, validates state, removes only an owned socket and records the recovery. A foreign file/symlink at the socket path is rejected.

Persistent platform policy revisions and persistent legacy operator intents remain separate state files. Their writes now use exclusive private temporary files, file fsync, atomic rename and directory fsync. Audit-history corruption is logged and does not authorize activation. Invalid recovery/policy/intent state fails closed without silently resetting it. Ephemeral SDK leases, workload process registrations, socket cookies, ifindices, BPF links and namespace IDs are not restored as authority.

Configuration generations advance on accepted reloads and changed startup configurations. Startup always inspects actual interfaces through read-only rtnetlink inventory. Reconciliation currently means observation plus conflict rejection in the existing owned lab/helper. It does not rebuild lost qdiscs or replay host changes. Controllers restart disabled. The established helper retains ownership checks and verified incremental changes; no stronger transactional claim is added.

Fsync/rename cannot make a multi-file configuration update or a multi-operation Linux policy update atomic. PolicyStore updates memory once replacement commits, even if parent-directory fsync then fails. Persistent legacy-intent callers receive a durability error if replacement committed but its directory could not be synchronized; inspect/restart before retrying. Backups and checkpoints are not a distributed journal or exactly-once execution system.

Manual state recovery:
1. Stop all FLOWCORE services using that state directory.
2. Preserve the corrupt file and journal for diagnosis.
3. Restore a known-compatible backup, verify owner/mode/schema, and validate configuration.
4. Restart in read-only mode and inspect ops status/inventory.
5. Re-register ephemeral applications and explicitly recreate a lab if wanted.
Never reuse a saved kernel object ID as proof of ownership.

Tested abrupt termination is SIGKILL, including automatic restart under a transient systemd sandbox using the extracted RPM payload, not a real machine power cut. A genuine reboot, filesystem crash recovery, controller kill during a privileged update, and helper/BPF failure in an installed active deployment still require dedicated destructive test infrastructure. Default read-only deployment cannot leave host network changes behind because it cannot make them.
