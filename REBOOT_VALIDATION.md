# Disposable VM reboot and recovery validation

## Harness and isolation

`scripts/reboot_qualification.py` runs only an AArch64 qcow2 **copy** that the
operator explicitly acknowledges with `--disposable-copy`. It creates a fresh
qcow2 overlay and private UEFI variable copy under a new results directory; all
guest disk writes go to the overlay. QEMU uses TCG, `virt`, a restricted
user-mode network, and SSH forwarding bound to `127.0.0.1`. It does not use a
tap device, bridge, host route, or host reboot. The report compares the backing
image's size, mtime, inode and device before and after the run.

The copy must already permit key-only SSH for the selected guest account. The
harness never enables password login or edits the source image to install a
key. A failed initial SSH probe is recorded as `BLOCKED` with a report and the
QEMU process is stopped.

## Checks

The guest snapshot records architecture, kernel, PID 1, package/config state,
FLOWCORE systemd unit states, network/qdisc state, resource counts and prior
boot warning logs. The runner writes and syncs a marker, requests a clean
`systemctl reboot`, waits for a changed boot ID, and verifies the marker and
configuration hash. It records installed package/service startup separately;
missing installed runtime components remain blocked. It then writes another
marker, sends `SIGKILL` to the QEMU process, restarts QEMU from the same
overlay, and checks boot ID and marker recovery.

The abrupt-stop result is specifically **QEMU process termination plus guest
filesystem recovery**. It is not host power loss, physical power interruption,
or proof of storage-device durability. A filesystem marker after a clean reboot
does not establish all FLOWCORE ownership or rollback invariants; those require
the corresponding service and fault-injection evidence.

Example:

```sh
python3 -B scripts/reboot_qualification.py \
  --base-image /path/to/standalone-disposable-copy.qcow2 \
  --ssh-key /path/to/private-qualification-key \
  --disposable-copy
```

The generated `reboot-qualification.json`, serial log, overlay, and UEFI vars
are retained together. Do not delete a failed overlay before its report has
been reviewed.

## Current run record

The harness ran Fedora 45 ARM64 under QEMU TCG using a separate converted
disposable base and a new overlay. Initial systemd boot passed. A guest
`systemctl reboot` changed the boot ID and preserved the synced marker. After a
second marker write and QEMU `SIGKILL`, the same overlay booted again, changed
boot ID, and preserved the marker. The disposable backing image fingerprint was
unchanged.

The overall reboot qualification is **BLOCKED** because the guest copy does not
have the FLOWCORE RPM, active systemd units or `/etc/flowcore/service.json`.
Thus installed service startup and configuration persistence were not verified;
the successful OS reboot and filesystem-recovery checks do not stand in for
them. The detailed raw report and overlay were retained outside the repository;
the sanitized machine summary is `results/prompt52-validation/reboot-report.json`.
Genuine host power loss and a primary-host reboot were not attempted.
