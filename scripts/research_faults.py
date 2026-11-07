"""Network faults scoped to an already-created ServiceLab."""
import json
import struct
import subprocess
import time
import adaptive_tc
from service_config import CLASSES, keys, number


def rate_args(rate, ceil):
    return ['htb', 'rate', f'{rate}kbit', 'ceil', f'{ceil}kbit']


def resize_htb(service, capacity, *, preserve_windows=False):
    """Rebase the managed controller on a scheduled capacity change."""
    number(capacity, 100, 1000000, True)
    lab, control = service.lab, service.controller
    old = service.profile['bandwidth_kbit']
    if capacity == old:
        return
    lab.assert_owned_sender()
    mode, strategy = control.mode, control.strategy
    control.stop('scheduled research capacity change')
    policy = service.config['policies'][service.active]
    expected = {name: (old*c['min_pct']//100, old*c['max_pct']//100)
                for name, c in policy['classes'].items()}
    if adaptive_tc.read(lab, old, policy) != expected:
        raise RuntimeError('research resize refused unexpected HTB baseline')

    def parent():
        lab.assert_owned_sender()
        lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:', 'classid', '1:1',
               *rate_args(capacity, capacity))
    if capacity > old:
        parent()
    desired = {}
    for name, c in policy['classes'].items():
        rate, ceil = capacity*c['min_pct']//100, capacity*c['max_pct']//100
        desired[name] = (rate, ceil)
        lab.assert_owned_sender()
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
    if not preserve_windows:
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
    from research_manifest import FAULTS, MODES, OPTIONAL_NETWORK_FIELDS, net_profile
    if not service.lab:
        raise ValueError('research requires an owned lab')
    if (not isinstance(settings, dict) or set(settings)-set((*FAULTS[:-1], *OPTIONAL_NETWORK_FIELDS))
            or not set(FAULTS[:-1]) <= set(settings)):
        raise ValueError('invalid research network settings')
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
        return update_conditions(service, lab, profile, mode, seed, link_up, settings)
    except (OSError, ValueError, RuntimeError, KeyError, struct.error, subprocess.SubprocessError) as error:
        # qdisc, class and netem updates are separate kernel operations. An
        # uncertain partial update invalidates the trial and its disposable lab.
        service.close_lab()
        service.error = f'research update failed; lab removed: {error}'
        raise RuntimeError(service.error) from error


def update_conditions(service, lab, profile, mode, seed, link_up, settings=None):
    from benchmark import TC
    from research_manifest import CLASSIFIED
    lab.assert_owned_sender()
    settings = settings or {}
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
        return dict(applied_ns=time.monotonic_ns(), kernel_confirmed_ns=time.monotonic_ns(),
                    capacity_kbit=None, netem=None, link_up=True)
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
    correlation = settings.get('loss_correlation_pct', 0)
    loss = ['loss', 'random', f"{profile['loss_pct']}%"]
    if correlation:
        loss.append(f'{correlation}%')
    reverse = settings.get('reverse_bandwidth_kbit')
    netem_command = [TC, 'qdisc','change','dev','fc1','root','netem','limit','1000']
    if reverse is not None:
        netem_command.extend(['rate',f'{reverse}kbit'])
    netem_command.extend(['delay', f"{profile['delay_ms']}ms", f"{profile['jitter_ms']}ms",
                          *loss, 'duplicate', f"{settings.get('duplicate_pct', 0)}%",
                          'reorder', f"{profile['reorder_pct']}%", 'seed', seed])
    lab.command(1, netem_command)
    observed = root(json.loads(lab.command(1, receiver)), 'netem')['options']
    delay = observed.get('delay', {})
    pairs = [(delay.get('delay',0),profile['delay_ms']/1000),
             (delay.get('jitter',0),profile['jitter_ms']/1000),
             (observed.get('loss-random',{}).get('loss',0),profile['loss_pct']/100),
             (observed.get('loss-random',{}).get('correlation',0),correlation/100),
             (observed.get('duplicate',{}).get('duplicate',0),settings.get('duplicate_pct',0)/100),
             (observed.get('reorder',{}).get('reorder',0),profile['reorder_pct']/100)]
    if reverse is not None:
        rate_options = observed.get('rate',0)
        actual_rate = rate_options.get('rate',0) if isinstance(rate_options,dict) else rate_options
        pairs.append((actual_rate,reverse*125))
    if observed.get('seed') != seed or any(abs(actual-wanted)>1e-6 for actual,wanted in pairs):
        raise RuntimeError(f'netem readback differs from requested conditions: {observed}')
    if link_up != service.research_link_up:
        lab.command(0, ['ip','link','set','dev','fc0','up' if link_up else 'down'])
    link = next(row for row in lab.network(0)['interfaces'] if row['name']=='fc0')
    if link_up and 'fd42:203::1' not in {item['address'] for item in link['addresses']}:
        # Linux may remove global IPv6 addresses when a device is brought down.
        # Recreate only the fixed address owned by this disposable veth.
        lab.command(0, ['ip','-6','addr','add','fd42:203::1/64','dev','fc0','nodad'])
        link = next(row for row in lab.network(0)['interfaces'] if row['name']=='fc0')
    addresses = {item['address'] for item in link['addresses']}
    if (link['up'] != link_up or link_up and
            not {'10.203.0.1','fd42:203::1'} <= addresses):
        raise RuntimeError('link state/address readback failed')
    service.profile = dict(profile, bandwidth_kbit=cap)
    service.research_link_up = link_up
    confirmed = time.monotonic_ns()
    result = dict(applied_ns=confirmed, kernel_confirmed_ns=confirmed, capacity_kbit=cap,
                  netem=observed, link_up=link_up)
    if reverse is not None:
        result['reverse_capacity_kbit'] = reverse
    return result

def reservation_step(service):
    """Single fixed 5-point intervention for a research counterfactual."""
    lab, control = service.lab, service.controller
    capacity, policy = control.capacity(), control.policy()
    allocation = {name: (capacity*cls['min_pct']//100, capacity*cls['max_pct']//100)
                  for name, cls in policy['classes'].items()}
    step = capacity*5//100
    critical, ceil = allocation['critical']
    background, background_ceil = allocation['background']
    if background-step < capacity*control.cfg['background_floor_pct']//100:
        raise ValueError('fixed step would violate the background floor')
    desired = dict(allocation, critical=(critical+step,ceil),
                   background=(background-step,background_ceil))
    timing = adaptive_tc.change(lab, capacity, policy, allocation, desired,
                                background_floor=capacity*control.cfg['background_floor_pct']//100)
    control.record(result='verified', reason='fixed research reservation step',
                   previous=allocation, allocation=desired, timing=timing)
    return timing
