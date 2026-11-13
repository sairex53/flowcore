#!/usr/bin/env python3
"""Reproducible, unprivileged estimator and forecast holdout evaluation."""
import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from demand_intelligence import NativeDemand

ROOT = Path(__file__).resolve().parents[1]
CAP = 12_000_000.0  # bit/s
MANIFEST = ROOT / 'scenarios' / 'demand-intelligence-v1.json'


def frame(name, target, **values):
    evidence = ['app_traffic', 'capacity', 'root_queue', 'configured_cap', 'delivered']
    # Readable fixture rates are bit/s; traffic counters at the native boundary are bytes.
    for field in ('app_offered_bytes', 'app_admitted_bytes', 'app_transmitted_bytes',
                  'app_received_bytes', 'delivered_bytes', 'class_served_bytes'):
        if field in values:
            values[field] /= 8
    row = dict(evidence=evidence, interval_seconds=1.0, capacity_bps=CAP,
               configured_shaping_bps=CAP, observed_bps=1_000_000.0,
               delivered_bytes=125_000.0, app_offered_bytes=125_000.0,
               app_admitted_bytes=125_000.0, app_transmitted_bytes=125_000.0,
               app_received_bytes=125_000.0, root_backlog_before_bytes=0,
               root_backlog_after_bytes=0, root_drops=0, class_backlog_before_bytes=0,
               class_backlog_after_bytes=0, class_drops=0,
               requests_attempted=100, requests_admitted=100, requests_completed=100,
               requests_failed=0, requests_rejected=0, deadline_misses=0,
               scenario=name, expected=target)
    row.update(values)
    if 'app_counts' not in row['evidence']:
        row['evidence'].append('app_counts')
    return row


def scenarios():
    return [
        frame('idle_critical_heavy_background', 'DEMAND_LIMITED', app_offered_bytes=2_000_000,
              app_admitted_bytes=2_000_000, app_transmitted_bytes=2_000_000,
              delivered_bytes=2_000_000, observed_bps=2_000_000),
        frame('heavy_background_transfer', 'LOCAL_CAPACITY_LIMITED', app_offered_bytes=16_000_000,
              app_admitted_bytes=16_000_000, app_transmitted_bytes=12_000_000,
              delivered_bytes=11_700_000, observed_bps=11_700_000, root_backlog_after_bytes=32_000,
              requests_attempted=1000, requests_admitted=1000, requests_completed=1000),
        frame('bursty_critical_before', 'DEMAND_LIMITED', app_offered_bytes=200_000,
              app_admitted_bytes=200_000, app_transmitted_bytes=200_000,
              delivered_bytes=200_000, observed_bps=200_000),
        frame('bursty_critical_after', 'LOCAL_CAPACITY_LIMITED', app_offered_bytes=20_000_000,
              app_admitted_bytes=20_000_000, app_transmitted_bytes=12_000_000,
              delivered_bytes=11_800_000, observed_bps=11_800_000, root_backlog_after_bytes=64_000),
        frame('receiver_limited_tcp', 'RECEIVER_LIMITED_SUSPECTED', app_offered_bytes=8_000_000,
              app_admitted_bytes=8_000_000, app_transmitted_bytes=2_000_000,
              delivered_bytes=2_000_000, observed_bps=2_000_000,
              evidence=['app_traffic','app_counts','delivered','root_queue','configured_cap','capacity',
                        'tcp','tcp_limits'], socket_notsent_bytes=8192,
              tcp_rwnd_limited_fraction=.82, tcp_sendbuf_limited_fraction=.02),
        frame('socket_pacing_bottleneck', 'SOCKET_PACING_RESTRICTED', app_offered_bytes=8_000_000,
              app_admitted_bytes=8_000_000, app_transmitted_bytes=1_000_000,
              delivered_bytes=950_000, observed_bps=950_000,
              evidence=['app_traffic','app_counts','delivered','root_queue','configured_cap','capacity',
                        'tcp','socket_pacing'], socket_pacing_bps=1_000_000,
              socket_send_queue_after_bytes=8192, socket_notsent_bytes=8192,
              socket_delivery_bps=950_000),
        frame('htb_class_bottleneck', 'CLASS_RESTRICTED', app_offered_bytes=8_000_000,
              app_admitted_bytes=8_000_000, app_transmitted_bytes=3_000_000,
              delivered_bytes=3_000_000, observed_bps=6_000_000,
              class_rate_bps=3_000_000, class_ceil_bps=3_000_000,
              class_served_bytes=3_000_000, class_backlog_after_bytes=24_000,
              evidence=['app_traffic','app_counts','delivered','root_queue','configured_cap',
                        'capacity','class','class_drops']),
        frame('external_emulated_bottleneck', 'REMOTE_BOTTLENECK_SUSPECTED', app_offered_bytes=8_000_000,
              app_admitted_bytes=8_000_000, app_transmitted_bytes=2_000_000,
              delivered_bytes=2_000_000, observed_bps=2_000_000,
              evidence=['app_traffic','app_counts','delivered','root_queue','configured_cap',
                        'capacity','remote_pressure']),
        frame('capacity_collapse_12_to_6_mbit', 'LOCAL_CAPACITY_LIMITED', capacity_bps=6_000_000,
              configured_shaping_bps=6_000_000, app_offered_bytes=10_000_000,
              app_admitted_bytes=10_000_000, app_transmitted_bytes=5_900_000,
              delivered_bytes=5_850_000, observed_bps=5_850_000, root_backlog_after_bytes=48_000),
        frame('capacity_recovery_6_to_12_mbit', 'DEMAND_LIMITED', app_offered_bytes=8_000_000,
              app_admitted_bytes=8_000_000, app_transmitted_bytes=8_000_000,
              delivered_bytes=8_000_000, observed_bps=8_000_000),
        frame('mixed_tcp_udp_demand', 'DEMAND_LIMITED', app_offered_bytes=5_000_000,
              app_admitted_bytes=5_000_000, app_transmitted_bytes=5_000_000,
              delivered_bytes=5_000_000, observed_bps=5_000_000,
              udp_messages_attempted=1200, udp_messages_transmitted=1198,
              udp_send_failures=2, udp_urgent_messages=100, udp_background_messages=1100,
              evidence=['app_traffic','app_counts','udp_accounting','delivered','root_queue',
                        'configured_cap','capacity']),
        frame('missing_demand_and_capacity_evidence', 'UNKNOWN', evidence=['root_queue'],
              capacity_bps=0, configured_shaping_bps=0),
        frame('tcp_sender_buffer_limited', 'UNKNOWN',
              evidence=['root_queue','capacity','configured_cap','tcp','tcp_limits'],
              tcp_sendbuf_limited_fraction=.8, socket_notsent_bytes=8192),
    ]


def source_digest():
    files = (ROOT / 'src' / 'demand.cpp', ROOT / 'include' / 'flowcore' / 'demand.hpp',
             ROOT / 'scripts' / 'demand_intelligence.py', Path(__file__), MANIFEST)
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def forecast_accuracy(model):
    series = ([100_000 + i * 50_000 for i in range(8)] +
              [500_000 + i * 25_000 for i in range(8)] +
              [900_000, 950_000, 1_000_000, 4_000_000, 1_000_000, 900_000, 850_000, 800_000] +
              [400_000] * 8)
    errors = {'trend': [], 'ema': [], 'last_value': []}
    for index in range(8, len(series) - 1):
        history = [dict(time_ns=(j + 1) * 1_000_000_000,
                        demand_bps=float(series[j]), capacity_bps=CAP)
                   for j in range(index - 8, index)]
        result = model.forecast(history, horizon_seconds=1.0, half_life_seconds=2.0)
        if result is None:
            continue
        actual = float(series[index])
        errors['trend'].append(abs(result['forecast_bps'] - actual))
        errors['ema'].append(abs(result['ema_bps'] - actual))
        errors['last_value'].append(abs(result['last_value_baseline_bps'] - actual))
    mae = {name: statistics.mean(rows) for name, rows in errors.items() if rows}
    return dict(samples=len(errors['trend']), mae_bps=mae,
                beats_last_value={name: value < mae['last_value'] for name, value in mae.items()
                                  if name != 'last_value'},
                series='synthetic ramp, burst, and recovery holdout')


def run(output=None, iterations=50_000):
    model = NativeDemand()
    rows = scenarios()
    results, confusion, times = [], {}, []
    policies = ('static', 'existing_queue_gate', 'demand_aware')
    action_counts = {name: 0 for name in policies}
    false_positive_actions = {name: 0 for name in policies}
    for row in rows:
        value = {key: item for key, item in row.items() if key not in ('scenario', 'expected')}
        start = time.perf_counter_ns()
        estimate = model.assess(value)
        elapsed = time.perf_counter_ns() - start
        times.append(elapsed)
        expected = row['expected']
        pair = confusion.setdefault(expected, {})
        pair[estimate['classification']] = pair.get(estimate['classification'], 0) + 1
        old_gate = (value.get('root_backlog_after_bytes', 0) >= 1500 and
                    value.get('observed_bps', 0) >= value.get('capacity_bps', float('inf')) * .85)
        demand_gate = (estimate['classification'] in ('LOCAL_CAPACITY_LIMITED', 'CLASS_RESTRICTED') and
                       estimate['confidence'] >= .75 and 'APPLICATION_REPORT_INCOMPLETE' not in estimate['flags'])
        for policy, active in (('static', False), ('existing_queue_gate', old_gate),
                               ('demand_aware', demand_gate)):
            action_counts[policy] += int(active)
            false_positive_actions[policy] += int(active and expected not in
                                                   ('LOCAL_CAPACITY_LIMITED', 'CLASS_RESTRICTED'))
        results.append(dict(scenario=row['scenario'], expected=expected,
                            estimated=estimate['classification'], confidence=estimate['confidence'],
                            offered_bps=estimate['offered_bps'], delivered_bps=estimate['delivered_bps'],
                            selected_demand_bps=estimate['selected_demand_bps'],
                            uncertainty_bps=estimate['demand_uncertainty_bps'],
                            unused_capacity_bps=estimate['unused_capacity_bps'],
                            reclaimable_capacity_bps=estimate['reclaimable_capacity_bps'],
                            flags=estimate['flags'], sample_cpu_ns=elapsed))

    iterations = max(100, min(iterations, 2_000_000))
    base = {key: item for key, item in rows[0].items() if key not in ('scenario', 'expected')}
    for _ in range(100):
        model.assess(base)
    start = time.process_time_ns()
    for _ in range(iterations):
        model.assess({key: item for key, item in rows[_ % len(rows)].items()
                      if key not in ('scenario', 'expected')})
    cpu_ns = time.process_time_ns() - start
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {}
    expected_names = {item['name'] for item in manifest.get('scenarios', [])}
    actual_names = {row['scenario'] for row in rows}
    if expected_names != actual_names:
        raise RuntimeError('scenario manifest does not match fixture implementation')
    report = dict(schema_version=1, status='COMPLETED',
                  evidence_kind='deterministic synthetic estimator fixtures',
                  generated_at=datetime.now(timezone.utc).isoformat(), source_commit=commit,
                  working_tree_digest=source_digest(),
                  environment=dict(architecture=platform.machine(), kernel=platform.release(),
                                   python=platform.python_version()),
                  thresholds=dict(queue_bytes=1500, utilization=.85,
                                  receiver_window_fraction=.65, socket_cap_utilization=.75,
                                  confidence_control_gate=.75),
                  scenario_manifest=MANIFEST.relative_to(ROOT).as_posix(),
                  scenarios=results, confusion=confusion,
                  controller_shadow_comparison=dict(action_counts=action_counts,
                      false_positive_actions=false_positive_actions,
                      note='fixture-only decision comparison; no kernel policy writes or network performance'),
                  estimator_cost=dict(iterations=iterations, cpu_seconds=cpu_ns/1e9,
                      cpu_ns_per_assessment=cpu_ns/iterations,
                      median_single_assessment_ns=statistics.median(times)),
                  forecasting=forecast_accuracy(model),
                  limitations=['Application, socket, capacity and queue counters are synthetic fixtures.',
                                'No real packet loss, RTT, P99, goodput, fairness or policy churn was measured.',
                                'Forecasts and demand-aware control remain shadow-only pending paired namespace trials.'])
    path = Path(output) if output else ROOT / 'results' / 'demand-intelligence' / f'run-{time.time_ns()}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--iterations', type=int, default=50_000)
    args = parser.parse_args()
    report, path = run(args.output, args.iterations)
    print(json.dumps(dict(status=report['status'], artifact=str(path),
                          scenarios=len(report['scenarios']), forecasting=report['forecasting'],
                          estimator_cost=report['estimator_cost']), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
