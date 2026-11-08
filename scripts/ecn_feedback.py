"""Fuse scoped AQM and TCP evidence without turning missing counters into zeroes."""
from collections import Counter


def delta(old, new, key):
    before, after = old.get(key), new.get(key)
    return after-before if type(before) is int and type(after) is int and 0 <= before <= after else None


def mode(row):
    explicit = row.get('ecn_mode')
    if explicit is not None:
        return explicit
    negotiated = row.get('ecn_negotiated')
    return 'classic-or-accurate' if negotiated is True else 'disabled' if negotiated is False else 'unknown'


def aqm_counters(qdisc):
    """Keep the fq_codel threshold and CoDel mark counters distinct."""
    if qdisc.get('kind') == 'fq_codel':
        return {name: qdisc.get(key) for name, key in (
            ('marks', 'ecn_mark'), ('threshold_marks', 'ce_mark'),
            ('drops', 'drops'), ('packets', 'packets'), ('backlog', 'backlog'))}
    if qdisc.get('kind') == 'cake':
        tins = qdisc.get('tins', [])
        marks = [row.get('ecn_mark') for row in tins]
        return dict(marks=sum(marks) if marks and all(type(n) is int for n in marks) else None,
                    threshold_marks=None, drops=qdisc.get('drops'), packets=qdisc.get('packets'),
                    backlog=qdisc.get('backlog'))
    return {name: None for name in ('marks', 'threshold_marks', 'drops', 'packets', 'backlog')}


def pairs(old, new):
    """Only comparable sender observations. Dict keys identify socket lifetimes."""
    counts = Counter()
    for key in old.keys() & new.keys():
        before, after = old[key], new[key]
        if before.get('ecn_negotiated') is not True or after.get('ecn_negotiated') is not True:
            continue
        sent, ce = (delta(before, after, k) for k in ('delivered_segments', 'delivered_ce_segments'))
        retrans = delta(before, after, 'total_retransmits')
        if None in (sent, ce, retrans) or ce > sent:
            continue
        counts.update(paired=1, delivered=sent, delivered_ce=ce, retransmits=retrans)
        rtt = after.get('rtt_us')
        prior_rtt = before.get('rtt_us')
        if type(rtt) is int and type(prior_rtt) is int and rtt > 0 and prior_rtt > 0:
            counts.update(rtt_trend_samples=1, rtt_change_us=rtt-prior_rtt)
    return dict(counts)


def estimate(*, previous, current, before_aqm, after_aqm, fresh, capacity, delivery_rate,
             rtt_us, p99_us, requests, elapsed_ns, scope_matches=True):
    transport = pairs(previous, current) if fresh else {}
    negotiated = Counter(mode(row) for row in current.values())
    marks = delta(before_aqm, after_aqm, 'marks') if fresh else None
    threshold = delta(before_aqm, after_aqm, 'threshold_marks') if fresh else None
    drops = delta(before_aqm, after_aqm, 'drops') if fresh else None
    total_marks = None if marks is None and threshold is None else (marks or 0)+(threshold or 0)
    sent = transport.get('delivered', 0)
    fraction = transport['delivered_ce']/sent if sent else None
    backlog = after_aqm.get('backlog')
    queue_ms = backlog*1000/capacity if type(backlog) is int and capacity else None
    trend_samples = transport.get('rtt_trend_samples', 0)
    paired = transport.get('paired', 0)
    # A 32-segment minimum and at least two acknowledged CE signals reduce
    # sensitivity to a lone mark. The high threshold prevents escalation under a storm.
    aligned = fresh and 0 < elapsed_ns <= 2_500_000_000
    evidence = aligned and scope_matches and sent >= 32 and drops is not None and total_marks is not None
    loss = aligned and ((drops or 0) > 0 or transport.get('retransmits', 0) > 0)
    if loss:
        classification, reason = 'loss_present', 'AQM drops or sender retransmissions in the observation'
    elif not scope_matches:
        classification, reason = 'unscoped', 'AQM and transport counters have different traffic scopes'
    elif not evidence:
        classification, reason = 'unknown', 'fresh AQM and paired ECN sender counters required'
    elif fraction >= .30:
        classification, reason = 'mark_saturated', 'CE feedback covers at least 30% of delivered segments'
    elif total_marks and transport['delivered_ce'] >= 2 and queue_ms is not None and queue_ms >= 2:
        classification, reason = 'pre_loss_pressure', 'same-scope AQM marks, sender CE feedback and backlog'
    elif total_marks or transport['delivered_ce']:
        classification, reason = 'mark_only', 'marking without sufficiently corroborated queue pressure'
    else:
        classification, reason = 'no_mark_evidence', 'paired observation without corroborated CE'
    return dict(classification=classification, reason=reason, early=classification == 'pre_loss_pressure',
                quality='paired' if evidence else 'direct_loss' if loss else 'unscoped' if not scope_matches else 'insufficient', negotiated=dict(negotiated),
                paired_sockets=paired, delivered_segments=sent if paired else None,
                delivered_ce_segments=transport.get('delivered_ce'), ce_fraction=fraction,
                retransmits=transport.get('retransmits'), aqm_marks=marks, threshold_marks=threshold, aqm_drops=drops,
                queue_ms_estimate=queue_ms, rtt_us=rtt_us,
                rtt_trend_us=transport['rtt_change_us']/trend_samples if trend_samples else None,
                background_acked_bytes_per_second=delivery_rate, application_p99_us=p99_us if requests else None,
                application_samples=requests)
