"""Unprivileged tests for admission, hierarchy verification and paired accounting."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from scheduler_experiment import CAPACITY, DEADLINE_NS, MODES, offer, paired_summary, urgent, verify


def hierarchy():
    qdisc = [dict(root=True, kind='htb', options={'default': '0x20'})]
    qdisc += [dict(parent=f'1:{leaf}', kind='fq_codel', options=dict(limit=256, memory_limit=1024*1024))
              for leaf in (10, 20)]
    classes = [dict(handle=f'1:{leaf}', **{'class': 'htb'}, rate=rate*125, ceil=ceil*125, prio=prio)
               for leaf, rate, ceil, prio in [(1, 12000, 12000, 0), (10, 3600, 7200, 0), (20, 8400, 12000, 1)]]
    return {'qdisc': qdisc, 'class': classes,
            'filter': 'filter protocol ip basic flowid 1:10\n  meta(nf_mark eq 42)\n  AND meta(priority eq 6)\n'}


class SchedulerFixture(unittest.TestCase):
    def test_deadline_validation(self):
        now = 20_000_000
        self.assertTrue(urgent(now+1, now))
        self.assertTrue(urgent(now+DEADLINE_NS, now))
        self.assertFalse(urgent(now+DEADLINE_NS+1, now))
        self.assertFalse(urgent(now+100_000_000, now))
        for invalid in (now-1, now, now+100_000_001):
            with self.assertRaises(ValueError):
                urgent(invalid, now)

    def test_same_bounded_offered_schedule(self):
        offered = offer(13, 2)
        self.assertEqual(offered, offer(13, 2))
        self.assertNotEqual(offered, offer(14, 2))
        self.assertEqual(sum(row[1] == 0 for row in offered), 240)
        self.assertTrue(all(0 < offset < 2_002_000_000 for offset, _, _ in offered))
        self.assertEqual(sorted(offered), offered)

    def test_refuse_misconfigured_hierarchy(self):
        rows = hierarchy()
        verify(rows, 'intent', CAPACITY)
        for category, index, field, value in [('class', 2, 'ceil', 10),
                                               ('qdisc', 1, 'kind', 'pfifo'),
                                               ('class', 0, 'rate', 30)]:
            invalid = copy.deepcopy(rows)
            invalid[category][index][field] = value
            with self.assertRaises(RuntimeError):
                verify(invalid, 'intent', CAPACITY)
        with self.assertRaises(RuntimeError):
            verify(dict(rows, filter=rows['filter'].replace('eq 42', 'eq 6')), 'intent', CAPACITY)

    def test_paired_tail_uses_only_complete_runs(self):
        rows = [dict(mode=mode, repetition=rep, p99_ms=1 if mode == 'intent' else 4,
                     failure_ratio=.2 if mode == 'cake' else 0,
                     deadline_attainment=1 if mode == 'intent' else .8, background_mbps=9)
                for mode in MODES for rep in range(10)]
        results = paired_summary(rows)
        self.assertEqual(results['cake']['p99_ms']['paired_differences'], [])
        self.assertEqual(results['cake-diffserv']['p99_ms']['median'], -3)
        self.assertAlmostEqual(results['cake']['deadline_attainment']['median'], .2)
        self.assertIsNotNone(results['cake']['deadline_attainment']['median_ci_95'])


if __name__ == '__main__':
    unittest.main()
