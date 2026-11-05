"""Freeze a static reservation using a separately seeded pilot."""
import hashlib
import json
import statistics

from research_manifest import load_manifest
from research_stats import load_summary


def tune(pilot_path, evaluation_path, output):
    pilot, evaluation = load_summary(pilot_path), load_manifest(evaluation_path)
    manifest = pilot['manifest']
    modes = {'static20', 'static30', 'static40'}
    if (manifest['version'] != 2 or evaluation['version'] != 2 or set(manifest['modes']) != modes
            or manifest['repetitions'] < 2 or manifest['seed'] == evaluation['seed']
            or evaluation['gates']['baseline'] != 'static' or pilot.get('status') != 'complete'
            or any(manifest[key] != evaluation[key] for key in
                   ('topology','network','workload','duration_seconds','faults','controller'))):
        raise ValueError('pilot must use separate seeds and the same workload and network')
    selection = []
    for mode in sorted(modes):
        rows = [row for row in pilot['runs'] if row['mode'] == mode and row['valid']]
        if len(rows) != manifest['repetitions'] or any(row['failed'] for row in rows):
            continue
        background = statistics.median(row['background_mbps'] for row in rows)
        if background < .5*manifest['network']['bandwidth_kbit']/1000:
            continue
        selection.append(dict(mode=mode, background_mbps=background,
            deadline_attainment=statistics.median(row['deadline_attainment'] for row in rows),
            p99_ms=statistics.median(row['p99_ms'] for row in rows)))
    if not selection:
        raise ValueError('no static pilot configuration met the predeclared safety gates')
    winner = min(selection, key=lambda row: (-row['deadline_attainment'], row['p99_ms'], row['mode']))
    evaluation['static_critical_pct'] = int(winner['mode'].removeprefix('static'))
    with output.open('x') as stream:
        json.dump(evaluation, stream, indent=2)
        stream.write('\n')
    return dict(manifest=str(output), static_critical_pct=evaluation['static_critical_pct'],
                pilot_sha256=hashlib.sha256(pilot_path.read_bytes()).hexdigest(), eligible=selection)
