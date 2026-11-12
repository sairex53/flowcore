"""Fail-closed FLOWCORE release qualification report generator.

It translates the existing release gate snapshot and retained Prompt 52
evidence into explicit PASS/FAIL/BLOCKED/UNVERIFIED states. It never treats a
missing report or a short smoke run as release evidence.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GATE_FILE = Path('config/release-gates.json')
GATE_NAMES = ('functional', 'security', 'performance_evidence', 'reproducibility',
              'portability', 'documentation', 'packaging', 'reliability', 'api_stability')
STATES = {'PASS', 'FAIL', 'BLOCKED', 'UNVERIFIED'}
PARTIAL_MAP = {'PASS': 'PASS', 'PARTIAL': 'UNVERIFIED', 'FAIL': 'FAIL',
               'BLOCKED': 'BLOCKED', 'UNVERIFIED': 'UNVERIFIED'}
PROMPT52_GATES = ('long_soak', 'reboot_and_recovery', 'power_loss', 'physical_network',
                  'cross_architecture', 'concurrency', 'fault_injection',
                  'resource_safety', 'api_and_package_lifecycle', 'signed_distribution')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _relative_file(root, name, max_bytes=16 * 1024 * 1024):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('evidence paths must be relative and cannot traverse directories')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink evidence paths are not accepted')
    resolved = current.resolve(strict=True)
    if root.resolve() not in resolved.parents:
        raise ValueError('evidence path escapes repository')
    if not resolved.is_file() or resolved.stat().st_size > max_bytes:
        raise ValueError('evidence must be a regular file within its size limit')
    return resolved


def _read_json(root, name):
    path = _relative_file(root, name)
    data = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    if not isinstance(data, dict):
        raise ValueError(f'{name} must contain a JSON object')
    return data, path


def _git_commit(root):
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                                 capture_output=True, text=True, timeout=3)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _git_state(root):
    """Fingerprint tracked edits and untracked source files without exposing contents."""
    try:
        report_path = 'config/release-qualification.json'
        tracked = subprocess.run(['git', 'diff', '--binary', 'HEAD', '--', '.',
                                  f':(exclude){report_path}'], cwd=root,
                                 check=True, capture_output=True, timeout=10).stdout
        untracked = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '-z'],
                                   cwd=root, check=True, capture_output=True, timeout=5).stdout
        digest = hashlib.sha256()
        digest.update(tracked)
        names = [name for name in untracked.split(b'\0')
                 if name and name.decode('utf-8', errors='surrogateescape') != report_path]
        for raw_name in sorted(names):
            path = root / raw_name.decode('utf-8', errors='surrogateescape')
            if path.is_symlink() or not path.is_file():
                digest.update(b'unsafe-untracked-path\0' + raw_name + b'\0')
                continue
            digest.update(raw_name + b'\0')
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
        return dict(dirty=bool(tracked or names), diff_sha256=digest.hexdigest())
    except (OSError, subprocess.SubprocessError):
        return dict(dirty=None, diff_sha256=None)


def _soak_steps(root, data):
    steps = data.get('steps')
    if not isinstance(steps, list) or not steps:
        return dict(valid=False, count=0, reason='soak report has no completed steps')
    log_hashes = []
    for step in steps:
        if (not isinstance(step, dict) or step.get('status') != 'PASS'
                or step.get('returncode') != 0):
            return dict(valid=False, count=len(steps), reason='one or more soak steps did not pass')
        leaks = step.get('leaked_processes')
        leak_count = step.get('leaked_process_count')
        if (not isinstance(leaks, list) and type(leak_count) is not int):
            return dict(valid=False, count=len(steps), reason='soak step has no process-leak accounting')
        if ((isinstance(leaks, list) and leaks)
                or (leak_count is not None and leak_count != 0)):
            return dict(valid=False, count=len(steps), reason='soak step recorded a leaked process')
        try:
            log = _relative_file(root, step.get('log'), max_bytes=64 * 1024 * 1024)
        except (OSError, ValueError, TypeError):
            return dict(valid=False, count=len(steps), reason='a soak step log is missing or invalid')
        log_hashes.append(hashlib.sha256(log.read_bytes()).hexdigest())
    return dict(valid=True, count=len(steps), log_sha256=log_hashes)


def _baseline_gates(root):
    data, path = _read_json(root, GATE_FILE.as_posix())
    if (set(data) != {'version', 'scope', 'license_approved', 'production_ready', 'gates'}
            or data.get('version') != 1 or data.get('production_ready') is not False
            or type(data.get('license_approved')) is not bool
            or not isinstance(data.get('gates'), dict)
            or set(data['gates']) != set(GATE_NAMES)):
        raise ValueError('release-gates.json does not match the version 1 schema')
    gates = {}
    for name in GATE_NAMES:
        state = data['gates'][name]
        if not isinstance(state, str) or state not in PARTIAL_MAP:
            raise ValueError(f'unsupported current gate state for {name}')
        gates[name] = dict(status=PARTIAL_MAP[state], source='config/release-gates.json',
                           declared_status=state)
    if not data['license_approved']:
        gates['security'] = dict(status='BLOCKED', source='config/release-gates.json',
                                 reason='owner license approval is absent')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return data, gates, digest


def _report_status(root, name, minimum_seconds=86_400):
    if not name:
        return dict(status='BLOCKED', reason='no retained evidence report supplied')
    try:
        data, path = _read_json(root, name)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return dict(status='FAIL', reason=f'evidence report is invalid: {type(error).__name__}')
    if name.endswith('runtime-soak-report.json') or data.get('profile') in ('full', 'service'):
        regression = data.get('regression', {}).get('resource_gates', {})
        statuses = [row.get('status') for row in regression.values() if isinstance(row, dict)]
        duration = data.get('elapsed_seconds', 0)
        if data.get('status') == 'FAIL':
            return dict(status='FAIL', path=str(path.relative_to(root)),
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        reason='soak runner recorded a failed test or resource gate')
        if data.get('status') == 'BLOCKED':
            return dict(status='BLOCKED', path=str(path.relative_to(root)),
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        reason='soak runner could not execute the requested profile')
        checks = (data.get('schema_version') == 1 and data.get('status') == 'PASS'
                  and data.get('profile') in ('full', 'service')
                  and type(duration) in (int, float) and duration >= minimum_seconds
                  and type(data.get('samples')) is int and data['samples'] >= 20
                  and statuses and all(s == 'PASS' for s in statuses))
        steps = _soak_steps(root, data)
        checks = checks and steps['valid']
        return dict(status='PASS' if checks else 'UNVERIFIED', path=str(path.relative_to(root)),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    elapsed_seconds=duration, minimum_seconds=minimum_seconds,
                    profile=data.get('profile'), resource_gate_states=statuses,
                    steps=steps,
                    reason=None if checks else 'report lacks a complete 24-hour service/full soak, passing resource gates, successful steps, or retained logs')
    tests = data.get('tests', {})
    boot = tests.get('initial_boot', {}).get('status')
    clean = tests.get('clean_reboot', {}).get('status')
    abrupt = tests.get('abrupt_qemu_stop', {}).get('status')
    lifecycle = [tests.get(name, {}).get('status') for name in
                 ('installed_runtime', 'configuration_persistence', 'service_startup')]
    statuses = (boot, clean, abrupt, *lifecycle)
    if any(value == 'FAIL' for value in statuses):
        state = 'FAIL'
    elif any(value == 'BLOCKED' for value in statuses):
        state = 'BLOCKED'
    else:
        state = 'UNVERIFIED'
    checks = (data.get('schema_version') == 1 and data.get('base_image_unchanged') is True
              and all(value == 'PASS' for value in statuses))
    return dict(status='PASS' if checks else state, path=str(path.relative_to(root)),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                clean_reboot=clean, abrupt_qemu_stop=abrupt,
                lifecycle_statuses=lifecycle,
                base_image_unchanged=data.get('base_image_unchanged'),
                reason=None if checks else 'reboot/recovery report is incomplete or failed')


def _inventory_status(data, section):
    row = data.get(section) if isinstance(data, dict) else None
    if not isinstance(row, dict) or row.get('status') not in STATES:
        return dict(status='UNVERIFIED', reason=f'{section} evidence is absent or has no recognized state')
    return {**row, 'status': row['status']}


def evaluate(root, *, soak_report=None, reboot_report=None, validation_manifest=None,
             minimum_soak_seconds=86_400):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('root must be a repository directory')
    baseline, gates, baseline_sha = _baseline_gates(root)
    prompt52 = {
        'long_soak': _report_status(root, soak_report, minimum_soak_seconds),
        'reboot_and_recovery': _report_status(root, reboot_report),
        'power_loss': dict(status='UNVERIFIED', reason='QEMU process interruption is not host power-loss validation'),
    }
    manifest_path = None
    if validation_manifest:
        manifest, manifest_path = _read_json(root, validation_manifest)
        manifest_fields = set(PROMPT52_GATES) - {'long_soak', 'reboot_and_recovery', 'power_loss'}
        if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
                or set(manifest) != manifest_fields | {'schema_version'}):
            raise ValueError('Prompt 52 validation manifest does not match schema version 1')
        for section, name in (('physical_network', 'physical_network'),
                              ('cross_architecture', 'cross_architecture'),
                              ('concurrency', 'concurrency'),
                              ('fault_injection', 'fault_injection'),
                              ('resource_safety', 'resource_safety'),
                              ('api_and_package_lifecycle', 'api_and_package_lifecycle'),
                              ('signed_distribution', 'signed_distribution')):
            prompt52[name] = _inventory_status(manifest, section)
    else:
        for name in PROMPT52_GATES:
            if name not in prompt52:
                prompt52[name] = dict(status='UNVERIFIED', reason='no validation manifest supplied')
    if prompt52['physical_network']['status'] == 'UNVERIFIED':
        prompt52['physical_network'] = dict(
            status='BLOCKED', reason='physical NIC with a controlled peer was not validated')
    if prompt52['signed_distribution']['status'] == 'UNVERIFIED':
        prompt52['signed_distribution'] = dict(
            status='BLOCKED', reason='no operator signing key and verified signed package evidence')
    if prompt52['long_soak']['status'] != 'PASS':
        gates['reliability'] = dict(status='BLOCKED', source='Prompt 52 soak gate',
                                    reason=prompt52['long_soak'].get('reason'))
    elif gates['reliability']['status'] != 'PASS':
        gates['reliability'] = dict(status='UNVERIFIED', source='prior release gate remains incomplete',
                                    evidence_status='soak passed; remaining lifecycle gates are not all demonstrated')
    for gate_name, evidence_name in (('portability', 'cross_architecture'),
                                     ('packaging', 'api_and_package_lifecycle')):
        if prompt52[evidence_name]['status'] in ('BLOCKED', 'FAIL'):
            gates[gate_name] = dict(status=prompt52[evidence_name]['status'],
                                    source=f'Prompt 52 {evidence_name}')
    blockers = [name for name, row in gates.items() if row['status'] != 'PASS']
    blockers.extend(f'prompt52:{name}' for name, row in prompt52.items()
                    if row['status'] != 'PASS')
    if not blockers:
        decision = '1.0 release candidate ready for review'
    elif (all(row['status'] in ('PASS', 'UNVERIFIED') for row in gates.values())
          and all(row['status'] == 'PASS' for row in prompt52.values())
          and baseline.get('scope')):
        decision = 'Experimental release ready'
    else:
        decision = 'Release blocked'
    source = _git_state(root)
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                source_commit=_git_commit(root), source_tree_dirty=source['dirty'],
                source_diff_sha256=source['diff_sha256'], baseline_sha256=baseline_sha,
                scope=baseline.get('scope'), production_ready=False,
                decision=decision, release_gates=gates, prompt52=prompt52,
                blocking_gates=blockers,
                evidence_manifest=(str(manifest_path.relative_to(root)) if manifest_path else None),
                policy=dict(minimum_qualified_soak_seconds=minimum_soak_seconds,
                            qemu_abrupt_stop_counts_as_power_loss=False,
                            missing_physical_hardware_is_not_a_pass=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--soak-report')
    parser.add_argument('--reboot-report')
    parser.add_argument('--validation-manifest')
    parser.add_argument('--minimum-soak-seconds', type=int, default=86_400)
    parser.add_argument('--output', type=Path, default=Path('config/release-qualification.json'))
    args = parser.parse_args(argv)
    if not 3_600 <= args.minimum_soak_seconds <= 604_800:
        parser.error('--minimum-soak-seconds must be between one hour and seven days')
    try:
        root = args.root.resolve(strict=True)
        output = args.output if args.output.is_absolute() else root / args.output
        if output.is_symlink() or root not in output.resolve().parents:
            raise ValueError('output must be a non-symlink path inside the repository')
        report = evaluate(root, soak_report=args.soak_report, reboot_report=args.reboot_report,
                          validation_manifest=args.validation_manifest,
                          minimum_soak_seconds=args.minimum_soak_seconds)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n')
        print(json.dumps(dict(decision=report['decision'], output=str(output),
                              blocking_gates=report['blocking_gates']), sort_keys=True))
        return 0 if report['decision'] == '1.0 release candidate ready for review' else 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
