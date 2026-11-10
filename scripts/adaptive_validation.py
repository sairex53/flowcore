"""Explain admission and judge a single owned HTB probe from observed evidence."""
from math import isfinite


def hold_reason(plan, minimum):
    optimizer = plan.get('optimizer', {})
    if optimizer.get('stage') == 'SOLVER_UNAVAILABLE':
        return 'ACTION_BLOCKED', 'optimizer_unavailable'
    if plan.get('feasible') is False:
        return 'POLICY_CONFLICT', 'infeasible_policy_constraints'
    if plan.get('bottleneck_gate'):
        return 'NO_CONTROLLABLE_BOTTLENECK', 'directional_gate_denied'
    if plan.get('reason') == 'transport observation truncated':
        return 'LOW_CONFIDENCE', 'truncated_transport_observation'
    objectives = plan.get('objectives', {})
    if not objectives:
        return 'INSUFFICIENT_SAMPLES', 'no_registered_service_observations'
    if not plan.get('pressure'):
        if plan.get('queue_signal_present'):
            return 'NO_CONGESTION', 'root_pressure_below_threshold'
        return 'LOW_CONFIDENCE', 'queue_signal_unavailable'
    if not any(row['samples'] >= minimum for row in objectives.values()):
        return 'INSUFFICIENT_SAMPLES', 'too_few_successful_requests'
    if not any(row['samples'] >= minimum and not row['failures'] and row['p99_us'] is not None
               for row in objectives.values()):
        return 'LOW_CONFIDENCE', 'failed_or_missing_application_observations'
    if not any(row.get('eligible') for row in plan.get('attribution', {}).values()):
        return 'NO_CONTROLLABLE_BOTTLENECK', 'no_backlogged_class_at_its_reservation'
    if plan.get('slo_gate') and not any(row['outside_deadband'] for row in objectives.values()):
        return 'CHANGE_NOT_WORTH_COST', 'service_objectives_within_deadband'
    if 'no_improving_transfer_above_deadband' in optimizer.get('limiting_constraints', ()):
        return 'CHANGE_NOT_WORTH_COST', 'solver_kept_current_allocation'
    if 'incomplete_or_stale_telemetry' in optimizer.get('limiting_constraints', ()):
        return 'LOW_CONFIDENCE', 'optimizer_input_incomplete'
    return 'NO_FEASIBLE_ACTION', 'no_eligible_transfer_within_limits'


def effect(plan, traffic_class):
    before, after = plan['previous'], plan['proposed']
    return dict(actuator='htb', bottleneck='owned_local_egress', target_class=traffic_class,
                reservation_delta_kbit={name: after[name][0]-before[name][0] for name in before},
                ceiling_delta_kbit={name: after[name][1]-before[name][1] for name in before},
                expected_latency='may_decrease_if_reservation_limits_the_target_class' if traffic_class else None,
                expected_background='may_decrease_under_contention' if after['background'][0] < before['background'][0] else None,
                expected_fairness=None, estimated_latency_gain_us=None, estimated_goodput_cost_kbit=None,
                expected_recovery_seconds=None, kernel_class_writes=sum(before[name] != after[name] for name in before))


def frame(window, traffic_class):
    queue = window['queue']
    target = queue.get(traffic_class, {})
    background = queue.get('background', {})
    rates = {name: queue.get(name, {}).get('bytes_per_second')
             for name in ('critical', 'interactive', 'standard', 'background')}
    observed = {name: rate for name, rate in rates.items() if rate is not None and rate > 0}
    material = sum(observed.values()) * .01
    active = {name: rate for name, rate in observed.items() if rate >= material}
    fairness = sum(active.values())**2/(len(active)*sum(x*x for x in active.values())) if active else None
    return dict(target_kbit=target['bytes_per_second']/125 if target.get('bytes_per_second') is not None else None,
                target_backlog_bytes=(target.get('backlog') or {}).get('p95_bytes'),
                background_kbit=background['bytes_per_second']/125 if background.get('bytes_per_second') is not None else None,
                background_demanded=background.get('backlogged_samples', 0) > 0,
                active_classes=sorted(active), jain_active_classes=fairness)


def _change(before, after, fraction, direction):
    return (before is not None and after is not None and isfinite(before) and isfinite(after)
            and before > 0 and (after-before)*direction >= before*fraction)


def assess(before, after, *, background_loss, fairness_loss):
    """Queue counters describe class traffic, with no per-request attribution."""
    served = _change(before['target_kbit'], after['target_kbit'], .05, 1)
    backlog = _change(before['target_backlog_bytes'], after['target_backlog_bytes'], .10, -1)
    background = before['background_demanded'] and after['background_demanded']
    matching = before['active_classes'] == after['active_classes']
    goodput_regressed = background and _change(before['background_kbit'], after['background_kbit'], background_loss, -1)
    jain = before['jain_active_classes'], after['jain_active_classes']
    fairness_regressed = matching and all(item is not None for item in jain) and jain[0]-jain[1] > fairness_loss
    return dict(target_served_increased=served, target_backlog_reduced=backlog,
                traffic_effect_observed=served or backlog, background_throughput_regressed=goodput_regressed,
                fairness_regressed=fairness_regressed, background_demand_comparable=background,
                active_classes_comparable=matching)


def summarize(events):
    """Classify verified probes whose verdict is outside the observed trace."""
    from collections import Counter
    reasons = Counter()
    probes = {}
    verdicts = {}
    restored = set()
    for event in events:
        trace = event.get('trace')
        if not isinstance(trace, dict) or trace.get('schema') != 1:
            continue
        reasons[trace['reason_code']] += 1
        key = (event['controller_session'], event.get('proposal_id'))
        if key[1] is None:
            continue
        if event['result'] == 'pending' and trace['reason_code'] == 'ACTION_APPLIED':
            probes[key] = event
        if event.get('outcome') in ('useful', 'harmful', 'inconclusive'):
            verdicts[key] = event['outcome']
        if trace['reason_code'] == 'ACTION_REVERTED':
            restored.add(key)
    rows = [dict(proposal_id=key[1], session=key[0], outcome=verdicts.get(key, 'inconclusive'),
                 reason='observed_verdict' if key in verdicts else
                        'interrupted_and_restored' if key in restored else 'right_censored',
                 rollback_verified=key in restored)
            for key in probes]
    return dict(schema=1, reasons=dict(reasons), verified_probes=len(rows), probes=rows,
                outcomes=dict(Counter(row['outcome'] for row in rows)))
