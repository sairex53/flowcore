"""Prometheus text and bounded connection CSV exports."""
import csv
import json


def prometheus(view, deployment=None):
    samples = {}

    def put(name, value, **labels):
        if value is not None:
            samples.setdefault(name, []).append((labels, value))

    put('observer_daemon_cpu_seconds_total', view['overhead']['daemon_cpu_seconds'])
    put('observer_daemon_resident_memory_bytes', view['overhead']['daemon_rss_bytes'])
    put('observer_bpf_active', int(view['bpf']['active']))
    put('observer_bpf_kernel_dropped_events', view['bpf']['kernel_reserve_drops'])
    put('observer_bpf_transport_dropped_events', view['bpf']['transport_drops'])
    put('tcp_socket_snapshot_entries', view['socket_count'])
    for seconds, window in view['windows'].items():
        label = dict(window_seconds=seconds)
        for percentile in (50, 95, 99):
            key = f'p{percentile}_us'
            value = window['rtt'][key]
            put('tcp_srtt_seconds', value / 1e6 if value is not None else None,
                quantile=percentile / 100, **label)
            for service, sources in window['application'].items():
                value = sources['application'][key]
                put('application_request_duration_seconds', value / 1e6 if value is not None else None,
                    service=service, traffic_class=sources['traffic_class'], quantile=percentile / 100, **label)
        for service, sources in window['application'].items():
            app = sources['application']
            for outcome in ('ok', 'failure', 'timeout'):
                put('application_requests', app[outcome], service=service, outcome=outcome, **label)
            put('application_error_ratio', app['error_ratio'], service=service, **label)
        for queue, stats in window['queue'].items():
            put('queue_throughput_bytes_per_second', stats['bytes_per_second'], queue=queue, **label)
            put('queue_shaper_utilization_ratio', stats['utilization'], queue=queue, **label)
        put('tcp_retransmit_ratio', window['retransmit_ratio'], **label)
        put('qdisc_backlog_bytes', window['backlog']['p95_bytes'], quantile=.95, **label)
    if deployment:
        status = deployment.get('state',{})
        control = deployment.get('controller',{})
        put('daemon_healthy',int(status.get('healthy',False)))
        put('policy_revision',deployment.get('policy_revision'))
        put('controller_probe_pending',int(bool(control.get('pending'))))
        put('controller_reversals',control.get('reversals'))
        put('controller_actions_session',control.get('actions'))
        for row in deployment.get('platform',{}).get('interfaces',[]):
            label = dict(interface=row['name'])
            put('interface_available',int(row['available']),**label)
            put('interface_tx_mbps',row['path_quality']['tx_mbps'],**label)
            put('interface_rx_mbps',row['path_quality']['rx_mbps'],**label)
            put('interface_root_backlog_bytes',(row.get('queue') or {}).get('backlog_bytes'),**label)
        for name, state in view.get('findings',{}).items():
            put('observation_active',int(state['active']),finding=name)
    for name, rows in samples.items():
        metric = 'flowcore_' + name
        kind = 'counter' if metric.endswith('_total') else 'gauge'
        yield f'# TYPE {metric} {kind}'
        for labels, value in rows:
            keys = ','.join(f'{key}={json.dumps(str(val))}' for key, val in sorted(labels.items()))
            suffix = '{' + keys + '}' if keys else ''
            yield f'{metric}{suffix} {value}'


def connection_csv(rows, stream):
    fields = ('namespace', 'cookie', 'local_address', 'local_port', 'remote_address', 'remote_port',
              'state', 'observed_age_ms', 'rtt_us', 'rttvar_us', 'cwnd_segments',
              'bytes_acked', 'total_retrans_segments')
    writer = csv.DictWriter(stream, fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, **row['tcp_info']})
