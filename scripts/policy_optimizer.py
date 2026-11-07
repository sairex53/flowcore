"""Operator constraints, demand proxies and the bounded native solver."""
import ctypes
import time

import adaptive_tc
from intelligence import estimate
from paths import BINDIR
from service_config import CLASSES

ALGORITHMS = ('static', 'max-min', 'proportional', 'adaptive')
PRIORITY = dict(critical=4, interactive=2, standard=1, background=1)
CHURN = 40
RATE_FIELDS = ('floor', 'limit', 'demand', 'current', 'baseline', 'priority', 'utility', 'eligible')


class NativeClass(ctypes.Structure):
    _fields_ = [(name,ctypes.c_int64) for name in RATE_FIELDS]


class Solver:
    def __init__(self, library_path=None):
        self.library_path = library_path or BINDIR/'libflowcore_intelligence.so'
        self.function = None

    def solve(self, algorithm, capacity, step, deadband, rows):
        if self.function is None:
            lib = ctypes.CDLL(str(self.library_path))
            self.function = lib.flowcore_optimize_v1
            integer = ctypes.c_int64
            self.function.argtypes = [ctypes.c_int,integer,integer,integer,integer,ctypes.POINTER(NativeClass),
                                      ctypes.POINTER(integer),ctypes.POINTER(integer)]
            self.function.restype = ctypes.c_int
        native = (NativeClass*4)(*(NativeClass(**rows[name]) for name in CLASSES))
        output, score = (ctypes.c_int64*4)(), ctypes.c_int64()
        code = self.function(ALGORITHMS.index(algorithm),capacity,step,deadband,CHURN,native,output,ctypes.byref(score))
        if code:
            raise ValueError({-1:'invalid numeric solver input',-2:'infeasible hard floors',
                              -3:'infeasible static fallback',-4:'current allocation outside bounds'}.get(
                                  code,'native solution failed validation'))
        return dict(zip(CLASSES,output)), score.value


def contracts(controller, now):
    """Build constraints from operator settings and the already authorized registry."""
    capacity, cfg = controller.capacity(), controller.cfg
    classes = controller.policy()['classes']
    base = {cls:(capacity*row['min_pct']//100, capacity*row['max_pct']//100) for cls,row in classes.items()}
    ceiling = {cls: pair[1] for cls,pair in base.items()}
    claims = {cls:0 for cls in CLASSES}
    conflicts, excluded, intents = [], [], {}
    floor_pct = cfg['background_floor_pct']
    for name,row in sorted(controller.registry.rows.items()):
        if row['monotonic_expiry'] <= now:
            excluded.append(dict(service=name,reason='EXPIRED'))
            continue
        intent = row['intent']
        cls = intent['priority']
        if cls not in base or cls == 'background' or type(row.get('owner_uid')) is not int or row['owner_uid'] < 0:
            conflicts.append(dict(code='UNAUTHORIZED_POLICY',service=name))
            continue
        intents[name] = intent
        claims[cls] += intent['min_kbit']
        floor_pct = max(floor_pct,intent['background_min_pct'])
        if intent['max_kbit']:
            if intent['max_kbit'] > base[cls][1]:
                conflicts.append(dict(code='CEILING_OUTSIDE_OPERATOR_BOUNDS',service=name,traffic_class=cls))
            ceiling[cls] = min(ceiling[cls],intent['max_kbit'])
    floors = {cls:max(rate,claims[cls]) for cls,(rate,_) in base.items()}
    floors['background'] = capacity*floor_pct//100
    maxima = {cls:min(ceiling[cls],base[cls][0]+capacity*cfg['maximum_boost_pct']//100) for cls in CLASSES}
    maxima['background'] = base['background'][0]
    for cls in CLASSES:
        rate = base[cls][0]
        if claims[cls] > rate:
            conflicts.append(dict(code='SERVICE_MINIMA_EXCEED_AUTHORIZED_RESERVATION',traffic_class=cls,
                                  requested_kbit=claims[cls],authorized_kbit=rate))
        if not 0 < floors[cls] <= rate <= maxima[cls] <= ceiling[cls] <= capacity:
            conflicts.append(dict(code='CLASS_BOUNDS_CONFLICT',traffic_class=cls,floor_kbit=floors[cls],
                                  baseline_kbit=rate,maximum_rate_kbit=maxima[cls],ceiling_kbit=ceiling[cls]))
    if sum(floors.values()) > capacity or sum(rate for rate,_ in base.values()) > capacity:
        conflicts.append(dict(code='RESERVATIONS_EXCEED_CAPACITY',required_kbit=max(sum(floors.values()),
                              sum(rate for rate,_ in base.values())),capacity_kbit=capacity))
    fallback = {cls:(rate,ceiling[cls]) for cls,(rate,_) in base.items()} if not conflicts else None
    return dict(intents=intents, conflicts=conflicts, excluded=excluded, floors=floors, maxima=maxima,
                class_ceiling=ceiling, fallback=fallback, claims=claims)


def plan(controller, window, current, objective, algorithm='adaptive', now=None, *, allow_adaptive=True):
    now = time.monotonic() if now is None else now
    cfg, capacity = controller.cfg, controller.capacity()
    model = contracts(controller,now)
    conflicts, fallback = model['conflicts'],model['fallback']
    intents = model['intents']
    queues = window['queue']
    root = queues.get('root',{})
    pressure = (root.get('utilization') or 0) >= .75 and (window['backlog'].get('p95_bytes') or 0) > 0
    bg = queues.get('background',{}).get('bytes_per_second')
    step = max(1,capacity*cfg['step_pct']//100)
    deadband = max(1,capacity//100)
    diagnostics = dict(schema=1, algorithm=algorithm, stage='PLANNED', ownership='owned_htb_lab',
                       capacity_kbit=capacity, rate_unit='decimal_kbit_per_second',
                       max_transfer_kbit=step, minimum_transfer_kbit=deadband, churn_weight=CHURN,
                       conflicts=conflicts, excluded_requests=model['excluded'], static_fallback=fallback,
                       admitted_service_count=len(intents), service_minimums_kbit=model['claims'],
                       desired_total_kbit=sum(intent['desired_kbit'] for intent in intents.values()),
                       floors_kbit=model['floors'], limits_kbit=model['maxima'],
                       ceilings_kbit=model['class_ceiling'], alternatives={}, diff=[])
    detail = dict(previous=current, proposed=None, action='hold', feasible=not conflicts, pressure=pressure,
                  background_floor_kbit=model['floors']['background'],
                  background_throughput_kbit=bg/125 if bg is not None else None, objectives={}, attribution={},
                  optimizer=diagnostics)
    if conflicts:
        return detail
    owners = {}
    risk = {}
    targets = {cls:0 for cls in CLASSES}
    for name,intent in intents.items():
        cls = intent['priority']
        targets[cls] += intent['desired_kbit']
        measured = detail['objectives'][name] = objective(window,name,intent,cfg['minimum_samples'])
        observation = estimate(window,name,intent,measured,current[cls][0],cfg['minimum_samples'])
        detail['attribution'][name] = observation
        if not allow_adaptive or bg is None or not pressure or not observation['eligible']:
            continue
        latency_risk = min(4,max(0,measured['p99_us']/measured['target_p99_us']-1))
        miss_risk = measured['deadline_miss']/max(1,measured['reports'])
        weight = min(500,int(100*latency_risk + 100*miss_risk))
        if cls not in owners or weight > risk[cls]:
            owners[cls], risk[cls] = name,weight

    if any(not model['floors'][cls] <= rate <= model['maxima'][cls] for cls,(rate,_) in current.items()):
        # An admitted contract change may require moving back to the verified
        # operator baseline. The solver never optimizes an invalid start state.
        target = fallback
    else:
        target = {cls:(rate,model['class_ceiling'][cls]) for cls,(rate,_) in current.items()}
    if target != current:
        diagnostics.update(limiting_constraints=['authorized_class_bounds'],
                           alternatives_deferred='reconcile_authorized_bounds_first',
                           diff=adaptive_tc.diff(current,target))
        return dict(detail, action='enforce_class_ceiling' if algorithm=='adaptive' else 'comparison_only',proposed=target)

    rows = {}
    estimates = {}
    for cls in CLASSES:
        rate = current[cls][0]
        queue = queues.get(cls,{})
        served = queue.get('bytes_per_second')
        backlog = (queue.get('backlog') or {}).get('p95_bytes')
        delivered = max(0,int(served/125)) if served is not None else None
        queue_rate = max(0,int(backlog/(125*cfg['window_seconds']))) if backlog is not None else 0
        demand = max((delivered or 0)+queue_rate,targets[cls])
        if cls in owners:
            demand = max(demand,rate+step)
        if cls == 'background' and (queue.get('backlogged_samples') or 0) >= 2:
            demand = max(demand,rate)
        rows[cls] = dict(floor=model['floors'][cls], limit=model['maxima'][cls], demand=min(capacity,demand),
                         current=rate, baseline=fallback[cls][0], priority=PRIORITY[cls],
                         utility=PRIORITY[cls]*100+risk.get(cls,0), eligible=int(cls in owners))
        estimates[cls] = dict(delivered_kbit=delivered, queued_equivalent_kbit=queue_rate,
                             desired_kbit=targets[cls], slo_risk_weight=risk.get(cls,0), eligible_service=owners.get(cls),
                             ceiling_kbit=model['class_ceiling'][cls])
    diagnostics.update(classes=rows, estimates=estimates,
                       soft_target_overcommit_kbit=max(0,diagnostics['desired_total_kbit']-capacity))

    began = time.perf_counter_ns()
    try:
        for alternative in ALGORITHMS:
            rates, score = controller.optimizer.solve(alternative,capacity,step,deadband,rows)
            diagnostics['alternatives'][alternative] = dict(rates_kbit=rates,surrogate_score=score,
                  disposition='selected' if alternative==algorithm else 'comparison_only')
    except (OSError,ValueError,AttributeError) as error:
        diagnostics['stage'] = 'SOLVER_UNAVAILABLE'
        diagnostics['conflicts'].append(dict(code='SOLVER_FAILURE',detail=str(error)))
        return detail
    diagnostics['solver_and_bridge_ns'] = time.perf_counter_ns()-began

    selected_summary = diagnostics['alternatives'][algorithm]
    for label, alternative in diagnostics['alternatives'].items():
        alternative['score_minus_selected'] = alternative['surrogate_score']-selected_summary['surrogate_score']
        if label != algorithm:
            alternative['reason'] = ('static_fallback' if label == 'static' else
                                     'alternative_allocation_outside_selected_strategy')
    selected = selected_summary['rates_kbit']
    target = {cls:(rate,current[cls][1]) for cls,rate in selected.items()}
    limiting = [f'{cls}:rate_limit' for cls,row in rows.items() if selected[cls]==row['limit']]
    limiting += [f'{cls}:protected_floor' for cls,row in rows.items() if selected[cls]==row['floor']]
    if sum(selected.values()) == capacity:
        limiting.append('parent_capacity')
    if not allow_adaptive or bg is None:
        limiting.append('incomplete_or_stale_telemetry')
    elif not owners:
        limiting.append('no_eligible_service_pressure')
    elif not adaptive_tc.diff(current,target):
        limiting.append('no_improving_transfer_above_deadband')
    diagnostics.update(limiting_constraints=limiting,diff=adaptive_tc.diff(current,target))
    if algorithm != 'adaptive':
        return dict(detail,action='comparison_only',proposed=target)
    receiver = next((cls for cls in CLASSES if target[cls][0] > current[cls][0]),None)
    if receiver:
        return dict(detail,action='transfer_reservation',proposed=target,service=owners[receiver],
                    prediction='improve class reservation utility under sustained pressure')
    return detail
