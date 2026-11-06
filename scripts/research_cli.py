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
    render = bench.add_parser('report')
    render.add_argument('summary',type=Path)
    render.add_argument('--baseline',type=Path)
    shrink = bench.add_parser('minimize')
    shrink.add_argument('manifest',type=Path)
    shrink.add_argument('--mode',choices=MODES,required=True)
    shrink.add_argument('--failure-ratio',type=float,required=True)
    shrink.add_argument('--budget',type=int,default=6)
    chaos = commands.add_parser('chaos',help='controlled laboratory fault injection').add_subparsers(dest='action',required=True)
    chaos.add_parser('run').add_argument('manifest',type=Path)
    chaos.add_parser('stop')


def dispatch(args):
    action = args.action
    if args.command=='chaos' and action=='stop':
        return cancel(),0
    if action=='soak':
        manifest = load_manifest(args.manifest)
        mode = args.mode or manifest['modes'][0]
        with exclusive():
            from research_soak import run_soak
            return run_soak(manifest,args.cycles,mode,args.max_minutes,
                            args.max_rss_growth_mb,args.max_fd_growth,args.max_bpf_growth),0
    if action=='compare':
        baseline = load_summary(args.baseline)
        candidate = load_summary(args.candidate)
        result = compare(baseline,candidate,args.baseline_mode,args.candidate_mode)
        return result, (2 if result.get('outcome')=='INVALID EXPERIMENT' else
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
    return dict(experiments=outputs), (2 if any(item['outcome']=='INVALID EXPERIMENT' for item in outputs)
        else int(any(item['outcome']=='REGRESSION' for item in outputs)))
