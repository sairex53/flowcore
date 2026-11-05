"""Readback-verified in-place HTB updates on an owned ServiceLab."""
import json
from service_config import CLASSES


def read(lab, capacity, policy):
    roots = json.loads(lab.tc('-j', 'qdisc', 'show', 'dev', 'fc0'))
    root = next((q for q in roots if q.get('root')), {})
    if root.get('kind') != 'htb' or root.get('handle') != '1:':
        raise RuntimeError('owned HTB root missing')
    rows = {row['handle']: row for row in json.loads(lab.tc('-j', 'class', 'show', 'dev', 'fc0'))
            if row.get('class') == 'htb'}
    if set(rows) != {'1:1', *(f'1:{minor}' for minor in CLASSES.values())}:
        raise RuntimeError('unexpected HTB class topology')
    parent = rows['1:1']
    if parent.get('rate') != capacity * 125 or parent.get('ceil') != capacity * 125:
        raise RuntimeError('shaper capacity drift')
    values = {}
    for name, minor in CLASSES.items():
        row = rows[f'1:{minor}']
        if row.get('class') != 'htb' or row.get('parent') != '1:1' or row.get('prio') != policy['classes'][name]['priority']:
            raise RuntimeError('unexpected HTB class ownership or priority')
        if any(type(row.get(key)) is not int or row[key] % 125 for key in ('rate', 'ceil')):
            raise RuntimeError('HTB rate units not supported')
        values[name] = (row['rate']//125, row['ceil']//125)
    return values


def change(lab, capacity, policy, expected, desired):
    if sum(rate for rate, _ in desired.values()) > capacity or any(
            not 0 < rate <= ceil <= capacity for rate, ceil in desired.values()):
        raise ValueError('unsafe HTB allocation')
    if read(lab, capacity, policy) != expected:
        raise RuntimeError('class allocation drift, refusing to overwrite')
    current = dict(expected)

    def write(name, value):
        rate, ceil = value
        lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:1', 'classid', f'1:{CLASSES[name]}',
               'htb', 'rate', f'{rate}kbit', 'ceil', f'{ceil}kbit',
               'prio', policy['classes'][name]['priority'], 'quantum', '1514')
        current[name] = value

    def apply(target):
        for name in sorted(current, key=lambda key: target[key][0] - current[key][0]):
            if current[name] != target[name]:
                write(name, target[name])

    try:
        apply(desired)
        if read(lab, capacity, policy) != desired:
            raise RuntimeError('HTB readback differs from desired allocation')
    except Exception as failure:
        try:
            observed = read(lab, capacity, policy)
            if observed != current:
                raise RuntimeError('unknown external state, refusing overwrite')
            apply(expected)
            if read(lab, capacity, policy) != expected:
                raise RuntimeError('rollback readback mismatch')
        except Exception as rollback:
            raise RuntimeError(f'HTB transaction failed: {failure}; rollback failed: {rollback}') from failure
        raise RuntimeError(f'HTB transaction failed and rolled back: {failure}') from failure
