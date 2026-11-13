"""The paired 2.0 experiment keeps uncontrolled and static admission baselines."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from research_manifest import load_manifest
from deadline_runtime import profile


class OverloadManifest(unittest.TestCase):
    def test_profile_and_comparison_arms_are_validated(self):
        manifest = load_manifest(ROOT/'scenarios/overload-prevention-v2.json')
        self.assertEqual(manifest['deadline']['version'], 2)
        self.assertEqual(manifest['gates']['min_pairs'], 10)
        self.assertIn('deadline-unaware', manifest['modes'])
        self.assertIn('deadline-static', manifest['modes'])
        self.assertIn('deadline-admission', manifest['modes'])
        self.assertIn('deadline-concurrency-static', manifest['modes'])
        self.assertIn('deadline-overload-v2', manifest['modes'])

    def test_profile_rejects_invalid_bounds(self):
        manifest = load_manifest(ROOT/'scenarios/overload-prevention-v2.json')
        value = dict(manifest['deadline'], minimum_concurrency=65)
        with self.assertRaises(ValueError):
            profile(value)


if __name__ == '__main__':
    unittest.main()
