#!/usr/bin/env python3
"""Report per-trial tails and gates without pooling samples across trials."""
import argparse
import json
import statistics
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    summary = json.loads((args.directory / 'summary.json').read_text())
    rows = summary['results']
    modes = sorted({row['mode'] for row in rows})
    print('| Mode | Runs | P99 median (ms) | P99 range (ms) | BG median (Mbit/s) | Minimum Jain | CPU busy median (%) | Failed requests |')
    print('|---|---:|---:|---:|---:|---:|---:|---:|')
    medians = {}
    for mode in modes:
        trials = [row for row in rows if row['mode'] == mode]
        tails = [row['p99_ms'] for row in trials]
        medians[mode] = statistics.median(tails)
        print(f"| {mode} | {len(trials)} | {medians[mode]:.2f} | {min(tails):.2f}–{max(tails):.2f} | "
              f"{statistics.median(row['background_mbps'] for row in trials):.2f} | "
              f"{min(row['jain_index'] for row in trials):.3f} | "
              f"{statistics.median(row['host_cpu_busy_percent'] for row in trials):.1f} | "
              f"{sum(row['failed'] for row in trials)} |")
    print(f"\nPilot-selected static policy: {summary['selected_static']}")
    if 'flowcore' in medians:
        flow_rows = [row for row in rows if row['mode'] == 'flowcore']
        constraints = all(row['bandwidth_gate'] and row['failed'] == 0 for row in flow_rows)
        if 'fifo' in medians:
            improvement = 100 * (1 - medians['flowcore'] / medians['fifo'])
            print(f'FLOWCORE P99 reduction versus FIFO: {improvement:.1f}%')
            print(f'Initial gate (>=20% reduction plus bandwidth/fairness): {constraints and improvement >= 20}')
        for mode in ['fq_codel', 'cake', summary['selected_static']]:
            if mode in medians:
                change = 100 * (1 - medians['flowcore'] / medians[mode])
                print(f'FLOWCORE P99 reduction versus {mode}: {change:.1f}%')
        transitions = 0
        for row in flow_rows:
            path = args.directory / row['trial'] / 'controller.stdout'
            transitions += sum(json.loads(line)['event'] == 'transition' for line in path.read_text().splitlines())
        print(f'Measured adaptive transitions: {transitions}')
        if transitions == 0:
            print('Evaluation exercised balanced classification only; adaptive response was validated separately with injected feedback.')
    print('Exploratory per-run tail estimates. See raw samples, trial errors and tuning.json.')


if __name__ == '__main__':
    main()
