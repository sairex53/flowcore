"""Exact HTB/flower scope projection for the socket planner."""
from service_config import CLASSES


def feedback(tc, configured, capacity_kbit, initial_capacity_kbit, *, generation, sampled_ns, healthy):
    expected = {f'1:{CLASSES[name]}': (capacity_kbit*entry['min_pct']//100*125,
                                       capacity_kbit*entry['max_pct']//100*125)
                for name, entry in configured.items()}
    expected['1:1'] = (capacity_kbit*125, capacity_kbit*125)
    actual = {row['handle']: (row.get('rate'), row.get('ceil'))
              for row in tc.get('class', []) if row.get('class') == 'htb'}
    qdisc = tc.get('qdisc', [])
    root = [(q.get('kind'), q.get('handle')) for q in qdisc if q.get('root')]
    leaves = {q.get('parent'): q.get('kind') for q in qdisc if 'parent' in q}
    expected_leaves = {f'1:{minor}': 'fq_codel' for minor in CLASSES.values()}
    background = next((q for q in qdisc if q.get('parent') == '1:20'), {})
    expected_rules = {(protocol, 2*CLASSES[name]+(protocol == 'ipv6'), f'1:{CLASSES[name]}',
                       'ipv6' if protocol == 'ipv6' else 'ipv4', entry['port'])
                      for name, entry in configured.items() for protocol in ('ip', 'ipv6')}
    headers, rules = set(), set()
    valid = bool(healthy and capacity_kbit and actual == expected and root == [('htb', '1:')]
                 and leaves == expected_leaves and background.get('options', {}).get('ecn'))
    if valid:
        for row in tc.get('filter', []):
            if row.get('kind') != 'flower' or row.get('parent') != '1:' or row.get('chain') != 0:
                valid = False
                break
            key = (row.get('protocol'), row.get('pref'))
            if 'options' not in row:
                headers.add(key)
                continue
            opts = row['options']
            fields = opts.get('keys', {})
            if set(fields) != {'eth_type', 'ip_proto', 'dst_port'} or fields['ip_proto'] != 'tcp' or opts.get('actions'):
                valid = False
                break
            rules.add((*key, opts.get('classid'), fields['eth_type'], fields['dst_port']))
        valid = (valid and rules == expected_rules and headers == {r[:2] for r in expected_rules}
                 and len(tc['filter']) == 2*len(expected_rules))

    return dict(sample_ns=sampled_ns, generation=generation, verified=valid,
                background_guarantee=initial_capacity_kbit*configured['background']['min_pct']//100*125)
