"""Argparse integration for the FLOWCORE Research Lab."""
import json
from pathlib import Path

from benchmark import ROOT
from research_manifest import MODES, load_manifest
from research_minimize import minimize
from research_report import report
from research_runner import cancel, exclusive, execute
from research_stats import compare, load_summary
from service_config import number


def add_commands(commands):
    bench = commands.add_parser('bench', help='run and analyze research manifests').add_subparsers(dest='action',required=True)
    for action in ('run','replay'):
        bench.add_parser(action).add_argument('manifest',type=Path)
    bench.add_parser('suite').add_argument('--quick',action='store_true',help='one repetition per scenario')
    soak = bench.add_parser('soak',help='repeat isolated trials with bounded resource monitoring')
    soak.add_argument('manifest',type=Path)
    soak.add_argument('--cycles',type=int,default=10)
    soak.add_argument('--mode')
    soak.add_argument('--max-minutes',type=float,default=60)
    soak.add_argument('--max-rss-growth-mb',type=float,default=256)
    soak.add_argument('--max-fd-growth',type=int,default=32)
    soak.add_argument('--max-bpf-growth',type=int,default=8)
    compare_cmd = bench.add_parser('compare')
    compare_cmd.add_argument('baseline',type=Path)
    compare_cmd.add_argument('candidate',type=Path)
    compare_cmd.add_argument('--baseline-mode',choices=MODES)
    compare_cmd.add_argument('--candidate-mode',choices=MODES)
    tuning = bench.add_parser('tune-static')
    for name in ('pilot', 'evaluation', 'output'):
        tuning.add_argument(name, type=Path)
    bench.add_parser('verify', help='verify checksums for an integrity-enabled experiment summary').add_argument('summary',type=Path)
    render = bench.add_parser('report')
    render.add_argument('summary',type=Path)
    render.add_argument('--baseline',type=Path)
    shrink = bench.add_parser('minimize')
    shrink.add_argument('manifest',type=Path)
    shrink.add_argument('--mode',choices=MODES,required=True)
    shrink.add_argument('--failure-ratio',type=float,required=True)
    shrink.add_argument('--budget',type=int,default=6)
    optimize = commands.add_parser('optimize', help='bounded isolated search with independent holdout evaluation').add_subparsers(dest='action',required=True)
    plan = optimize.add_parser('plan', help='validate a search and write a reproducible plan; does not run experiments')
    plan.add_argument('manifest', type=Path)
    plan.add_argument('--algorithm', choices=('manual','random','grid','coordinate','local'), default='grid')
    plan.add_argument('--candidate-budget', type=int, default=5)
    plan.add_argument('--step', type=int, default=5)
    plan.add_argument('--repetitions', type=int, default=10)
    plan.add_argument('--seed', type=int, default=1)
    plan.add_argument('--holdout', type=Path, action='append', default=[])
    plan.add_argument('--weights', type=Path, help='JSON objective-weight object')
    plan.add_argument('--constraints', type=Path, help='JSON hard-constraint object')
    plan.add_argument('--budget-seconds', type=float, default=3600)
    plan.add_argument('--budget-cpu-seconds', type=float, default=3600)
    plan.add_argument('--budget-memory-mb', type=float, default=2048)
    plan.add_argument('--budget-disk-mb', type=float, default=2048)
    plan.add_argument('--output', type=Path)
    optimize.add_parser('run', help='execute a validated plan only in owned Research Lab namespaces').add_argument('plan', type=Path)
    for action in ('status','results','recommend'):
        optimize.add_parser(action).add_argument('job', type=Path)
    compare_cmd=optimize.add_parser('compare', help='compare two Research Lab summaries')
    compare_cmd.add_argument('baseline', type=Path)
    compare_cmd.add_argument('candidate', type=Path)
    compare_cmd.add_argument('--baseline-mode', required=True)
    compare_cmd.add_argument('--candidate-mode', required=True)
    chaos = commands.add_parser('chaos',help='controlled laboratory fault injection').add_subparsers(dest='action',required=True)
    chaos.add_parser('run').add_argument('manifest',type=Path)
    chaos.add_parser('stop')


def dispatch(args):
    action = args.action
    if args.command == 'optimize':
        from autotune import make_plan, load_plan, load_job, results as optimize_results, run_plan, status as optimize_status
        from paths import RESULTS
        if action == 'plan':
            def read_object(path, label):
                if path is None:
                    return None
                value=json.loads(path.read_bytes())
                if not isinstance(value,dict):
                    raise ValueError(f'{label} must contain a JSON object')
                return value
            plan=make_plan(args.manifest,args.algorithm,args.candidate_budget,args.step,
                args.repetitions,args.seed,args.holdout,read_object(args.weights,'weights'),
                read_object(args.constraints,'constraints'),args.budget_seconds,
                args.budget_cpu_seconds,args.budget_memory_mb,args.budget_disk_mb)
            RESULTS.mkdir(parents=True,exist_ok=True)
            output=args.output or RESULTS/f'autotune-plan-{plan["created_unix_ns"]}.json'
            with output.open('x') as stream:
                json.dump(plan,stream,indent=2,allow_nan=False)
                stream.write('\n')
            return dict(plan=str(output.resolve()),algorithm=plan['algorithm'],
                candidate_budget=plan['search']['candidate_budget'],repetitions=plan['search']['repetitions'],
                holdouts=len(plan['holdouts']),application_mode='recommendation only'),0
        if action == 'run':
            load_plan(args.plan)
            with exclusive():
                state=run_plan(args.plan)
            code=2 if state['status'] not in ('complete',) or state['outcome'] in ('REGRESSION','INVALID EXPERIMENT') else 0
            return dict(job=state['job_directory'],status=state['status'],outcome=state['outcome'],
                selected_pct=state.get('selected_pct'),recommendation=state.get('recommendation'),
                error=state.get('error')),code
        if action in ('status','results','recommend'):
            state=load_job(args.job)
            if action=='status':
                return optimize_status(args.job),0
            if action=='results':
                return optimize_results(args.job),0
            return state.get('recommendation') or dict(state=state['status'],recommendation='No recommendation is available yet.'),0
        if action == 'compare':
            from autotune import compare_summaries
            result=compare_summaries(args.baseline,args.candidate,args.baseline_mode,args.candidate_mode)
            return result,2 if result.get('outcome')=='INVALID EXPERIMENT' else int(result.get('outcome')=='REGRESSION')
    if args.command=='chaos' and action=='stop':
        return cancel(),0
    if action=='soak':
        manifest = load_manifest(args.manifest)
        mode = args.mode or manifest['modes'][0]
        with exclusive():
            from research_soak import run_soak
            return run_soak(manifest,args.cycles,mode,args.max_minutes,
                            args.max_rss_growth_mb,args.max_fd_growth,args.max_bpf_growth),0
    if action=='verify':
        data = load_summary(args.summary)
        if not data['manifest'].get('integrity'):
            raise ValueError('summary does not contain Measurement Integrity V3 artifacts')
        rows = [dict(artifact=row['artifact'], **row.get('artifact_integrity',dict(verified=False)))
                for row in data['runs']]
        valid = all(row.get('verified') for row in rows)
        return dict(summary=str(args.summary),verified=valid,runs=rows), 0 if valid else 2
    if action=='compare':
        baseline = load_summary(args.baseline)
        candidate = load_summary(args.candidate)
        result = compare(baseline,candidate,args.baseline_mode,args.candidate_mode)
        return result, (2 if result.get('outcome') in ('INVALID EXPERIMENT','FAILED_WORKLOAD') else
                        int(result.get('outcome')=='REGRESSION' or
                            any(item['status']=='confirmed_regression' for item in result['findings'])))
    if action=='tune-static':
        from static_pilot import tune
        return tune(args.pilot,args.evaluation,args.output),0
    if action=='report':
        return dict(report=str(report(args.summary,args.baseline))),0
    if action=='minimize':
        number(args.failure_ratio,.0001,1)
        number(args.budget,2,20,True)
        manifest = load_manifest(args.manifest)
        with exclusive():
            return minimize(manifest,args.mode,args.failure_ratio,args.budget),0
    paths = sorted((ROOT/'scenarios').glob('*.json')) if action=='suite' else [args.manifest]
    outputs = []
    with exclusive():
        for path in paths:
            manifest = load_manifest(path)
            if args.command=='chaos' and not (manifest.get('events') or manifest['faults']):
                raise ValueError('chaos run requires at least one scheduled transition')
            if getattr(args,'quick',False):
                manifest['repetitions'] = 1
            output,data = execute(manifest,label='chaos' if args.command=='chaos' else 'research')
            page = report(output/'summary.json')
            rendered = json.loads((output/'report.json').read_text())
            outputs.append(dict(directory=str(output),report=str(page),outcome=rendered['outcome'],
                valid=sum(r['valid'] for r in data['runs']),invalid=sum(not r['valid'] for r in data['runs'])))
    return dict(experiments=outputs), (2 if any(item['outcome'] in ('INVALID EXPERIMENT','FAILED_WORKLOAD')
                                                   for item in outputs)
        else int(any(item['outcome']=='REGRESSION' for item in outputs)))
