"""Strict ECN experiment profile and capability checks for disposable namespaces."""
import json
from pathlib import Path

from paths import TC
from service_config import CLASSES, keys, number

MODES = ('ecn-loss', 'ecn-static', 'ecn-dctcp', 'ecn-queue', 'ecn-aware', 'ecn-pacing-control', 'ecn-cake', 'integrated')
CLASSIFIED = tuple(mode for mode in MODES if mode != 'ecn-cake')
DEFAULT = dict(version=1, target_us=5000, interval_us=100000, threshold_us=0, dctcp_threshold_us=1000)
SYSCTL = '/usr/sbin/sysctl'


def validate(profile):
    optional = ('tcp_ecn',) if isinstance(profile, dict) and 'tcp_ecn' in profile else ()
    keys(profile, (*DEFAULT, *optional))
    ecn = profile.get('tcp_ecn', 1)
    if type(ecn) is not int or ecn not in (1, 3):
        raise ValueError('tcp_ecn must request Classic ECN (1) or Accurate ECN (3)')
    number(profile['version'], 1, 1, True)
    number(profile['target_us'], 1000, 20000, True)
    number(profile['interval_us'], 50000, 250000, True)
    number(profile['threshold_us'], 0, 20000, True)
    number(profile['dctcp_threshold_us'], 500, 20000, True)
    if profile['threshold_us'] and profile['dctcp_threshold_us'] >= profile['threshold_us']:
        raise ValueError('DCTCP requires a shallower separate marking threshold')
    return dict(profile, tcp_ecn=ecn)


def local():
    names = ('tcp_ecn', 'tcp_ecn_option', 'tcp_ecn_option_beacon', 'tcp_ecn_fallback',
             'tcp_congestion_control', 'tcp_available_congestion_control', 'tcp_allowed_congestion_control')
    values = {}
    for name in names:
        try:
            values[name] = (Path('/proc/sys/net/ipv4')/name).read_text().strip()
        except OSError:
            values[name] = None
    return values


def endpoint(lab, index, ecn, cc):
    if ecn not in (0, 1, 3) or cc not in ('cubic', 'reno', 'dctcp'):
        raise ValueError('unsupported ECN mode or TCP algorithm')
    available = lab.command(index, [SYSCTL, '-n', 'net.ipv4.tcp_available_congestion_control']).split()
    if cc not in available:
        raise ValueError(f'{cc} is not registered; load or enable it explicitly before the lab')
    # A restricted algorithm can be selected on owned sockets without
    # modifying the global unprivileged congestion-control allowlist.
    default = 'cubic' if cc == 'dctcp' else cc
    for key, value in (('tcp_ecn', ecn), ('tcp_congestion_control', default)):
        lab.command(index, [SYSCTL, '-w', f'net.ipv4.{key}={value}'])
        if lab.command(index, [SYSCTL, '-n', f'net.ipv4.{key}']).strip() != str(value):
            raise RuntimeError('namespace TCP sysctl readback failed')
    return dict(tcp_ecn=ecn, default=default, congestion=cc, selection='socket' if cc == 'dctcp' else 'namespace')


def leaf_args(profile, mode):
    threshold = profile['dctcp_threshold_us' if mode == 'ecn-dctcp' else 'threshold_us']
    opts = ['fq_codel', 'limit', '256', 'target', f"{profile['target_us']}us",
            'interval', f"{profile['interval_us']}us", 'memory_limit', '1mb']
    if mode == 'ecn-loss':
        return [*opts, 'noecn']
    return [*opts, 'ecn', *(['ce_threshold', f'{threshold}us'] if threshold else [])]


def configure(lab, profile, mode):
    """Caller supplies the existing pinned ServiceLab after validating its HTB tree."""
    lab.assert_owned_sender()
    ecn, cc = (0 if mode == 'ecn-loss' else 1 if mode == 'ecn-dctcp' else profile.get('tcp_ecn', 1)), ('dctcp' if mode == 'ecn-dctcp' else 'cubic')
    before = json.loads(lab.tc('-j', 'qdisc', 'show', 'dev', 'fc0'))
    root = next((row for row in before if row.get('root')), {})
    if mode == 'ecn-cake':
        if root.get('kind') != 'cake' or root.get('handle') != '1:':
            raise RuntimeError('unexpected CAKE root before ECN setup')
    elif (root.get('kind') != 'htb' or root.get('handle') != '1:'
          or {row['parent']: row['kind'] for row in before if row.get('parent')}
              != {f'1:{minor}': 'fq_codel' for minor in CLASSES.values()}):
        raise RuntimeError('refusing to change unexpected ECN queue tree')
    endpoints = [endpoint(lab, index, ecn, cc) for index in range(2)]
    if mode != 'ecn-cake':
        for minor in CLASSES.values():
            lab.assert_owned_sender()
            lab.tc('qdisc', 'change', 'dev', 'fc0', 'parent', f'1:{minor}', *leaf_args(profile, mode))
    rows = json.loads(lab.tc('-j', '-s', 'qdisc', 'show', 'dev', 'fc0'))
    verify(rows, profile, mode)
    return endpoints


def verify(rows, profile, mode):
    if mode == 'ecn-cake':
        root = next((r for r in rows if r.get('root')), {})
        if root.get('kind') != 'cake':
            raise RuntimeError('expected CAKE ECN lab root')
        return
    leaves = {row['parent']: row for row in rows if row.get('parent')}
    if set(leaves) != {f'1:{minor}' for minor in CLASSES.values()}:
        raise RuntimeError('unexpected ECN lab class topology')
    for row in leaves.values():
        options = row.get('options', {})
        threshold = profile['dctcp_threshold_us' if mode == 'ecn-dctcp' else 'threshold_us']
        actual = options.get('ce_threshold')
        # tc converts through kernel time units, so allow sub-2 us rounding.
        if (row.get('kind') != 'fq_codel' or options.get('limit') != 256
                or bool(options.get('ecn')) != (mode != 'ecn-loss')
                or abs(options.get('target', 0)-profile['target_us']) > 2
                or abs(options.get('interval', 0)-profile['interval_us']) > 2
                or ((mode == 'ecn-loss' or not threshold) and actual is not None)
                or (mode != 'ecn-loss' and threshold and (actual is None or abs(actual-threshold) > 2))):
            raise RuntimeError(f'ECN AQM readback mismatch: {options}')
