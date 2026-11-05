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
    with Path(path).open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('configuration exceeds 64 KiB')
    cfg = json.loads(data, object_pairs_hook=unique)
    from observability_config import DEFAULT, validate
    from adaptive import DEFAULT as ADAPTIVE_DEFAULT
    if isinstance(cfg, dict):
        cfg.setdefault('observability', copy.deepcopy(DEFAULT))
        supplied = cfg.get('adaptive', {})
        if isinstance(supplied, dict):
            cfg['adaptive'] = dict(ADAPTIVE_DEFAULT, **supplied)
    api = cfg.pop('intent_api', {'priorities':['standard']}) if isinstance(cfg,dict) else {}
    keys(api, ('priorities',))
    if (not isinstance(api['priorities'],list) or not api['priorities']
            or any(item not in ('critical','interactive','standard') for item in api['priorities'])):
        raise ValueError('invalid intent API priority permissions')
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
