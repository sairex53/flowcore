"""Reboot and abrupt-QEMU-stop checks against a disposable qcow2 copy.

The VM uses QEMU TCG and user-mode networking bound to loopback. The base
image is opened only as a qcow2 backing file; all guest writes land in a new
overlay retained with the local qualification evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results'
DEFAULT_FIRMWARE_CODE = Path('/usr/share/AAVMF/AAVMF_CODE.fd')
DEFAULT_FIRMWARE_VARS = Path('/usr/share/AAVMF/AAVMF_VARS.fd')
FLOWCORE_UNITS = ('flowcore-monitor.service', 'flowcored.service',
                  'flowcore-sdk.socket', 'flowcore-network.socket')


class QualificationStop(Exception):
    """End the run with a recorded BLOCKED or FAIL result."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


def build_qemu_argv(qemu, base, overlay, vars_copy, firmware, port, memory_mb, cpus,
                    serial_log, qmp_socket):
    """Build a TCG-only VM command with no bridge or host interface changes."""
    return [str(qemu), '-machine', 'virt,accel=tcg', '-cpu', 'max', '-smp', str(cpus),
            '-m', str(memory_mb),
            '-drive', f'if=pflash,format=raw,readonly=on,file={firmware}',
            '-drive', f'if=pflash,format=raw,file={vars_copy}',
            '-drive', f'if=none,id=flowcore-disk,format=qcow2,file={overlay}',
            '-device', 'virtio-blk-pci,drive=flowcore-disk',
            '-netdev', f'user,id=flowcore-net,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:22',
            '-device', 'virtio-net-pci,netdev=flowcore-net',
            '-display', 'none', '-serial', f'file:{serial_log}', '-monitor', 'none',
            '-qmp', f'unix:{qmp_socket},server=on,wait=off']


def _run(argv, timeout=10, check=False):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=check)


def _qemu_info(image):
    result = _run(['qemu-img', 'info', '--output=json', str(image)], timeout=10, check=True)
    return json.loads(result.stdout)


def _base_fingerprint(path):
    stat_result = path.stat()
    return dict(size=stat_result.st_size, mtime_ns=stat_result.st_mtime_ns,
                inode=stat_result.st_ino, device=stat_result.st_dev)


def _ssh_args(key, port, user):
    return ['ssh', '-i', str(key), '-p', str(port), '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=4',
            '-o', 'LogLevel=ERROR', f'{user}@127.0.0.1']


def _guest(ssh, remote, timeout=10):
    return _run([*ssh, remote], timeout=timeout)


def _wait_ssh(ssh, qemu, timeout):
    deadline = time.monotonic() + timeout
    last_error = 'SSH has not connected'
    while time.monotonic() < deadline:
        if qemu.poll() is not None:
            return False, f'QEMU exited with status {qemu.returncode}'
        try:
            result = _guest(ssh, 'printf ready', timeout=5)
        except subprocess.TimeoutExpired:
            last_error = 'SSH readiness probe timed out'
            time.sleep(2)
            continue
        if result.returncode == 0 and result.stdout.strip() == 'ready':
            return True, None
        last_error = result.stderr.strip() or 'SSH probe failed'
        time.sleep(2)
    return False, last_error


def _boot_id(ssh):
    result = _guest(ssh, 'cat /proc/sys/kernel/random/boot_id')
    if result.returncode:
        raise RuntimeError('guest boot ID could not be read')
    return result.stdout.strip()


def _wait_reboot(ssh, qemu, previous, timeout):
    deadline = time.monotonic() + timeout
    observed_disconnect = False
    while time.monotonic() < deadline:
        if qemu.poll() is not None:
            return False, 'QEMU exited during guest reboot'
        try:
            result = _guest(ssh, 'cat /proc/sys/kernel/random/boot_id', timeout=5)
        except subprocess.TimeoutExpired:
            observed_disconnect = True
            time.sleep(2)
            continue
        if result.returncode:
            observed_disconnect = True
        elif result.stdout.strip() != previous and observed_disconnect:
            return True, result.stdout.strip()
        time.sleep(2)
    return False, 'guest reboot transition was not observed'


def _unit_inventory(ssh):
    result = {}
    for unit in FLOWCORE_UNITS:
        row = _guest(ssh, f'systemctl show --no-pager --property=LoadState,ActiveState,SubState {unit}')
        fields = {}
        for line in row.stdout.splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                fields[key] = value
        result[unit] = dict(available=row.returncode == 0, **fields)
    return result


def _guest_snapshot(ssh):
    commands = {
        'system': 'printf "arch="; uname -m; printf "kernel="; uname -r; printf "pid1="; cat /proc/1/comm',
        'flowcore_rpm': 'rpm -q flowcore 2>&1',
        'config_hash': 'if test -f /etc/flowcore/service.json; then sha256sum /etc/flowcore/service.json; else echo absent; fi',
        'network': 'ip -j link show; ip -4 -j route show table all; ip -6 -j route show table all; tc -j qdisc show',
        'flowcore_status': 'if command -v flowcore >/dev/null 2>&1; then flowcore status 2>&1; else echo unavailable; fi',
        'resource_counts': 'printf "threads="; ps -eLf | wc -l; printf "fd="; find /proc/[0-9]*/fd -mindepth 1 -maxdepth 1 2>/dev/null | wc -l; printf "mem="; awk "/MemAvailable:/ {print $2}" /proc/meminfo',
        'recovery_log': 'journalctl -b -1 --no-pager -p warning 2>&1 | tail -40',
    }
    result = {'boot_id': _boot_id(ssh), 'units': _unit_inventory(ssh), 'commands': {}}
    for name, command in commands.items():
        row = _guest(ssh, command, timeout=20)
        result['commands'][name] = dict(returncode=row.returncode,
                                        output=row.stdout[-16_384:],
                                        stderr=row.stderr[-4_096:])
    return result


def _start_qemu(argv, serial_path):
    log = serial_path.open('ab')
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=log, start_new_session=True)
    finally:
        log.close()
    return process


def _stop_qemu(process, force=False):
    if process.poll() is not None:
        return
    if force:
        process.send_signal(signal.SIGKILL)
    else:
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _validate_inputs(args):
    base = args.base_image
    if not args.disposable_copy:
        raise ValueError('explicit --disposable-copy acknowledgement is required')
    if base.is_symlink() or not base.is_file():
        raise ValueError('base image must be an existing regular file, not a symlink')
    output = args.output_dir.resolve()
    if base.resolve() == output or output in base.resolve().parents:
        raise ValueError('output directory cannot contain or replace the base image')
    if base.resolve() in output.parents:
        raise ValueError('output directory cannot be inside the base image directory')
    if args.output_dir.exists() or args.output_dir.is_symlink():
        raise ValueError('output directory must not already exist')
    if not args.ssh_key.is_file() or args.ssh_key.is_symlink():
        raise ValueError('a private key for passwordless SSH to the disposable guest is required')
    if stat.S_IMODE(args.ssh_key.stat().st_mode) & 0o077:
        raise ValueError('SSH private key permissions must be 0600 or stricter')
    if not args.firmware_code.is_file() or not args.firmware_vars.is_file():
        raise ValueError('AArch64 UEFI firmware is unavailable')
    if not 1024 <= args.memory_mb <= 65_536 or not 1 <= args.cpus <= 32:
        raise ValueError('VM memory or CPU setting is outside its safe bound')
    if not 30 <= args.boot_timeout <= 1800 or not 30 <= args.reboot_timeout <= 1800:
        raise ValueError('boot/reboot timeouts must be in [30, 1800] seconds')
    info = _qemu_info(base)
    if info.get('format') != 'qcow2' or info.get('backing-filename'):
        raise ValueError('base image must be a standalone qcow2 copy, not a backing overlay')
    return base.resolve()


def run_qualification(args):
    qemu = shutil.which('qemu-system-aarch64')
    qemu_img = shutil.which('qemu-img')
    ssh_bin = shutil.which('ssh')
    if not qemu or not qemu_img or not ssh_bin:
        raise FileNotFoundError('qemu-system-aarch64, qemu-img and ssh are required')
    base = _validate_inputs(args)
    args.output_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    args.output_dir.mkdir(mode=0o700, exist_ok=False)
    args.output_dir = args.output_dir.resolve()
    overlay = args.output_dir / 'disposable-overlay.qcow2'
    serial_log = args.output_dir / 'serial.log'
    vars_copy = args.output_dir / 'AAVMF_VARS.fd'
    qmp_socket = args.output_dir / 'qmp.sock'
    port_probe = socket.socket()
    port_probe.bind(('127.0.0.1', 0))
    port = port_probe.getsockname()[1]
    port_probe.close()
    ssh = _ssh_args(args.ssh_key.resolve(), port, args.ssh_user)
    base_before = _base_fingerprint(base)
    result = dict(schema_version=1, environment=dict(host_arch=os.uname().machine,
                   guest_arch='aarch64', qemu='qemu-system-aarch64/TCG', network='user-mode, loopback host-forward only'),
                  base_image=str(base), overlay=str(overlay), tests={}, status='RUNNING')
    qemu_proc = None
    try:
        shutil.copyfile(args.firmware_vars, vars_copy)
        _run([qemu_img, 'create', '-q', '-f', 'qcow2', '-F', 'qcow2', '-b', str(base),
              str(overlay)], timeout=30, check=True)
        overlay_info = _qemu_info(overlay)
        if overlay_info.get('backing-filename') != str(base):
            raise RuntimeError('overlay backing file did not resolve to the declared copy')
        argv = build_qemu_argv(qemu, base, overlay, vars_copy, args.firmware_code,
                               port, args.memory_mb, args.cpus, serial_log, qmp_socket)
        result['qemu_mode'] = 'tcg'
        result['network_mode'] = 'user-mode; host-forward bound to 127.0.0.1'
        qemu_proc = _start_qemu(argv, serial_log)
        ready, error = _wait_ssh(ssh, qemu_proc, args.boot_timeout)
        if not ready:
            result['tests']['initial_boot'] = dict(status='BLOCKED', reason=error)
            raise QualificationStop('BLOCKED', error)
        before = _guest_snapshot(ssh)
        system_probe = before['commands']['system']['output'].lower()
        if (before['commands']['system']['returncode'] != 0 or 'aarch64' not in system_probe
                or 'systemd' not in system_probe):
            result['tests']['initial_boot'] = dict(status='FAIL', reason='guest architecture or systemd probe mismatch')
            raise QualificationStop('FAIL', 'guest architecture or systemd probe mismatch')
        result['tests']['initial_boot'] = dict(status='PASS', architecture='aarch64',
                                               init='systemd', snapshot=before)
        installed_units = {name: row for name, row in before['units'].items()
                           if row.get('LoadState') not in (None, 'not-found')}
        package_installed = before['commands']['flowcore_rpm']['returncode'] == 0
        result['tests']['installed_runtime'] = dict(
            status='PASS' if package_installed and installed_units else 'BLOCKED',
            package_installed=package_installed, installed_units=installed_units,
            reason=None if package_installed and installed_units else
            'FLOWCORE RPM or service units are not installed in this disposable guest')
        marker_id = uuid.uuid4().hex
        marker = f'/var/tmp/flowcore-reboot-{marker_id}'
        marker_value = f'flowcore-reboot-{marker_id}'
        write = _guest(ssh, f"printf '%s' '{marker_value}' > '{marker}' && sync")
        if write.returncode:
            raise RuntimeError('could not write reboot persistence marker')
        try:
            q = _guest(ssh, 'systemctl reboot', timeout=5)
        except subprocess.TimeoutExpired:
            q = subprocess.CompletedProcess(ssh, 124, '', 'SSH reboot request timed out')
        clean, boot_or_error = _wait_reboot(ssh, qemu_proc, before['boot_id'], args.reboot_timeout)
        if clean:
            persisted = _guest(ssh, f"cat '{marker}'")
            after = _guest_snapshot(ssh)
            result['tests']['clean_reboot'] = dict(
                status='PASS' if persisted.stdout.strip() == marker_value and
                after['boot_id'] != before['boot_id'] else 'FAIL',
                reboot_returncode=q.returncode, boot_id_before=before['boot_id'],
                boot_id_after=after['boot_id'], marker_persisted=persisted.stdout.strip() == marker_value,
                before=before, after=after)
            config_before = before['commands']['config_hash']['output'].strip()
            config_after = after['commands']['config_hash']['output'].strip()
            result['tests']['configuration_persistence'] = dict(
                status=('PASS' if config_before == config_after and config_before != 'absent'
                        else 'UNVERIFIED'), before=config_before, after=config_after,
                reason=None if config_before == config_after and config_before != 'absent'
                else 'FLOWCORE config is absent or its hash changed')
            unit_states_before = {name: (row.get('LoadState'), row.get('ActiveState'))
                                  for name, row in installed_units.items()}
            unit_states_after = {name: (after['units'].get(name, {}).get('LoadState'),
                                        after['units'].get(name, {}).get('ActiveState'))
                                 for name in installed_units}
            required_units = [name for name in installed_units
                              if name.endswith('.socket') or
                              before['units'][name].get('ActiveState') == 'active']
            service_ok = bool(required_units) and all(
                unit_states_after[name][0] not in (None, 'not-found')
                and unit_states_after[name][1] == 'active' for name in required_units)
            result['tests']['service_startup'] = dict(
                status='PASS' if service_ok else 'BLOCKED', before=unit_states_before,
                after=unit_states_after, reason=None if service_ok else
                'no installed active FLOWCORE service/socket was verified after reboot')
        else:
            result['tests']['clean_reboot'] = dict(status='FAIL', reason=boot_or_error,
                                                  before=before)
            raise QualificationStop('FAIL', boot_or_error)
        current_id = _boot_id(ssh)
        marker_value = f'flowcore-abrupt-{uuid.uuid4().hex}'
        abrupt_write = _guest(ssh, f"printf '%s' '{marker_value}' > '{marker}' && sync")
        if abrupt_write.returncode:
            raise RuntimeError('could not stage abrupt-stop marker')
        _stop_qemu(qemu_proc, force=True)
        qemu_proc = None
        qemu_proc = _start_qemu(argv, serial_log)
        ready, error = _wait_ssh(ssh, qemu_proc, args.boot_timeout)
        if ready:
            persisted = _guest(ssh, f"cat '{marker}'")
            recovered = _guest_snapshot(ssh)
            result['tests']['abrupt_qemu_stop'] = dict(
                status='PASS' if persisted.stdout.strip() == marker_value and
                recovered['boot_id'] != current_id else 'FAIL',
                boot_id_before=current_id, boot_id_after=recovered['boot_id'],
                marker_persisted=persisted.stdout.strip() == marker_value,
                after=recovered,
                interpretation='QEMU process SIGKILL and guest filesystem recovery; not host power-loss validation')
        else:
            result['tests']['abrupt_qemu_stop'] = dict(status='FAIL', reason=error,
                interpretation='QEMU process SIGKILL and restart failed; not host power-loss validation')
    except QualificationStop as error:
        result['status'] = error.status
        result['stop_reason'] = str(error)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result['error'] = dict(type=type(error).__name__, message=str(error))
        result['status'] = 'FAIL'
    finally:
        if qemu_proc is not None:
            _stop_qemu(qemu_proc)
    result['base_image_unchanged'] = _base_fingerprint(base) == base_before
    if result.get('status') == 'RUNNING':
        statuses = [row.get('status') for row in result['tests'].values()]
        if statuses and all(status == 'PASS' for status in statuses):
            result['status'] = 'PASS'
        elif 'FAIL' in statuses:
            result['status'] = 'FAIL'
        elif 'BLOCKED' in statuses:
            result['status'] = 'BLOCKED'
        else:
            result['status'] = 'INCOMPLETE'
    report_path = args.output_dir / 'reboot-qualification.json'
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n')
    print(json.dumps(dict(status=result['status'], report=str(report_path),
                          base_image_unchanged=result.get('base_image_unchanged')),
                     sort_keys=True))
    return 0 if result['status'] == 'PASS' else (2 if result['status'] in ('BLOCKED', 'INCOMPLETE') else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path,
                        default=RESULTS / f'reboot-qualification-{time.time_ns()}')
    parser.add_argument('--ssh-key', type=Path, required=True,
                        help='private key already authorized in the disposable guest')
    parser.add_argument('--ssh-user', default='root')
    parser.add_argument('--disposable-copy', action='store_true',
                        help='acknowledge that --base-image is a disposable copy')
    parser.add_argument('--firmware-code', type=Path, default=DEFAULT_FIRMWARE_CODE)
    parser.add_argument('--firmware-vars', type=Path, default=DEFAULT_FIRMWARE_VARS)
    parser.add_argument('--memory-mb', type=int, default=4096)
    parser.add_argument('--cpus', type=int, default=4)
    parser.add_argument('--boot-timeout', type=int, default=300)
    parser.add_argument('--reboot-timeout', type=int, default=300)
    args = parser.parse_args(argv)
    try:
        return run_qualification(args)
    except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
