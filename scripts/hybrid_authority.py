"""Versioned HTB authority for the parent of a disposable Hybrid V3 daemon."""
import os
import time

import adaptive_tc
from service_config import keys, number


class Authority:
    def __init__(self, service):
        self.service = service
        self.owner = os.getppid()
        self.revision = 0
        self.boosted = False
        self.probes = 0

    def authorize(self, pid):
        if not self.service.research or os.geteuid() != 0 or pid != self.owner or os.getppid() != self.owner:
            raise PermissionError('Hybrid V3 is reserved for the owning Research Lab runner')

    def invalidate(self):
        self.revision += 1
        self.boosted = False
        self.probes = 0

    def baseline(self):
        controller = self.service.controller
        cap = controller.capacity()
        return {key: (cap*row['min_pct']//100, cap*row['max_pct']//100)
                for key, row in controller.policy()['classes'].items()}

    def allocation(self, boosted=None):
        target = self.baseline()
        active = self.boosted if boosted is None else boosted
        if active:
            step = self.service.controller.capacity()*5//100
            rate, ceil = target['critical']
            target['critical'] = rate+step, ceil
            rate, ceil = target['background']
            target['background'] = rate-step, ceil
        return target

    def status(self):
        return dict(revision=self.revision, boosted=self.boosted, allocation=self.allocation())

    def change(self, boost):
        service = self.service
        controller = service.controller
        capacity = controller.capacity()
        floor = capacity*controller.cfg['background_floor_pct']//100
        if not service.lab or controller.mode != 'off' or controller.policy()['scheduler'] != 'htb':
            raise RuntimeError('Hybrid V3 lacks exclusive HTB authority')
        if boost == self.boosted:
            raise ValueError('requested HTB state is already active')
        try:
            timing = adaptive_tc.change(service.lab, capacity, controller.policy(),
                                        self.allocation(), self.allocation(boost), background_floor=floor)
        except adaptive_tc.TransactionFailure as error:
            if not error.compensated:
                service.close_lab()
            raise
        self.boosted = boost
        self.probes += boost
        self.revision += 1
        service.poll_metrics()
        service.log('hybrid_class_verified', revision=self.revision, boosted=boost, **timing)
        return dict(**self.status(), timing=timing)

    def command(self, value, pid):
        self.authorize(pid)
        keys(value, ('op', 'action', 'revision', 'capacity_kbit', 'sample_ns'))
        service = self.service
        number(value['revision'], 0, 2**53-1, True)
        number(value['capacity_kbit'], 100, 12000, True)
        number(value['sample_ns'], 0, 2**63-1, True)
        if (service.research_mode != 'hybrid-v3' or value['revision'] != self.revision
                or value['capacity_kbit'] != service.controller.capacity()):
            raise ValueError('stale Hybrid V3 class transaction')
        if value['action'] == 'restore':
            return self.change(False)
        if value['action'] != 'shift' or self.probes >= 3:
            raise ValueError('unsupported or exhausted Hybrid V3 action')
        observer = service.observer
        stamp = observer.sampled_ns
        if (not stamp or stamp != value['sample_ns'] or not 0 <= time.monotonic_ns()-stamp <= 2_500_000_000
                or observer.errors or observer.truncated or service.error or not service.research_link_up):
            raise ValueError('stale Hybrid V3 observer sample')
        return self.change(True)
