"""Validate the versioned service configuration before any network operation."""
import copy
import json
import re
from pathlib import Path

CLASSES = dict(critical=10, interactive=11, standard=12, background=20)
LAB_FIELDS = dict(bandwidth_kbit=(100, 1000000), delay_ms=(0, 5000),
                  jitter_ms=(0, 5000), loss_pct=(0, 100), reorder_pct=(0, 100))


def keys(obj, expected):
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise ValueError(f'expected fields {", ".join(expected)}')


def number(value, low, high, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high:
        raise ValueError(f'expected {"integer" if integer else "number"} in [{low}, {high}]')


def profile(lab):
    keys(lab, LAB_FIELDS)
    for name, bounds in LAB_FIELDS.items():
        number(lab[name], *bounds, integer=name == 'bandwidth_kbit')
    if lab['jitter_ms'] > lab['delay_ms'] or (lab['reorder_pct'] and not lab['delay_ms']):
        raise ValueError('jitter must fit within delay and reordering requires delay')
    return lab


def unique(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError('duplicate configuration key')
    return obj


def load(path):
    from durable_state import read_json
    from paths import ROOT, INSTALLED
    selected=Path(path)
    # Executing a source checkout already trusts its owner. Keep this exception
    # local to source configuration; installed configuration/state stay strict.
    owners=(ROOT.stat().st_uid,) if not INSTALLED and selected.resolve().is_relative_to(ROOT) else ()
    cfg = read_json(selected,65536,trusted_owners=owners)
    from observability_config import DEFAULT, validate
    from adaptive import DEFAULT as ADAPTIVE_DEFAULT
    if isinstance(cfg, dict):
        cfg.setdefault('observability', copy.deepcopy(DEFAULT))
        supplied = cfg.get('adaptive', {})
        if isinstance(supplied, dict):
            cfg['adaptive'] = dict(ADAPTIVE_DEFAULT, **supplied)
    api = cfg.pop('intent_api', {'priorities':['standard']}) if isinstance(cfg,dict) else {}
    if isinstance(api, dict):
        api.setdefault('udp_message_policies', {})
    keys(api, ('priorities', 'udp_message_policies'))
    if (not isinstance(api['priorities'],list) or not api['priorities']
            or any(item not in ('critical','interactive','standard') for item in api['priorities'])):
        raise ValueError('invalid intent API priority permissions')
    udp_policies = api['udp_message_policies']
    if not isinstance(udp_policies, dict) or len(udp_policies) > 16:
        raise ValueError('UDP message policies must be an object with at most 16 services')
    for service, policy in udp_policies.items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', service):
            raise ValueError('invalid UDP message service name')
        keys(policy, ('priority', 'uids', 'cgroup_prefix', 'socket_mark',
                      'urgent_rate_pps', 'urgent_burst', 'max_deadline_ms'))
        if policy['priority'] != 'critical':
            raise ValueError('UDP urgency requires the critical priority ceiling')
        if policy['priority'] not in api['priorities']:
            raise ValueError('UDP message priority is not enabled for the intent API')
        uids = policy['uids']
        if (not isinstance(uids, list) or not uids or len(uids) > 16 or
                any(type(uid) is not int or not 0 <= uid < 2**32 for uid in uids) or len(set(uids)) != len(uids)):
            raise ValueError('invalid UDP message service uid allowlist')
        prefix = policy['cgroup_prefix']
        if (not isinstance(prefix, str) or not prefix.startswith('/') or len(prefix) > 256 or
                any(part in ('', '.', '..') for part in prefix[1:].split('/')) or '\x00' in prefix):
            raise ValueError('invalid UDP message cgroup prefix')
        number(policy['socket_mark'], 1, 2**32-1, integer=True)
        number(policy['urgent_rate_pps'], 1, 100000, integer=True)
        number(policy['urgent_burst'], 1, 10000, integer=True)
        number(policy['max_deadline_ms'], 1, 10000, integer=True)
    keys(cfg, ('version', 'interface', 'lab', 'telemetry_seconds', 'log_level', 'policies', 'observability', 'adaptive'))
    validate(cfg['observability'])
    settings = cfg['adaptive']
    keys(settings, ADAPTIVE_DEFAULT)
    for key, bounds in dict(window_seconds=(1, max(cfg['observability']['windows'])),
                            minimum_samples=(1, 100000), enter_windows=(2, 10),
                            cooldown_seconds=(1, 300), background_floor_pct=(20, 100),
                            step_pct=(1, 10), maximum_boost_pct=(1, 30),
                            maximum_actions=(1, 32), recovery_windows=(2, 10),
                            maximum_reversals=(2, 8)).items():
        number(settings[key], *bounds, integer=True)
    number(cfg['version'], 1, 1, integer=True)
    if cfg['interface'] != 'fc0':
        raise ValueError('only the owned lab interface fc0 is supported')
    profile(cfg['lab'])
    number(cfg['telemetry_seconds'], .25, 60)
    if cfg['log_level'] not in ('info', 'error'):
        raise ValueError('log_level must be info or error')
    policies = cfg['policies']
    if not isinstance(policies, dict) or not 1 <= len(policies) <= 16 or 'default' not in policies:
        raise ValueError('1–16 named policies including default required')
    for name, policy in policies.items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) or not isinstance(policy, dict):
            raise ValueError('invalid policy name or definition')
        if policy.get('scheduler') == 'cake':
            keys(policy, ('scheduler',))
            continue
        keys(policy, ('scheduler', 'classes'))
        if policy['scheduler'] != 'htb':
            raise ValueError('scheduler must be htb or cake')
        classes = policy['classes']
        keys(classes, CLASSES)
        ports = set()
        for cls in classes.values():
            keys(cls, ('port', 'min_pct', 'max_pct', 'priority'))
            number(cls['port'], 1, 65535, True)
            number(cls['min_pct'], 1, 100, True)
            number(cls['max_pct'], cls['min_pct'], 100, True)
            number(cls['priority'], 0, 7, True)
            if cls['port'] in ports:
                raise ValueError('class ports must be unique')
            ports.add(cls['port'])
        if sum(cls['min_pct'] for cls in classes.values()) > 100:
            raise ValueError('class reservations exceed link capacity')
    cfg['intent_api'] = api
    return cfg
