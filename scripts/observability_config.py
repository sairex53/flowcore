"""Validated bounds and fixed labels for the observability engine."""
from service_config import CLASSES, keys, number

DEFAULT = dict(enabled=True, bpf='auto', windows=[1, 10, 60], max_sockets=512,
               services={'demo': {'class': 'critical', 'slo_ms': 15}},
               thresholds={'rtt_us': 20000, 'retransmit_ratio': .02, 'backlog_bytes': 32768,
                           'drops_per_second': 10, 'min_tx_bytes_per_second': 0,
                           'failures_per_second': 3, 'min_samples': 5,
                           'window_seconds': 10, 'enter_windows': 2, 'exit_windows': 3})


def validate(cfg):
    keys(cfg, DEFAULT)
    if type(cfg['enabled']) is not bool or cfg['bpf'] not in ('auto', 'off'):
        raise ValueError('invalid observability backend')
    windows = cfg['windows']
    if not isinstance(windows, list) or not 1 <= len(windows) <= 8:
        raise ValueError('observability needs 1–8 windows')
    for window in windows:
        number(window, 1, 300, True)
    if len(set(windows)) != len(windows):
        raise ValueError('duplicate observation window')
    number(cfg['max_sockets'], 16, 1024, True)
    services = cfg['services']
    if not isinstance(services, dict) or not 1 <= len(services) <= 16:
        raise ValueError('1–16 registered services required')
    for name, service in services.items():
        if not name.isascii() or not name.replace('_', '').replace('-', '').isalnum() or len(name) > 32:
            raise ValueError('invalid telemetry service label')
        keys(service, ('class', 'slo_ms'))
        if service['class'] not in CLASSES:
            raise ValueError('invalid telemetry traffic class')
        number(service['slo_ms'], .001, 600000)
    thresholds = cfg['thresholds']
    keys(thresholds, DEFAULT['thresholds'])
    for key, value in thresholds.items():
        number(value, 0, 10**12)
    for key in ('min_samples', 'enter_windows', 'exit_windows'):
        number(thresholds[key], 1, 10000, True)
    number(thresholds['window_seconds'], 1, 300, True)
    number(thresholds['retransmit_ratio'], 0, 1)
    return cfg
