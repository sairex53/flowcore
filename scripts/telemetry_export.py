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
    put('observer_bpf_decode_dropped_events',view['bpf'].get('kernel_decode_drops'))
    put('tcp_socket_snapshot_entries', view['socket_count'])
    put('observer_snapshot_truncated',int(view['truncated']))
    put('observer_lifecycle_retained',view.get('lifecycle',{}).get('retained'))
    put('observer_lifecycle_evictions',view.get('lifecycle',{}).get('evicted'))
    put('observer_lifecycle_out_of_order',view.get('lifecycle',{}).get('out_of_order'))
    put('observer_parse_dropped_records',view['bpf']['parse_drops'])
    put('observer_records_per_second',view['overhead']['records_per_second'])
    put('observer_sample_age_seconds',view['sample_age_seconds'])
    for seconds, window in view['windows'].items():
        label = dict(window_seconds=seconds)
        for key,value in window.get('tcp_counter_deltas',{}).items():
            put('tcp_window_'+key,value,**label)
        for name,distribution in window.get('tcp_distributions',{}).items():
            for percentile in (50,95,99):
                unit = ('us' if name.endswith('_us') else 'segments' if name.endswith('_segments')
                        else 'bytes_per_second' if name.endswith('_bytes_per_second') else 'bytes')
                value = distribution[f'p{percentile}_{unit}']
                metric = name.removesuffix('_us')+'_seconds' if unit=='us' else name
                put('tcp_'+metric,value/1e6 if unit=='us' and value is not None else value,
                    quantile=percentile/100,**label)
        for service,sources in window['application'].items():
            app = sources['application']
            total = app['ok']+app['failure']+app['timeout']
            put('application_deadline_misses',app['deadline_miss'],service=service,**label)
            put('application_completion_ratio',app['ok']/total if total else None,service=service,**label)
        for name,stats in window['queue'].items():
            put('queue_drop_rate_per_second',stats['drops_per_second'],queue=name,**label)
            put('queue_backlogged_samples',stats.get('backlogged_samples'),queue=name,**label)
            put('queue_ecn_marks_window',stats.get('ecn_marks'),queue=name,**label)
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
        put('controller_engine_state',1,state=control.get('engine_state','DISABLED'))
        for row in deployment.get('platform',{}).get('interfaces',[]):
            for name,value in row['counters'].items():
                put('interface_'+name+'_total',value,interface=row['name'])
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
        yield f'# HELP {metric} FLOWCORE observed '+name.replace('_',' ')+'.'
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
        writer.writerow({**row, **row.get('tcp_info',{})})
