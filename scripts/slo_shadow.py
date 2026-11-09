"""Read-only bridge to authorized intents, allocation and deadline evidence."""
import time

import deadline_runtime
from service_config import keys, number
from slo_optimizer import resource_conflicts


def snapshot(service, request):
    keys(request, ('op', 'deadline'))
    work = request['deadline']
    now = time.monotonic_ns()
    admission = None
    if work is not None:
        keys(work, ('budget_ms', 'payload_bytes', 'processing_us'))
        number(work['budget_ms'], .001, 5000)
        number(work['payload_bytes'], 0, 64*1024**2, True)
        if work['processing_us'] is not None:
            number(work['processing_us'], 0, 600_000_000, True)
        evidence = deadline_runtime.evidence(service, now).get('critical', {})
        admission = deadline_runtime.estimate(
            dict(deadline_ns=now+round(work['budget_ms']*1e6), egress_bytes=work['payload_bytes'],
                 processing_us=work['processing_us'], reliability=0, expiration=0), evidence, now)
    preview = service.controller.optimization_preview('adaptive')
    if service.research_mode == 'hybrid-v3' and service.lab:
        authority = service.hybrid_authority.status()
        preview['hybrid_authority_revision'] = authority['revision']
        if preview.get('actual') != authority['allocation']:
            preview.update(feasible=False, actual_verified=False, hybrid_authority_conflict=True)
    return dict(allocation_preview=preview, deadline=admission)


def explain(snapshot, profile, frame=None):
    preview = snapshot['allocation_preview']
    model = preview['optimizer']
    capacity = model.get('capacity_kbit')
    feasible = bool(preview.get('feasible') and preview.get('actual_verified'))
    candidates = {}
    if feasible:
        actual = preview['actual']
        candidates['hold'] = actual
        for name, row in model.get('alternatives', {}).items():
            candidates[name] = {cls: (rate, actual[cls][1]) for cls, rate in row['rates_kbit'].items()}
        if preview.get('proposed') is not None:
            candidates['planned'] = preview['proposed']
    current_conflicts = (resource_conflicts(capacity, profile['resources'], preview['actual'])
                         if preview.get('actual_verified') else [])
    result = dict(version=1, evidence='allocation_surrogate_only', disposition='SHADOW_HOLD',
                  resource_conflicts=current_conflicts,
                  candidates={name: dict(allocation_kbit=rows,
                                         conflicts=resource_conflicts(capacity, profile['resources'], rows))
                              for name, rows in candidates.items()},
                  objectives=profile['objectives'], **snapshot)
    if frame is not None:
        from hybrid_runtime import plan
        result['hybrid_frame_advisory'] = plan(frame)
    return result
