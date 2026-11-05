"""Correctness and bounded-work regressions for sparse histogram aggregation."""
import random
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from telemetry_windows import Histogram


class Performance(unittest.TestCase):
    def test_sparse_merge_matches_dense_reference(self):
        rng=random.Random(812)
        merged=Histogram()
        dense=[0]*257
        count=overflow=0
        for _ in range(100):
            histogram=Histogram()
            for _ in range(200):
                histogram.add(rng.choice((0,1,1000,2000,2**40)))
            merged.merge(histogram)
            dense=[a+b for a,b in zip(dense,histogram.counts)]
            count+=histogram.count
            overflow+=histogram.overflow
        self.assertEqual(merged.counts,dense)
        self.assertEqual((merged.count,merged.overflow),(count,overflow))
        self.assertEqual(len(merged.occupied),len(set(merged.occupied)))
        self.assertEqual(set(merged.occupied),{i for i,n in enumerate(dense) if n})
        self.assertLessEqual(len(merged.occupied),257)
        reference=Histogram()
        reference.counts=dense
        reference.count=count
        reference.overflow=overflow
        for p in (.5,.95,.99):
            self.assertEqual(merged.quantile(p),reference.quantile(p))


if __name__=='__main__':
    unittest.main()
