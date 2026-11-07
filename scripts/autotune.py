"""Bounded, isolated HTB policy search with independent holdout confirmation."""
import hashlib
import json
import math
import os
import random
import statistics
import tempfile
import time
from pathlib import Path

from research_manifest import load_manifest, validate
from research_runner import execute, owned_directory
from research_stats import value

ALGORITHMS = ("manual", "random", "grid", "coordinate", "local")
WEIGHT_DEFAULTS = dict(p99_ms=.30, deadline_attainment=.20, failure_ratio=.15,
                       background_mbps=.15, jain_index=.07, cpu_seconds=.05,
                       peak_daemon_rss_bytes=.03, adjustments_per_minute=.05)
HARD_DEFAULTS = dict(max_failure_ratio=1.0, min_deadline_attainment=0.0,
                     min_background_mbps=0.0, min_jain_index=0.0, min_objective_improvement=.01)
PLAN_FIELDS = {"schema", "algorithm", "training", "holdouts", "search", "objectives",
               "hard_constraints", "budget", "capabilities", "created_unix_ns"}
_MAX_PLAN = 4 * 1024 * 1024


def _finite(value, low, high, name, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    if integer and type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _manifest_snapshot(path):
    resolved = Path(path).resolve(strict=True)
    manifest = load_manifest(resolved)
    if manifest["version"] not in (2, 3):
        raise ValueError("autotuning requires Research Lab manifest v2 or v3")
    return dict(source=str(resolved), sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
                manifest=manifest)


def _distinct_holdout(training, holdout):
    if holdout["version"] != training["version"]:
        raise ValueError("training and holdout manifests must use the same schema version")
    if all(holdout[key] == training[key] for key in ("network", "workload")):
        raise ValueError("holdout must change network conditions or workload; seeds alone do not establish generalization")
    if holdout["seed"] == training["seed"]:
        raise ValueError("holdout seed must differ from the training seed")


def _static_pct(mode):
    if not isinstance(mode, str) or not mode.startswith("static") or not mode[6:].isdigit():
        raise ValueError("autotuning only supports static HTB reservation modes")
    value = int(mode[6:])
    return _finite(value, 20, 40, "static critical reservation", True)


def _normalize_objectives(weights):
    if not isinstance(weights, dict) or set(weights) - WEIGHT_DEFAULTS.keys():
        raise ValueError("objective weights contain unsupported metrics")
    result = dict(WEIGHT_DEFAULTS)
    result.update(weights)
    for key, weight in result.items():
        _finite(weight, 0, 1, f"objective weight {key}")
    total = sum(result.values())
    if total <= 0:
        raise ValueError("at least one objective weight must be positive")
    return {key: value / total for key, value in result.items()}


def _hard_constraints(value):
    if not isinstance(value, dict) or set(value) - HARD_DEFAULTS.keys():
        raise ValueError("unsupported hard constraint")
    result = dict(HARD_DEFAULTS)
    result.update(value)
    for key, item in result.items():
        upper = 1 if key in ("max_failure_ratio", "min_deadline_attainment", "min_jain_index", "min_objective_improvement") else 1000000
        _finite(item, 0, upper, f"hard constraint {key}")
    return result


def _search_values(algorithm, baseline, count, step, seed):
    domain = list(range(20, 41))
    if algorithm == "manual":
        if count != 1:
            raise ValueError("manual search evaluates exactly the configured baseline")
        return dict(kind="fixed", values=[baseline])
    if algorithm == "grid":
        values = list(range(20, 41, step))
        if 40 not in values:
            values.append(40)
        if baseline not in values:
            values.append(baseline)
        values = sorted(set(values))
        if len(values) > count:
            raise ValueError(f"grid requires {len(values)} candidates, exceeding candidate budget {count}")
        return dict(kind="fixed", values=values)
    if algorithm == "random":
        if count < 2:
            raise ValueError("random search requires at least two candidates")
        remaining = [item for item in domain if item != baseline]
        values = sorted([baseline, *random.Random(seed).sample(remaining, min(count - 1, len(remaining)))])
        return dict(kind="fixed", values=values)
    if algorithm in ("coordinate", "local"):
        if count < 2:
            raise ValueError(f"{algorithm} search requires at least two candidates")
        return dict(kind="adaptive", initial=baseline, step=step)
    raise ValueError("unknown search algorithm")


def make_plan(manifest_path, algorithm="grid", candidate_budget=5, step=5, repetitions=10,
              seed=1, holdout_paths=(), weights=None, constraints=None, budget_seconds=3600,
              budget_cpu_seconds=3600, budget_memory_mb=2048, budget_disk_mb=2048):
    if algorithm not in ALGORITHMS:
        raise ValueError("unsupported search algorithm")
    candidate_budget = _finite(candidate_budget, 1, 21, "candidate budget", True)
    step = _finite(step, 1, 20, "search step", True)
    repetitions = _finite(repetitions, 10, 30, "independent repetitions", True)
    seed = _finite(seed, 0, 2**32 - 1, "search seed", True)
    training = _manifest_snapshot(manifest_path)
    base = training["manifest"]
    baseline = int(base["static_critical_pct"])
    search = _search_values(algorithm, baseline, candidate_budget, step, seed)
    holdouts = [_manifest_snapshot(path) for path in holdout_paths]
    if len(holdouts) > 8:
        raise ValueError("at most eight holdout scenarios are allowed")
    for snapshot in holdouts:
        _distinct_holdout(base, snapshot["manifest"])
    if any(snapshot["manifest"]["repetitions"] < 10 for snapshot in holdouts):
        raise ValueError("holdout manifests require at least ten repetitions")
    if weights is None:
        weights = {}
    if constraints is None:
        constraints = {}
    budget = dict(max_test_seconds=_finite(budget_seconds, 1, 7*24*3600, "test-time budget"),
                  max_daemon_cpu_seconds=_finite(budget_cpu_seconds, 1, 7*24*3600, "daemon CPU budget"),
                  max_daemon_rss_bytes=int(_finite(budget_memory_mb, 16, 65536, "memory budget") * 1024 * 1024),
                  max_artifact_bytes=int(_finite(budget_disk_mb, 16, 65536, "artifact budget") * 1024 * 1024),
                  max_wall_seconds=_finite(budget_seconds, 1, 7*24*3600, "wall-time budget"))
    service_cfg = json.loads((Path(__file__).resolve().parents[1]/"config/service.json").read_text())
    classes = service_cfg["policies"]["default"]["classes"]
    capabilities = dict(searchable_parameters=["HTB critical minimum reservation (20–40% of owned lab rate)"],
        fixed_parameters=dict(parent_rate_kbit=base["network"]["bandwidth_kbit"],
            background_minimum_pct="60 minus critical reservation; at least 20%",
            critical_ceiling_pct=classes["critical"]["max_pct"],
            background_ceiling_pct=classes["background"]["max_pct"],
            class_priorities={name: row["priority"] for name,row in classes.items()},
            leaf_qdisc="fq_codel with Research Lab defaults",
            controller_parameters="not active for static reservation candidates",
            telemetry_interval_seconds=1),
        unsupported_search_parameters=["custom HTB ceil", "class priority", "qdisc target/interval/limit",
            "controller interval/hysteresis/action limits", "telemetry rate", "fairness/SLO controller weights"],
        isolation="owned veth namespaces only; no host interface policy mutation")
    plan = dict(schema=1, algorithm=algorithm, training=training, holdouts=holdouts,
                search=dict(**search, candidate_budget=candidate_budget, step=step, repetitions=repetitions,
                            seed=seed, baseline_pct=baseline),
                objectives=_normalize_objectives(weights), hard_constraints=_hard_constraints(constraints),
                budget=budget, capabilities=capabilities, created_unix_ns=time.time_ns())
    validate_plan(plan)
    estimated_modes = len(search.get("values", [baseline, min(40, baseline + step), max(20, baseline-step)]))
    estimated = base["duration_seconds"] * repetitions * estimated_modes
    estimated += sum(item["manifest"]["duration_seconds"] * max(10,repetitions) * 2 for item in holdouts)
    if estimated > budget["max_test_seconds"]:
        raise ValueError(f"training search requires at least {estimated} test seconds, over the {budget['max_test_seconds']} second budget")
    return plan


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS or plan.get("schema") != 1:
        raise ValueError("invalid autotuning plan schema")
    if plan["algorithm"] not in ALGORITHMS or not isinstance(plan["training"], dict) or not isinstance(plan["holdouts"], list):
        raise ValueError("invalid autotuning plan header")
    search = plan["search"]
    common={"kind", "step", "candidate_budget", "seed", "repetitions", "baseline_pct"}
    if not isinstance(search, dict) or search.get("kind") not in ("fixed","adaptive"):
        raise ValueError("invalid search configuration")
    expected_keys=common | ({"values"} if search["kind"]=="fixed" else {"initial"})
    if set(search)!=expected_keys:
        raise ValueError("search configuration contains missing or unexpected fields")
    if ((plan["algorithm"] in ("manual","random","grid")) != (search["kind"]=="fixed")):
        raise ValueError("search algorithm and candidate plan disagree")
    _finite(search["step"], 1, 20, "search step", True)
    _finite(search["candidate_budget"], 1, 21, "candidate budget", True)
    _finite(search["repetitions"], 10, 30, "independent repetitions", True)
    _finite(search["seed"], 0, 2**32-1, "search seed", True)
    baseline = _finite(search["baseline_pct"], 20, 40, "baseline reservation", True)
    if search["kind"] == "fixed":
        values = search.get("values")
        if not isinstance(values, list) or not values or len(values) > search["candidate_budget"]:
            raise ValueError("invalid fixed candidate list")
        values = [_finite(value, 20, 40, "candidate reservation", True) for value in values]
        if len(set(values)) != len(values) or baseline not in values:
            raise ValueError("candidate list must be unique and contain the manual baseline")
    elif search["kind"] == "adaptive":
        initial=_finite(search["initial"], 20, 40, "initial reservation", True)
        if initial!=baseline:
            raise ValueError("adaptive search must start at the manual baseline")
    else:
        raise ValueError("invalid search kind")
    if plan["algorithm"] == "manual" and search.get("values") != [baseline]:
        raise ValueError("manual plan must contain only its baseline")
    caps=plan["capabilities"]
    if (not isinstance(caps,dict) or not isinstance(caps.get("searchable_parameters"),list)
            or not isinstance(caps.get("fixed_parameters"),dict)
            or not isinstance(caps.get("unsupported_search_parameters"),list)
            or not isinstance(caps.get("isolation"),str)):
        raise ValueError("invalid capability-aware search description")
    plan["objectives"]=_normalize_objectives(plan["objectives"])
    plan["hard_constraints"]=_hard_constraints(plan["hard_constraints"])
    budget = plan["budget"]
    budget_keys = {"max_test_seconds", "max_daemon_cpu_seconds", "max_daemon_rss_bytes",
                   "max_artifact_bytes", "max_wall_seconds"}
    if not isinstance(budget, dict) or set(budget) != budget_keys:
        raise ValueError("invalid autotuning budgets")
    _finite(budget["max_test_seconds"], 1, 7*24*3600, "test-time budget")
    _finite(budget["max_wall_seconds"], 1, 7*24*3600, "wall-time budget")
    _finite(budget["max_daemon_cpu_seconds"], 1, 7*24*3600, "daemon CPU budget")
    for key in ("max_daemon_rss_bytes", "max_artifact_bytes"):
        _finite(budget[key], 16*1024*1024, 65536*1024*1024, key, True)
    for snapshot in [plan["training"], *plan["holdouts"]]:
        if not isinstance(snapshot, dict) or set(snapshot) != {"source", "sha256", "manifest"}:
            raise ValueError("invalid manifest snapshot")
        manifest = validate(snapshot["manifest"])
        if manifest["version"] not in (2, 3):
            raise ValueError("autotuning requires manifest v2 or v3")
        if not isinstance(snapshot["source"], str) or not isinstance(snapshot["sha256"], str) or len(snapshot["sha256"]) != 64:
            raise ValueError("invalid manifest provenance")
    for snapshot in plan["holdouts"]:
        _distinct_holdout(plan["training"]["manifest"], snapshot["manifest"])
    return plan


def load_plan(path):
    payload = Path(path).read_bytes()
    if len(payload) > _MAX_PLAN:
        raise ValueError("autotuning plan exceeds 4 MiB")
    return validate_plan(json.loads(payload))


def _summary(rows_by_mode, mode):
    return [row for row in rows_by_mode.get(mode, []) if row.get("valid")]


def _paired_improvement(base, candidate, weights):
    base_fail = value(base, "failure_ratio")
    cand_fail = value(candidate, "failure_ratio")
    deltas = {}
    if base_fail is not None and cand_fail is not None:
        deltas["failure_ratio"] = base_fail - cand_fail
    for key, direction in (("p99_ms", "lower"), ("background_mbps", "higher"),
                           ("jain_index", "higher"), ("cpu_seconds", "lower"),
                           ("peak_daemon_rss_bytes", "lower"),
                           ("adjustments_per_minute", "lower"),
                           ("deadline_attainment", "higher")):
        a, b = value(base, key), value(candidate, key)
        if a is None or b is None:
            continue
        if key == "p99_ms" and (base.get("failed", 0) or candidate.get("failed", 0)):
            continue
        if key in ("deadline_attainment", "jain_index"):
            deltas[key] = b-a if direction == "higher" else a-b
        else:
            denominator = max(abs(a), abs(b), 1.0)
            deltas[key] = (a-b)/denominator if direction == "lower" else (b-a)/denominator
    available = [(weights[key], delta) for key, delta in deltas.items() if weights.get(key, 0) > 0]
    total = sum(weight for weight, _ in available)
    return (sum(weight*delta for weight, delta in available)/total, deltas) if total else (None, deltas)


def _bootstrap(values, seed, draws=10000):
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = [statistics.median(rng.choices(values, k=n)) for _ in range(draws)]
    means.sort()
    return dict(low=means[int(.025*(draws-1))], median=statistics.median(values),
                high=means[int(.975*(draws-1))], confidence=.95, method="paired run-block percentile bootstrap",
                independent_units=n, resamples=draws)


def _candidate_result(summary, baseline_mode, candidate_mode, plan, bootstrap_seed):
    grouped = {}
    for row in summary["runs"]:
        grouped.setdefault(row["mode"], {})[row["repetition"]] = row
    base_rows = grouped.get(baseline_mode, {})
    candidate_rows = grouped.get(candidate_mode, {})
    paired, component_rows = [], []
    for rep in sorted(base_rows.keys() & candidate_rows.keys()):
        a, b = base_rows[rep], candidate_rows[rep]
        if not a.get("valid") or not b.get("valid") or a.get("workload_seed") != b.get("workload_seed"):
            continue
        score, components = _paired_improvement(a, b, plan["objectives"])
        if score is not None:
            paired.append(score)
            component_rows.append(components)
    ci = _bootstrap(paired, bootstrap_seed)
    agg = dict(valid_candidate=sum(row.get("valid", False) for row in candidate_rows.values()),
               valid_baseline=sum(row.get("valid", False) for row in base_rows.values()),
               pairs=len(paired), median_objective_improvement=statistics.median(paired) if paired else None,
               mean_objective_improvement=statistics.mean(paired) if paired else None, confidence_interval=ci)
    metrics = {}
    for key in ("p50_ms", "p95_ms", "p99_ms", "failure_ratio", "deadline_attainment",
                "background_mbps", "jain_index", "cpu_seconds", "peak_daemon_rss_bytes",
                "adjustments_per_minute"):
        vals = [value(row, key) for row in candidate_rows.values() if row.get("valid")]
        vals = [item for item in vals if item is not None]
        metrics[key] = dict(n=len(vals), median=statistics.median(vals) if vals else None)
    agg["candidate_metrics"] = metrics
    agg["paired_components_median"] = {
        key: statistics.median([row[key] for row in component_rows if key in row])
        for key in WEIGHT_DEFAULTS if any(key in row for row in component_rows)
    }
    agg["paired_component_intervals"] = {
        key: _bootstrap([row[key] for row in component_rows if key in row],
                        bootstrap_seed ^ (sum(key.encode("utf-8")) * 2246822519))
        for key in WEIGHT_DEFAULTS if any(key in row for row in component_rows)
    }
    agg["valid"] = agg["valid_candidate"] == summary["manifest"]["repetitions"] and agg["valid_baseline"] == summary["manifest"]["repetitions"]
    agg["hard_constraints"] = _constraint_findings(metrics, plan["hard_constraints"])
    return agg


def _constraint_findings(metrics, constraints):
    values = dict(max_failure_ratio=metrics["failure_ratio"]["median"],
                  min_deadline_attainment=metrics["deadline_attainment"]["median"],
                  min_background_mbps=metrics["background_mbps"]["median"],
                  min_jain_index=metrics["jain_index"]["median"])
    findings = {}
    for key, actual in values.items():
        limit = constraints[key]
        if actual is None:
            findings[key] = dict(status="unmeasured", observed=None, limit=limit)
        elif key == "max_failure_ratio":
            findings[key] = dict(status="pass" if actual <= limit else "fail", observed=actual, limit=limit)
        else:
            findings[key] = dict(status="pass" if actual >= limit else "fail", observed=actual, limit=limit)
    return findings


def _tree_bytes(path):
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())


def _write_state(directory, state):
    directory=Path(directory)
    payload=json.dumps(state,indent=2,allow_nan=False)+"\n"
    fd,name=tempfile.mkstemp(prefix=".job-",dir=directory)
    temporary=Path(name)
    owner=directory.stat()
    try:
        os.fchown(fd,owner.st_uid,owner.st_gid)
        with os.fdopen(fd,"w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,directory/"job.json")
    finally:
        temporary.unlink(missing_ok=True)


def _execute_capture(manifest, label, parent, state, job, values, baseline, seed):
    before={item.resolve() for item in parent.iterdir() if item.is_dir()}
    try:
        return execute(manifest,label=label,parent=parent)
    except KeyboardInterrupt:
        for directory in parent.iterdir():
            if not directory.is_dir() or directory.resolve() in before:
                continue
            summary=directory/"summary.json"
            data=json.loads(summary.read_bytes()) if summary.is_file() else {}
            rows=data.get("runs",[])
            state["stages"].append(dict(label=label,directory=str(directory),seed=seed,
                candidates=list(values),baseline_pct=baseline,interrupted=True,
                valid_runs=sum(row.get("valid",False) for row in rows),
                invalid_runs=sum(not row.get("valid",False) for row in rows)))
        _write_state(job,state)
        raise


def _seeded_manifest(source, seed, modes, repetitions):
    manifest = dict(source)
    manifest["seed"] = seed
    manifest["repetitions"] = repetitions
    manifest["modes"] = sorted(set(modes), key=lambda item: int(item[6:]))
    baseline_mode = f"static{manifest['static_critical_pct']}"
    if baseline_mode not in manifest["modes"]:
        manifest["modes"].append(baseline_mode)
        manifest["modes"].sort(key=lambda item: int(item[6:]))
    manifest["gates"] = dict(manifest["gates"])
    manifest["gates"]["baseline"] = baseline_mode
    return validate(manifest)


def _batch_result(summary_path, baseline, values, plan, seed):
    from research_stats import load_summary
    summary = load_summary(summary_path)
    reports = {value: _candidate_result(summary, f"static{baseline}", f"static{value}", plan,
                                        seed ^ (value * 2654435761)) for value in values}
    return reports


def run_plan(plan_path):
    plan = load_plan(plan_path)
    from paths import RESULTS
    job = RESULTS/f"autotune-{time.time_ns()}"
    owned_directory(job)
    os.chmod(job,0o700)
    stages = job/"stages"
    owned_directory(stages)
    os.chmod(stages,0o700)
    source = plan["training"]["manifest"]
    baseline = plan["search"]["baseline_pct"]
    rng = random.Random(plan["search"]["seed"])
    state = dict(schema=1, status="running", job_directory=str(job.resolve()), plan=str(Path(plan_path).resolve()),
                 plan_sha256=hashlib.sha256(Path(plan_path).read_bytes()).hexdigest(),
                 algorithm=plan["algorithm"], started_unix_ns=time.time_ns(), stages=[], evaluated_candidates=[],
                 selected_pct=None, holdouts=[], resource_usage=dict(test_seconds=0.0,cpu_seconds=0.0,
                      peak_daemon_rss_bytes=0,artifact_bytes=0), outcome="INCOMPLETE")
    _write_state(job, state)
    started = time.monotonic()
    seed_used = set()

    def new_seed(manifest):
        for _ in range(1000):
            seed = rng.randrange(0, 2**32)
            seeds = set(__import__("random").Random(seed).sample(range(1,2**32), plan["search"]["repetitions"]))
            if not (seed_used & seeds):
                seed_used.update(seeds)
                return seed
        raise RuntimeError("cannot allocate independent experiment block seeds")

    def evaluate_batch(manifest, candidates, label):
        values = sorted(set(candidates)|{baseline})
        previously = set(state["evaluated_candidates"])
        if len(previously | set(values)) > plan["search"]["candidate_budget"]:
            raise RuntimeError("candidate budget exhausted")
        run_reps = plan["search"]["repetitions"]
        planned = manifest["duration_seconds"]*run_reps*len(values)
        usage = state["resource_usage"]
        if usage["test_seconds"] + planned > plan["budget"]["max_test_seconds"]:
            raise RuntimeError("test-time budget exhausted before batch")
        if time.monotonic()-started+planned > plan["budget"]["max_wall_seconds"]:
            raise RuntimeError("wall-time budget cannot fit the next batch")
        trial_manifest = _seeded_manifest(manifest,new_seed(manifest),[f"static{x}" for x in values],run_reps)
        output, data = _execute_capture(trial_manifest,f"autotune-{label}",stages,state,job,values,baseline,trial_manifest["seed"])
        usage["test_seconds"] += planned
        usage["cpu_seconds"] += sum(row.get("cpu_seconds",0) or 0 for row in data["runs"] if row.get("valid"))
        usage["peak_daemon_rss_bytes"] = max([usage["peak_daemon_rss_bytes"], *[
            row.get("peak_daemon_rss_bytes",0) or 0 for row in data["runs"] if row.get("valid")]])
        usage["artifact_bytes"] = _tree_bytes(job)
        report = _batch_result(output/"summary.json",baseline,values,plan,trial_manifest["seed"])
        state["evaluated_candidates"] = sorted(previously | set(values))
        state["stages"].append(dict(label=label,directory=str(output),seed=trial_manifest["seed"],
                                    candidates=values,baseline_pct=baseline,reports=report,
                                    environment=data.get("environment"),
                                    valid_runs=sum(row.get("valid",False) for row in data["runs"]),
                                    invalid_runs=sum(not row.get("valid",False) for row in data["runs"])))
        _write_state(job,state)
        if usage["cpu_seconds"] > plan["budget"]["max_daemon_cpu_seconds"]:
            raise RuntimeError("measured daemon CPU budget exhausted")
        if usage["peak_daemon_rss_bytes"] > plan["budget"]["max_daemon_rss_bytes"]:
            raise RuntimeError("measured daemon RSS budget exhausted")
        if usage["artifact_bytes"] > plan["budget"]["max_artifact_bytes"]:
            raise RuntimeError("artifact storage budget exhausted")
        return report

    try:
        algorithm=plan["algorithm"]
        if plan["search"]["kind"] == "fixed":
            candidates=plan["search"]["values"]
            report=evaluate_batch(source,candidates,"search")
            selected=_select(report,baseline)
        else:
            center=baseline
            step=plan["search"]["step"]
            selected=baseline
            candidates_used=set()
            rounds=0
            while len(candidates_used) < plan["search"]["candidate_budget"] and step>=1:
                proposals={selected, max(20,center-step), min(40,center+step)}
                proposals-=candidates_used
                remaining=plan["search"]["candidate_budget"]-len(candidates_used)
                proposals=set(sorted(proposals,key=lambda value:(abs(value-center),value))[:remaining])
                proposals.add(selected)
                if not proposals:
                    step//=2
                    if step<1: break
                    continue
                report=evaluate_batch(source,proposals,f"{algorithm}-{rounds}")
                candidates_used|=proposals
                best=_select(report,baseline,allowed=proposals)
                current_score=report.get(selected,{}).get("median_objective_improvement")
                best_score=report.get(best,{}).get("median_objective_improvement")
                if algorithm=="local" and best_score is not None and (current_score is None or best_score>current_score):
                    center=best
                    selected=best
                else:
                    if best_score is not None and (current_score is None or best_score>current_score):
                        selected=best
                        center=best
                    step//=2
                rounds+=1
                if algorithm=="coordinate" and rounds>=1:
                    algorithm="local"
        state["selected_pct"]=selected
        state["selection"]="training-only provisional choice; no policy was applied"
        for index, snapshot in enumerate(plan["holdouts"]):
            holdout=snapshot["manifest"]
            if holdout["seed"]==source["seed"]:
                raise ValueError("holdout seed overlaps training manifest seed")
            if selected==baseline:
                state["holdouts"].append(dict(source=snapshot["source"],outcome="INCONCLUSIVE",
                    reason="selected policy equals baseline; no distinct canary comparison exists"))
                continue
            hm= dict(holdout)
            planned_holdout=hm["duration_seconds"]*max(10,plan["search"]["repetitions"])*2
            usage=state["resource_usage"]
            if usage["test_seconds"]+planned_holdout>plan["budget"]["max_test_seconds"]:
                raise RuntimeError("holdout test-time budget exhausted before scenario")
            if time.monotonic()-started+planned_holdout>plan["budget"]["max_wall_seconds"]:
                raise RuntimeError("holdout wall-time budget cannot fit scenario")
            hm["repetitions"]=max(holdout["repetitions"],plan["search"]["repetitions"])
            hm["static_critical_pct"]=baseline
            hm["modes"]=[f"static{baseline}",f"static{selected}"]
            hm["gates"]=dict(hm["gates"],baseline=f"static{baseline}")
            # Use disjoint block seeds even when user manifests happen to collide.
            seed=new_seed(holdout)
            holdout_manifest=_seeded_manifest(hm,seed,[f"static{baseline}",f"static{selected}"],hm["repetitions"])
            label=f"autotune-holdout-{index}"
            try:
                output,data=_execute_capture(holdout_manifest,label,stages,state,job,
                    [baseline,selected],baseline,holdout_manifest["seed"])
            except KeyboardInterrupt:
                new_dirs=[item for item in stages.iterdir() if item.is_dir() and label in item.name]
                state["holdouts"].append(dict(source=snapshot["source"],outcome="INCOMPLETE",
                    reason="interrupted; inspect the retained stage artifact",directory=str(new_dirs[-1]) if new_dirs else None))
                _write_state(job,state)
                raise
            comparison=_candidate_result(__import__("research_stats").load_summary(output/"summary.json"),
                f"static{baseline}",f"static{selected}",plan,seed ^ (selected * 2654435761))
            checks=comparison["hard_constraints"]
            passes=all(item["status"]=="pass" for item in checks.values())
            ci=comparison["confidence_interval"]
            minimum_effect=plan["hard_constraints"]["min_objective_improvement"]
            outcome=("INVALID EXPERIMENT" if not comparison["valid"] else
                     "REGRESSION" if not passes or (ci and ci["high"]<0) else
                     "PASS" if ci and ci["low"]>minimum_effect else "INCONCLUSIVE")
            state["holdouts"].append(dict(source=snapshot["source"],directory=str(output),
                seed=seed,selected_pct=selected,baseline_pct=baseline,outcome=outcome,
                comparison=comparison,environment=data.get("environment"),
                valid_runs=sum(r.get("valid",False) for r in data["runs"])))
            usage["test_seconds"] += holdout_manifest["duration_seconds"]*holdout_manifest["repetitions"]*2
            usage["cpu_seconds"] += sum(row.get("cpu_seconds",0) or 0 for row in data["runs"] if row.get("valid"))
            usage["peak_daemon_rss_bytes"] = max([usage["peak_daemon_rss_bytes"], *[
                row.get("peak_daemon_rss_bytes",0) or 0 for row in data["runs"] if row.get("valid")]])
            usage["artifact_bytes"]=_tree_bytes(job)
            _write_state(job,state)
            if usage["cpu_seconds"] > plan["budget"]["max_daemon_cpu_seconds"] or usage["peak_daemon_rss_bytes"] > plan["budget"]["max_daemon_rss_bytes"] or usage["artifact_bytes"]>plan["budget"]["max_artifact_bytes"]:
                raise RuntimeError("measured resource budget exhausted during holdout evaluation")
        # A recommendation is validated only when every requested holdout passes.
        holdout_outcomes=[item["outcome"] for item in state["holdouts"]]
        if not plan["holdouts"]:
            state["outcome"]="INCONCLUSIVE"
            state["recommendation"]="NOT VALIDATED: no independent holdout scenario was supplied"
        elif "INVALID EXPERIMENT" in holdout_outcomes:
            state["outcome"]="INVALID EXPERIMENT"
        elif "REGRESSION" in holdout_outcomes:
            state["outcome"]="REGRESSION"
        elif holdout_outcomes and all(item=="PASS" for item in holdout_outcomes):
            state["outcome"]="PASS"
        else:
            state["outcome"]="INCONCLUSIVE"
        state["recommendation"]=recommendation(state,plan)
        state["status"]="complete"
    except KeyboardInterrupt:
        state["status"]="interrupted"
        state["outcome"]="INCOMPLETE"
        state["recommendation"]="Interrupted; partial raw experiment files are retained."
        _write_state(job,state)
        raise
    except (OSError,RuntimeError,ValueError,KeyError) as error:
        state["status"]="budget_exhausted" if "budget" in str(error).lower() else "failed"
        state["outcome"]="INCOMPLETE"
        state["error"]=str(error)
        state["recommendation"]="No deployment recommendation; inspect the retained partial experiment artifacts."
    state["finished_unix_ns"]=time.time_ns()
    _write_state(job,state)
    return load_job(job)


def _select(reports, baseline, allowed=None):
    options=allowed if allowed is not None else reports.keys()
    eligible=[]
    for candidate in options:
        row=reports.get(candidate,{})
        constraints=row.get("hard_constraints",{})
        if not row.get("valid") or not all(item["status"]=="pass" for item in constraints.values()):
            continue
        score=row.get("median_objective_improvement")
        if score is not None:
            eligible.append((score,candidate))
    if not eligible:
        return baseline
    return max(eligible,key=lambda pair:(pair[0],pair[1]==baseline,-pair[1]))[1]


def recommendation(state, plan):
    selected=state.get("selected_pct")
    baseline=plan["search"]["baseline_pct"]
    if selected is None:
        return "No candidate was selected; keep the manual baseline."
    report_rows=[]
    for stage in state.get("stages",[]):
        if selected in stage["reports"]:
            report_rows.append(stage["reports"][selected])
    evidence=report_rows[-1] if report_rows else {}
    held=state.get("holdouts",[])
    outcomes=[row.get("outcome") for row in held]
    return dict(mode="shadow recommendation only; FLOWCORE never applies the result to host traffic",
        selected_critical_minimum_percent=selected,
        rollback_configuration=dict(critical_minimum_percent=baseline,
            background_minimum_percent=60-baseline,
            tested_namespace_configuration="original Research Lab baseline"),
        expected_benefit=evidence.get("median_objective_improvement"),
        training_evidence=evidence,
        independent_holdout_outcomes=outcomes,
        confidence_limit=("no holdout evidence supplied" if not held else
                          "holdout outcomes are run-level only; generalization is limited to tested manifests"),
        regressions=["HTB guaranteed rate is a reservation, not guaranteed application latency.",
                     "Successful-request P99 is censored whenever failures occur; compare failures and deadlines alongside it."],
        deployment_eligibility=state.get("outcome"),
        capability_limits=plan["capabilities"],
        tested_environments=[stage["environment"] for stage in state.get("stages",[]) if stage.get("environment")]
            + [item["environment"] for item in held if item.get("environment")])


def load_job(path):
    job=Path(path).resolve(strict=True)
    if not job.is_dir():
        raise ValueError("job path must be a directory")
    raw=(job/"job.json").read_bytes()
    if len(raw)>_MAX_PLAN:
        raise ValueError("job state exceeds size limit")
    state=json.loads(raw)
    if not isinstance(state,dict) or state.get("schema")!=1 or state.get("status") not in ("running","complete","interrupted","budget_exhausted","failed"):
        raise ValueError("invalid autotuning job state")
    return state


def status(path):
    state=load_job(path)
    stages=[]
    if state["status"]=="running":
        for directory in (Path(path)/"stages").iterdir():
            summary=directory/"summary.json"
            if not summary.is_file():
                continue
            payload=summary.read_bytes()
            if len(payload)>16*1024*1024:
                continue
            data=json.loads(payload)
            if data.get("status")!="running":
                continue
            rows=data.get("runs",[])
            stages.append(dict(directory=str(directory),completed_runs=len(rows),
                expected_runs=data["manifest"]["repetitions"]*len(data["manifest"]["modes"]),
                valid_runs=sum(row.get("valid",False) for row in rows),
                invalid_runs=sum(not row.get("valid",False) for row in rows),
                candidates=data["manifest"]["modes"],block_seeds=data.get("block_seeds",[])))
    return {key:state.get(key) for key in ("status","outcome","algorithm","started_unix_ns",
            "finished_unix_ns","selected_pct","resource_usage","error")} | {"active_stages":stages}


def results(path):
    state=load_job(path)
    return dict(status=state["status"],outcome=state["outcome"],algorithm=state["algorithm"],
        selected_pct=state["selected_pct"],stages=state["stages"],holdouts=state["holdouts"],
        resource_usage=state["resource_usage"],recommendation=state.get("recommendation"),
        error=state.get("error"))


def compare_summaries(baseline_path,candidate_path,baseline_mode,candidate_mode):
    from research_stats import compare,load_summary
    return compare(load_summary(baseline_path),load_summary(candidate_path),baseline_mode,candidate_mode)
