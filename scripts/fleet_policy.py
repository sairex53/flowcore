"""Admission for declarative Fabric caps and immutable Git policy bundles."""
import base64
import binascii
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from fabric_protocol import CLASSES, MAX_AGENTS, Rejected, boolean, bounded_list, fields, ident, integer
from service_config import unique

DOMAIN = b'FLOWCORE-FLEET-V1\0'
MAX_REVISION = 2**53-1
BUNDLE_LIMIT = 32768
MODES = ('dry-run', 'shadow', 'canary', 'percentage', 'full')
GIT_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1',
           'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}


def identifiers(value, limit=MAX_AGENTS):
    values = [ident(item) for item in bounded_list(value, limit)]
    if not values or len(values) != len(set(values)):
        raise ValueError('expected nonempty unique names')
    return values


def decode(raw):
    def reject_number(_):
        raise ValueError('non-finite policy value')
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_number)
    except RecursionError as error:
        raise ValueError('policy JSON nesting exceeds limit') from error


def binary(value, size):
    if not isinstance(value, str) or len(value) != ((size+2)//3)*4:
        raise ValueError('invalid base64 value')
    try:
        result = base64.b64decode(value, validate=True)
    except binascii.Error as error:
        raise ValueError('invalid base64 value') from error
    if len(result) != size:
        raise ValueError('invalid key or signature length')
    return result


def configuration(cfg, cluster):
    fields(cfg, ('repositories', 'operators', 'owners'))
    repositories = cfg['repositories']
    owners = cfg['owners']
    operators = cfg['operators']
    for value in (repositories, owners, operators):
        if not isinstance(value, dict) or not 1 <= len(value) <= 16:
            raise ValueError('invalid Fleet authority map')
    for name, repository in repositories.items():
        ident(name)
        fields(repository, ('path', 'bundle', 'require_signature', 'keys'))
        path = repository['path']
        if not isinstance(path, str) or not Path(path).is_absolute() or len(path) > 1024:
            raise ValueError('trusted Git path must be absolute')
        bundle = repository['bundle']
        if (not isinstance(bundle, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]{0,127}\.json', bundle)
                or '..' in PurePosixPath(bundle).parts):
            raise ValueError('invalid Git blob path')
        boolean(repository['require_signature'])
        keys = repository['keys']
        if not isinstance(keys, dict) or len(keys) > 8 or (repository['require_signature'] and not keys):
            raise ValueError('invalid signing keys')
        for key, public in keys.items():
            ident(key)
            binary(public, 32)
    claimed_hosts = set()
    for name, owner in owners.items():
        ident(name)
        fields(owner, ('repositories', 'hosts', 'services', 'classes'))
        if set(identifiers(owner['repositories'])) - repositories.keys():
            raise ValueError('unknown repository')
        hosts = set(identifiers(owner['hosts']))
        if hosts - cluster['agents'].keys() or hosts & claimed_hosts:
            raise ValueError('unknown or multiply owned host')
        claimed_hosts |= hosts
        identifiers(owner['services'], 32)
        if set(identifiers(owner['classes'])) - set(CLASSES):
            raise ValueError('unknown owned class')
    names = set()
    for pin, operator in operators.items():
        fields(operator, ('name', 'owners'))
        name = ident(operator['name'])
        if pin not in cluster['admins'] or name in names or set(identifiers(operator['owners'])) - owners.keys():
            raise ValueError('invalid Fleet operator')
        names.add(name)
    return cfg


def git(repo, *args):
    try:
        return subprocess.run(['/usr/bin/git', '-C', repo, '--no-pager', '-c', 'core.hooksPath=/dev/null',
                               *args], check=False, capture_output=True, timeout=2, env=GIT_ENV)
    except (OSError, subprocess.SubprocessError) as error:
        raise Rejected('git_unavailable') from error


def read_object(repo, obj, expected, limit, optional=False):
    kind = git(repo, 'cat-file', '-t', obj)
    if kind.returncode:
        if optional:
            return None
        raise Rejected('git_object_unavailable')
    size = git(repo, 'cat-file', '-s', obj)
    if kind.stdout.strip() != expected.encode() or size.returncode or not size.stdout.strip().isdigit():
        raise Rejected('git_object_type')
    length = int(size.stdout)
    if not 1 <= length <= limit:
        raise Rejected('git_object_size')
    data = git(repo, 'cat-file', expected, obj)
    if data.returncode or len(data.stdout) != length:
        raise Rejected('git_object_changed')
    return data.stdout


def load(repository, commit):
    if not isinstance(commit, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', commit):
        raise Rejected('full_git_commit_required')
    repo = repository['path']
    metadata = read_object(repo, commit, 'commit', 65536)
    obj = f"{commit}:{repository['bundle']}"
    payload = read_object(repo, obj, 'blob', BUNDLE_LIMIT)
    envelope = read_object(repo, obj+'.sig', 'blob', 2048, optional=True)
    signer = None
    key_fingerprint = None
    if envelope is None:
        if repository['require_signature']:
            raise Rejected('signature_required')
    else:
        signed = fields(decode(envelope), ('key_id', 'signature'))
        signer = ident(signed['key_id'])
        key = repository['keys'].get(signer)
        if key is None:
            raise Rejected('untrusted_signer')
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as error:
            raise Rejected('signature_backend_unavailable') from error
        public = binary(key, 32)
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(binary(signed['signature'], 64), DOMAIN+payload)
        except InvalidSignature as error:
            raise Rejected('invalid_signature') from error
        key_fingerprint = hashlib.sha256(public).hexdigest()
    raw_author = next((line[7:] for line in metadata.split(b'\n\n', 1)[0].splitlines()
                       if line.startswith(b'author ')), None)
    author = None if raw_author is None else ''.join(
        char if char.isprintable() else '?' for char in raw_author[:256].decode('utf-8', 'replace'))
    return decode(payload), dict(commit=commit, author=author, signer=signer, key_fingerprint=key_fingerprint,
                                  bundle_sha256=hashlib.sha256(payload).hexdigest())


def schema(bundle):
    fields(bundle, ('version', 'policy', 'revision', 'owner', 'cluster', 'host_groups',
                    'service_groups', 'target', 'caps_kbit', 'controller', 'slo', 'stages'))
    integer(bundle['version'], 1, 1)
    integer(bundle['revision'], 1, MAX_REVISION)
    for key in ('policy', 'owner', 'cluster'):
        ident(bundle[key])
    for key, limit in (('host_groups', MAX_AGENTS), ('service_groups', 32)):
        group = bundle[key]
        if not isinstance(group, dict) or not 1 <= len(group) <= 8:
            raise ValueError('invalid group map')
        for name, members in group.items():
            ident(name)
            identifiers(members, limit)
    target = fields(bundle['target'], ('hosts', 'services', 'classes'))
    if (target['hosts'] not in bundle['host_groups'] or target['services'] not in bundle['service_groups']
            or set(identifiers(target['classes'])) - set(CLASSES)):
        raise ValueError('invalid rollout scope')
    caps = fields(bundle['caps_kbit'], bundle['host_groups'][target['hosts']])
    for value in caps.values():
        integer(value, 100, 1_000_000)
    controller = fields(bundle['controller'], ('mode', 'on_loss'))
    if controller != dict(mode='fixed-cap', on_loss='protected-floor'):
        raise ValueError('unsupported Fabric controller configuration')
    slo = fields(bundle['slo'], ('max_p99_us', 'max_p99_increase_us', 'max_error_ppm',
                 'max_deadline_miss_ppm', 'min_samples', 'min_egress_kbit', 'min_jain_ppm',
                 'max_controller_actions'))
    integer(slo['max_p99_us'], 1, 600_000_000)
    if slo['max_p99_increase_us'] is not None:
        integer(slo['max_p99_increase_us'], 0, 600_000_000)
    for key in ('max_error_ppm', 'max_deadline_miss_ppm', 'min_jain_ppm'):
        integer(slo[key], 0, 1_000_000)
    integer(slo['min_samples'], 1, 100000)
    integer(slo['min_egress_kbit'], 0, 1_000_000)
    if slo['max_controller_actions'] is not None:
        integer(slo['max_controller_actions'], 0, 10000)
    stages = fields(bundle['stages'], ('canary', 'percentages', 'verify_seconds', 'observe_seconds',
                                       'confirm_windows'))
    canary = identifiers(stages['canary'])
    if set(canary) - caps.keys() or (len(caps) > 1 and len(canary) == len(caps)):
        raise ValueError('canary must be a proper subset of a multi-host target')
    percentages = [integer(value, 1, 99) for value in bounded_list(stages['percentages'], 6)]
    if percentages != sorted(set(percentages)):
        raise ValueError('percentages must increase')
    integer(stages['verify_seconds'], 5, 120)
    integer(stages['observe_seconds'], 5, 300)
    integer(stages['confirm_windows'], 2, 5)
    return bundle


def layout(bundle):
    stages = bundle['stages']
    all_hosts = set(bundle['caps_kbit'])
    order = list(stages['canary'])
    order += sorted(all_hosts-set(order), key=lambda host: hashlib.sha256(
        f"{bundle['policy']}:{bundle['revision']}:{host}".encode()).digest())
    return [dict(name='canary', hosts=stages['canary']),
            *(dict(name=str(pct), hosts=order[:max(len(stages['canary']), (pct*len(order)+99)//100)])
              for pct in stages['percentages']), dict(name='full', hosts=order)]


def selection(bundle, coordinator, authority):
    if bundle['cluster'] != coordinator.cfg['cluster']:
        raise Rejected('cluster_scope')
    hosts = set(bundle['caps_kbit'])
    wanted = set(bundle['service_groups'][bundle['target']['services']])
    classes = set(bundle['target']['classes'])
    if (hosts - set(authority['hosts']) or wanted - set(authority['services']) or
            classes - set(authority['classes'])):
        raise Rejected('owner_scope')
    selected, impact = {}, {}
    for host in sorted(hosts):
        if host not in coordinator.egress:
            raise Rejected('host_not_actuable')
        services = coordinator.cfg['agents'][host]['services']
        selected[host] = sorted(name for name, cls in services.items() if name in wanted and cls in classes)
        if not selected[host]:
            raise Rejected('service_not_enrolled')
        # An aggregate cap also impacts every other enrolled service on this host.
        collateral = {name: cls for name, cls in services.items() if name not in selected[host]}
        if set(collateral) - set(authority['services']) or set(collateral.values()) - set(authority['classes']):
            raise Rejected('collateral_outside_owner_scope')
        impact[host] = dict(selected=selected[host], other_enrolled_services=sorted(collateral),
                            scope='all egress on the enrolled interface')
    return selected, impact


def approval(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
