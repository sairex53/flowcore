"""Unit coverage for the bounded runtime soak and resource inventory."""
import argparse
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime_soak as soak


class RuntimeSoakTest(unittest.TestCase):
    def test_proc_stat_handles_parentheses_and_extracts_identity(self):
        tail = ['S', '7', '9', '9', '0', '-1', '0', '0', '0', '0', '0', '11', '13',
                '0', '0', '20', '0', '1', '0', '1234']
        row = soak.parse_proc_stat('123 (process ) with spaces) ' + ' '.join(tail))
        self.assertEqual(row['comm'], 'process ) with spaces')
        self.assertEqual(row['ppid'], 7)
        self.assertEqual(row['pgrp'], 9)
        self.assertEqual(row['cpu_ticks'], 24)
        self.assertEqual(row['start_ticks'], 1234)

    def test_descendant_walk_is_bounded_and_cycle_safe(self):
        rows = {
            10: dict(ppid=1), 11: dict(ppid=10), 12: dict(ppid=11),
            13: dict(ppid=10), 14: dict(ppid=14),
        }
        self.assertEqual(soak.descendant_pids(10, rows), [10, 11, 12, 13])
        self.assertEqual(soak.descendant_pids(99, rows), [])

    def test_directory_inventory_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'output'
            outside = Path(temp) / 'outside'
            root.mkdir()
            outside.write_bytes(b'outside data')
            (root / 'sample.json').write_bytes(b'abc')
            (root / 'link').symlink_to(outside)
            result = soak.directory_inventory(root)
            self.assertEqual(result, dict(available=True, files=1, bytes=3, truncated=False))

    def test_network_signature_normalization_drops_counters_only(self):
        value = {'kind': 'fq_codel', 'stats': {'bytes': 20, 'drops': 1},
                 'options': {'limit': 1000}, 'expires': 30}
        self.assertEqual(soak._stable_network_value(value),
                         {'kind': 'fq_codel', 'options': {'limit': 1000}})

    def test_delta_gate_preserves_unknown_inventory(self):
        unknown = soak._check_delta('count', {'available': False},
                                    {'available': False}, 0)
        self.assertEqual(unknown['status'], 'UNVERIFIED')
        passed = soak._check_delta('count', {'available': True, 'count': 4},
                                   {'available': True, 'count': 4}, 0)
        failed = soak._check_delta('count', {'available': True, 'count': 4},
                                   {'available': True, 'count': 5}, 0)
        self.assertEqual(passed['status'], 'PASS')
        self.assertEqual(failed['status'], 'FAIL')

    def test_profiles_use_fixed_argv_without_shell(self):
        command = soak._profile_command('service', ROOT / 'scenarios/example.json',
                                        'optimizer-overload', 10, ROOT / 'build')
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[2]), ROOT / 'tests/service_integration.py')
        self.assertNotIn('shell', command)
        research = soak._profile_command('research', ROOT / 'scenarios/example.json',
                                         'optimizer-overload', 10, ROOT / 'build')
        self.assertEqual(research[1:3], ['bench', 'soak'])

    def test_manifest_must_be_inside_scenarios_and_not_symlinked(self):
        args = argparse.Namespace(profile='research', cycles=1, duration_seconds=60,
                                  sample_seconds=5, max_output_mb=10, max_minutes=5,
                                  max_rss_growth_mb=128, max_fd_growth=32,
                                  max_thread_growth=16, max_netns_growth=0,
                                  max_qdisc_growth=0, max_bpf_growth=0,
                                  max_volume_growth=0,
                                  min_free_mb=1024,
                                  build_dir=ROOT / 'build',
                                  tc=Path(soak.shutil.which('tc') or ROOT/'tools/root/usr/bin/tc'),
                                  manifest=ROOT / 'README.md', mode='optimizer-overload')
        with self.assertRaisesRegex(ValueError, 'under scenarios'):
            soak.validate_args(args)
        with tempfile.TemporaryDirectory(dir=ROOT / 'scenarios') as temp:
            link = Path(temp) / 'alias.json'
            link.symlink_to(ROOT / 'scenarios/overload-gradual-smoke.json')
            args.manifest = link
            with self.assertRaisesRegex(ValueError, 'symlink'):
                soak.validate_args(args)

    def test_tc_falls_back_to_the_repository_platform_tool(self):
        args = argparse.Namespace(profile='service', cycles=1, duration_seconds=60,
            sample_seconds=5, max_output_mb=10, max_minutes=5, max_rss_growth_mb=128,
            max_fd_growth=32, max_thread_growth=16, max_netns_growth=0,
            max_qdisc_growth=0, max_bpf_growth=0, max_volume_growth=0, min_free_mb=1024,
            build_dir=ROOT/'build',
            tc=Path(soak.shutil.which('tc') or ROOT/'tools/root/usr/bin/tc'),
            manifest=ROOT/'README.md', mode='optimizer-overload')
        validated = soak.validate_args(args)
        self.assertTrue(validated.tc.is_file())

    def test_early_late_resources_and_request_denominators_are_preserved(self):
        steps = [
            dict(profile='research', cycle=0,
                 peak_process=dict(rss_bytes=100, open_fds=8, threads=2),
                 research_artifacts=[dict(trials=[dict(mode='optimizer', valid=True,
                     offered=100, completed=95, failed=5, p99_ms=20.0,
                     background_mbps=5.0, adjustments=1, rollbacks=0)])]),
            dict(profile='research', cycle=1,
                 peak_process=dict(rss_bytes=110, open_fds=8, threads=2),
                 research_artifacts=[dict(trials=[dict(mode='optimizer', valid=True,
                     offered=200, completed=194, failed=6, p99_ms=25.0,
                     background_mbps=4.5, adjustments=2, rollbacks=1)])]),
        ]
        summary = soak._stability_summary(steps, dict(rss_growth_bytes=100,
            fd_growth=2, thread_growth=2))
        self.assertEqual(summary['resource_drift_status'], 'PASS')
        trial = summary['workload_by_mode']['optimizer']
        self.assertEqual(trial['offered_requests'], 300)
        self.assertEqual(trial['failed_requests'], 11)
        self.assertEqual(trial['controller_adjustments'], 3)
        self.assertEqual(trial['controller_rollbacks'], 1)

    def test_qdisc_gate_fails_only_for_host_namespace_change(self):
        def snapshot(qdiscs):
            return dict(network_namespaces=dict(available=True, count=1),
                temporary_directory=dict(available=True, entries=3),
                qdiscs=qdiscs, container_volumes=dict(available=False),
                bpf=dict(available=False, reason='missing'),
                host_network=dict(available=True, sha256='host'))
        before_qdisc = dict(available=True, count=1, current_namespace='1',
                            by_namespace={'1': {'sha256': 'a'}})
        host_after = dict(available=True, count=2, current_namespace='1',
                          by_namespace={'1': {'sha256': 'b'}})
        other_after = dict(available=True, count=2, current_namespace='1',
                           by_namespace={'1': {'sha256': 'a'}, '2': {'sha256': 'c'}})
        limits = dict(netns=0, qdiscs=0, volumes=0, bpf=0)
        host_gate = soak._resource_gates(snapshot(before_qdisc), snapshot(host_after), limits, [])
        other_gate = soak._resource_gates(snapshot(before_qdisc), snapshot(other_after), limits, [])
        self.assertEqual(host_gate['qdiscs']['status'], 'FAIL')
        self.assertEqual(other_gate['qdiscs']['status'], 'UNVERIFIED')


if __name__ == '__main__':
    unittest.main()
