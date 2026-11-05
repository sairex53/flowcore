"""Network faults scoped to an already-created ServiceLab."""
import json
import struct
import subprocess
import time
import adaptive_tc
from service_config import CLASSES, keys, number


def rate_args(rate, ceil):
    return ['htb', 'rate', f'{rate}kbit', 'ceil', f'{ceil}kbit']


def resize_htb(service, capacity):
    """Rebase the managed controller on a scheduled capacity change."""
    number(capacity, 100, 1000000, True)
    lab, control = service.lab, service.controller
    old = service.profile['bandwidth_kbit']
    if capacity == old:
        return
    mode, strategy = control.mode, control.strategy
    control.stop('scheduled research capacity change')
    policy = service.config['policies'][service.active]
    expected = {name: (old*c['min_pct']//100, old*c['max_pct']//100)
                for name, c in policy['classes'].items()}
    if adaptive_tc.read(lab, old, policy) != expected:
        raise RuntimeError('research resize refused unexpected HTB baseline')

    def parent():
        lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:', 'classid', '1:1',
               *rate_args(capacity, capacity))
    if capacity > old:
        parent()
    desired = {}
    for name, c in policy['classes'].items():
        rate, ceil = capacity*c['min_pct']//100, capacity*c['max_pct']//100
        desired[name] = (rate, ceil)
        lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:1', 'classid', f'1:{CLASSES[name]}',
               *rate_args(rate, ceil), 'prio', c['priority'], 'quantum', '1514')
    if capacity < old:
        parent()
    if adaptive_tc.read(lab, capacity, policy) != desired:
        raise RuntimeError('research resize readback failed')
    service.profile = dict(service.profile, bandwidth_kbit=capacity)
    # Counter deltas and feedback from the previous capacity must not arm probes.
    observer = service.observer
    observer.previous = {}
    observer.queue_time = None
    observer.windows.buckets.clear()
    observer.last_detect = None
    observer.detectors.clear()
    if mode != 'off':
        control.start(mode, strategy)
    control.record(result='research_rebase', previous_capacity_kbit=old, capacity_kbit=capacity,
                   previous_mode=mode, allocation=desired)


def root(rows, kind):
    value = next((row for row in rows if row.get('root')), {})
    if value.get('kind') != kind:
        raise RuntimeError(f'owned {kind} root missing')
    return value


def apply_conditions(service, settings, mode, seed, link_up):
    """Operate only on interfaces owned by the daemon's ephemeral veth lab."""
    from research_manifest import FAULTS, MODES, net_profile
    if not service.lab:
        raise ValueError('research requires an owned lab')
    keys(settings, FAULTS[:-1])
    if mode not in MODES:
        raise ValueError('unknown research mode')
    profile = net_profile(settings)
    number(seed, 1, 2**32-1, True)
    if type(link_up) is not bool:
        raise ValueError('link_up must be boolean')
    lab = service.lab
    if not lab.namespace_fds:
        raise RuntimeError('research namespace handles are not pinned')
    if service.research_mode is not None and mode != service.research_mode:
        raise ValueError('research mode cannot change within a trial')
    try:
        return update_conditions(service, lab, profile, mode, seed, link_up)
    except (OSError, ValueError, RuntimeError, KeyError, struct.error, subprocess.SubprocessError) as error:
        # qdisc, class and netem updates are separate kernel operations. An
        # uncertain partial update invalidates the trial and its disposable lab.
        service.close_lab()
        service.error = f'research update failed; lab removed: {error}'
        raise RuntimeError(service.error) from error


def update_conditions(service, lab, profile, mode, seed, link_up):
    from benchmark import TC
    from research_manifest import CLASSIFIED
    cap = profile['bandwidth_kbit']
    if service.research_mode is None:
        if mode in ('linux', 'linux-default', 'fifo', 'fq_codel'):
            installed = service.config['policies'][service.active]
            if adaptive_tc.read(lab, service.profile['bandwidth_kbit'], installed) != {
                    name: (service.profile['bandwidth_kbit']*cls['min_pct']//100,
                           service.profile['bandwidth_kbit']*cls['max_pct']//100)
                    for name,cls in installed['classes'].items()}:
                raise RuntimeError('refusing to replace unexpected baseline qdisc')
            lab.tc('qdisc','del','dev','fc0','root')
            if mode == 'linux-default':
                lab.command(1, [TC, 'qdisc', 'del', 'dev', 'fc1', 'root'])
            else:
                lab.tc('qdisc','add','dev','fc0','root','handle','1:', 'htb','default','20')
                lab.tc('class','add','dev','fc0','parent','1:', 'classid','1:20', *rate_args(cap, cap))
                if mode != 'linux':
                    lab.tc('qdisc','add','dev','fc0','parent','1:20',
                           'pfifo' if mode=='fifo' else 'fq_codel','limit','1000')
        service.research_mode = mode
    qdiscs = json.loads(lab.tc('-j', 'qdisc', 'show', 'dev', 'fc0'))
    if mode == 'linux-default':
        receiver = json.loads(lab.command(1, [TC, '-j', 'qdisc', 'show', 'dev', 'fc1']))
        for rows in (qdiscs, receiver):
            root(rows, 'noqueue')
        return dict(applied_ns=time.monotonic_ns(), capacity_kbit=None, netem=None, link_up=True)
    if mode == 'cake':
        root(qdiscs, 'cake')
        if cap != service.profile['bandwidth_kbit']:
            lab.tc('qdisc', 'change', 'dev', 'fc0', 'root', 'handle', '1:', 'cake',
                   'bandwidth', f'{cap}kbit', 'besteffort', 'flows', 'rtt', '100ms')
        if root(json.loads(lab.tc('-j', 'qdisc', 'show', 'dev', 'fc0')), 'cake')['options']['bandwidth'] != cap*125:
            raise RuntimeError('CAKE capacity readback failed')
    elif mode in ('linux', 'fifo', 'fq_codel'):
        root(qdiscs, 'htb')
        classes = lambda: [row for row in json.loads(lab.tc('-j','class','show','dev','fc0'))
                           if row.get('class') == 'htb']
        rows = classes()
        if len(rows) != 1 or rows[0]['handle'] != '1:20':
            raise RuntimeError('unexpected baseline HTB topology')
        if cap != service.profile['bandwidth_kbit']:
            lab.tc('class','change','dev','fc0','parent','1:', 'classid','1:20', *rate_args(cap, cap))
        if classes()[0]['rate'] != cap*125 or classes()[0]['ceil'] != cap*125:
            raise RuntimeError('baseline capacity readback failed')
    elif mode in CLASSIFIED:
        root(qdiscs, 'htb')
        resize_htb(service, cap)
    else:
        raise ValueError('unknown research mode')
    receiver = [TC, '-j', 'qdisc', 'show', 'dev', 'fc1']
    root(json.loads(lab.command(1, receiver)), 'netem')
    lab.command(1, [TC, 'qdisc','change','dev','fc1','root','netem','limit','1000',
                    'delay', f"{profile['delay_ms']}ms", f"{profile['jitter_ms']}ms",
                    'loss', f"{profile['loss_pct']}%",
                    'reorder', f"{profile['reorder_pct']}%", 'seed', seed])
    observed = root(json.loads(lab.command(1, receiver)), 'netem')['options']
    delay = observed.get('delay', {})
    pairs = [(delay.get('delay',0),profile['delay_ms']/1000),
             (delay.get('jitter',0),profile['jitter_ms']/1000),
             (observed.get('loss-random',{}).get('loss',0),profile['loss_pct']/100),
             (observed.get('reorder',{}).get('reorder',0),profile['reorder_pct']/100)]
    if observed.get('seed') != seed or any(abs(actual-wanted)>1e-6 for actual,wanted in pairs):
        raise RuntimeError('netem readback differs from requested conditions')
    if link_up != service.research_link_up:
        lab.command(0, ['ip','link','set','dev','fc0','up' if link_up else 'down'])
    link = next(row for row in lab.network(0)['interfaces'] if row['name']=='fc0')
    if link['up'] != link_up:
        raise RuntimeError('link state readback failed')
    service.profile = profile
    service.research_link_up = link_up
    return dict(applied_ns=time.monotonic_ns(), capacity_kbit=cap, netem=observed, link_up=link_up)
