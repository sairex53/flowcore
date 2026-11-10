"""Readback-verified in-place HTB updates on an owned ServiceLab."""
import json
import time
from service_config import CLASSES


class TransactionFailure(RuntimeError):
    """The attempted update failed, with or without verified compensation."""
    def __init__(self, message, *, compensated):
        super().__init__(message)
        self.compensated = compensated


def read(lab, capacity, policy):
    roots = json.loads(lab.tc('-j', 'qdisc', 'show', 'dev', 'fc0'))
    if scope := getattr(lab, "htb_safety", None):
        lab.assert_htb_layout(scope.layout, roots)
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


def validate(capacity, allocation):
    if (type(capacity) is not int or not 100 <= capacity <= 1000000 or set(allocation) != set(CLASSES)
            or any(type(rate) is not int or type(ceil) is not int or not 0 < rate <= ceil <= capacity
                   for rate,ceil in allocation.values())
            or sum(rate for rate,_ in allocation.values()) > capacity):
        raise ValueError('unsafe HTB allocation')


def diff(before, after):
    ordered = sorted(CLASSES,key=lambda cls: after[cls][0]-before[cls][0])
    return [dict(traffic_class=cls,handle=f'1:{CLASSES[cls]}',before=before[cls],after=after[cls])
            for cls in ordered if before[cls] != after[cls]]


def change(lab, capacity, policy, expected, desired, *, background_floor=1, generation=None):
    validate(capacity,expected)
    validate(capacity,desired)
    for name,(rate,ceil) in desired.items():
        cls = policy['classes'][name]
        floor = background_floor if name=='background' else capacity*cls['min_pct']//100
        if rate < floor or ceil > capacity*cls['max_pct']//100:
            raise ValueError('HTB target exceeds authorized class bounds')
    changes = diff(expected,desired)
    scope = getattr(lab, 'htb_safety', None)
    if scope:
        scope.begin(capacity, expected, generation)
    start = time.monotonic_ns()
    try:
        lab.assert_owned_sender()
        if read(lab, capacity, policy) != expected:
            raise RuntimeError('class allocation drift, refusing to overwrite')
    except Exception as error:
        if scope:
            scope.fail(error, 'UNVERIFIED')
        raise
    pre_read_ns = time.monotonic_ns()-start
    current = dict(expected)
    attempted = None

    def write(name, value):
        nonlocal attempted
        lab.assert_owned_sender()
        rate, ceil = value
        attempted = name, value
        lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:1', 'classid', f'1:{CLASSES[name]}',
               'htb', 'rate', f'{rate}kbit', 'ceil', f'{ceil}kbit',
               'prio', policy['classes'][name]['priority'], 'quantum', '1514')
        current[name] = value
        attempted = None

    def apply(target):
        for item in diff(current,target):
            write(item['traffic_class'],item['after'])

    try:
        apply_start = time.monotonic_ns()
        apply(desired)
        verify_start = time.monotonic_ns()
        lab.assert_owned_sender()
        if read(lab, capacity, policy) != desired:
            raise RuntimeError('HTB readback differs from desired allocation')
        verified_ns = time.monotonic_ns()
        if scope:
            scope.commit(capacity, desired)
        return dict(pre_read_ns=pre_read_ns, apply_ns=verify_start-apply_start,
                    verify_ns=verified_ns-verify_start, verified_ns=verified_ns,
                    generation=scope.verified if scope else None, transaction='VERIFIED', diff=changes)
    except Exception as failure:
        try:
            lab.assert_owned_sender()
            observed = read(lab, capacity, policy)
            # Timeout/IPC loss can hide an accepted last mutation. Only the
            # exact acknowledged prefix or that prefix plus the last request
            # can be compensated. Anything else is foreign or ambiguous.
            after_attempt = dict(current)
            if attempted:
                name, value = attempted
                after_attempt[name] = value
            if observed != current and observed != after_attempt:
                raise RuntimeError('unknown external state, refusing overwrite')
            current = observed
            apply(expected)
            lab.assert_owned_sender()
            if read(lab, capacity, policy) != expected:
                raise RuntimeError('rollback readback mismatch')
        except Exception as rollback:
            error = TransactionFailure(f'HTB transaction failed: {failure}; rollback failed: {rollback}',
                                       compensated=False)
            if scope:
                scope.fail(error, 'UNVERIFIED')
            raise error from failure
        if scope:
            scope.fail(failure, 'VERIFIED_COMPENSATION')
        raise TransactionFailure(f'HTB transaction failed and rolled back: {failure}', compensated=True) from failure
