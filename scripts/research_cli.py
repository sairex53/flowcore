"""Argparse integration for the FLOWCORE Research Lab."""
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
    compare_cmd = bench.add_parser('compare')
    compare_cmd.add_argument('baseline',type=Path)
    compare_cmd.add_argument('candidate',type=Path)
    compare_cmd.add_argument('--baseline-mode',choices=MODES)
    compare_cmd.add_argument('--candidate-mode',choices=MODES)
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
    if action=='compare':
        baseline = load_summary(args.baseline)
        candidate = load_summary(args.candidate)
        result = compare(baseline,candidate,args.baseline_mode,args.candidate_mode)
        return result, int(any(item['status']=='confirmed_regression' for item in result['findings']))
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
            if args.command=='chaos' and not manifest['faults']:
                raise ValueError('chaos run requires at least one scheduled transition')
            if getattr(args,'quick',False):
                manifest['repetitions'] = 1
            output,data = execute(manifest,label='chaos' if args.command=='chaos' else 'research')
            page = report(output/'summary.json')
            outputs.append(dict(directory=str(output),report=str(page),
                valid=sum(r['valid'] for r in data['runs']),invalid=sum(not r['valid'] for r in data['runs'])))
    return dict(experiments=outputs),int(any(item['invalid'] for item in outputs))
