"""Generation and ownership evidence for a single owned HTB hierarchy."""
import time

from durable_state import atomic_json


def layout(queues, filters):
    # No packet counters, timestamps or transient offload counters.
    roots = [(q.get('kind'), q.get('handle'), q.get('options', {}).get('default'))
             for q in queues if q.get('root')]
    leaves = sorted((q['parent'], q['kind'], q['handle']) for q in queues
                    if q.get('parent', '').startswith('1:'))
    keys = ('kind', 'protocol', 'pref', 'chain', 'handle', 'classid', 'keys', 'actions')
    rules = []
    for row in filters:
        options = row.get('options', {})
        rules.append({key: row[key] if key in row else options[key] for key in keys
                      if key in row or key in options})
    return roots, leaves, rules


class Safety:
    def __init__(self, lab, layout, capacity, allocation):
        self.resource = dict(namespace=list(lab.sender_owner[:2]), ifindex=lab.sender_owner[2],
                             owner=lab.sender_owner[3], interface='fc0', root='1:')
        self.layout = layout
        self.capacity = capacity
        self.allocation = dict(allocation)
        self.desired = self.applied = self.verified = self.stable = 0
        self.proposed = None
        self.verified_ns = time.monotonic_ns()
        self.phase = 'VERIFIED'
        self.failure = None
        self.recovery = None
        self.path = None

    def view(self):
        return dict(schema=1, resource=self.resource, capacity_kbit=self.capacity,
                    desired_generation=self.desired, proposed_generation=self.proposed,
                    applied_generation=self.applied, kernel_verified_generation=self.verified,
                    last_stable_generation=self.stable, last_stable_allocation=self.allocation,
                    phase=self.phase, failure=self.failure, recovery=self.recovery,
                    last_verified_ns=self.verified_ns)

    def save(self):
        if self.path is not None:
            atomic_json(self.path, self.view())

    def begin(self, capacity, before, generation):
        if self.proposed is not None or self.verified is None:
            raise RuntimeError('HTB actuation authority is busy or quarantined')
        if (generation is not None and generation != self.verified
                or capacity != self.capacity or before != self.allocation):
            raise RuntimeError('stale HTB generation or allocation')
        self.desired += 1
        self.proposed = self.desired
        self.phase, self.failure, self.recovery = 'PROPOSED', None, None
        try:
            self.save()
        except OSError as error:
            self.fail(error, 'NO_MUTATION')
            raise

    def commit(self, capacity, allocation):
        if self.proposed is None:
            raise RuntimeError('no staged HTB transaction')
        self.applied = self.proposed
        snapshot = dict(self.__dict__)
        self.capacity, self.allocation = capacity, dict(allocation)
        self.verified = self.stable = self.applied
        self.verified_ns = time.monotonic_ns()
        self.phase, self.proposed = 'VERIFIED', None
        try:
            self.save()
        except OSError:
            self.__dict__.update(snapshot)
            raise

    def fail(self, error, recovery):
        self.failure, self.recovery = str(error)[:400], recovery
        self.phase = 'QUARANTINED' if recovery == 'UNVERIFIED' else 'REJECTED'
        self.applied = self.verified = None if recovery == 'UNVERIFIED' else self.stable
        self.proposed = None
        if recovery == 'VERIFIED_COMPENSATION':
            self.verified_ns = time.monotonic_ns()
        try:
            self.save()
        except OSError as failure:
            self.failure = f'{self.failure[:250]}; recovery journal unavailable: {failure}'[:400]

    def released(self):
        self.phase = 'RELEASED'
        self.applied = self.verified = self.proposed = None
        try:
            self.save()
        except OSError as error:
            self.failure = f'release journal unavailable: {error}'[:400]
