"""Differential API compatibility including empty, overflow and merged histograms."""
import random
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from telemetry_windows import Histogram, Windows
from native_telemetry import MODE

class NativeTelemetry(unittest.TestCase):
    def test_randomized_summaries_preserve_python_reference(self):
        rng=random.Random(39017)
        for _ in range(300):
            result=Histogram()
            for _ in range(rng.randrange(1,8)):
                part=Histogram()
                for _ in range(rng.randrange(150)):
                    part.add(rng.choice((0,1,2,7,100,1000,2**31,2**40)))
                result.merge(part)
            expected=dict(samples=result.count,overflow_samples=result.overflow,
                          **{f'p{p}_us':result.quantile(p/100) for p in (50,95,99)})
            self.assertEqual(result.summary(),expected)
        self.assertIsNone(Histogram().summary()['p99_us'])

    def test_window_boundaries_and_complete_window(self):
        windows=Windows(3)
        for second in range(4):
            windows.at(second*10**9).observe('a',second*100)
        result=windows.summarize(3*10**9,2,complete=True)
        self.assertEqual(result.histograms['a'].count,2)
        self.assertEqual(result.histograms['a'].summary()['p99_us'],
                         result.histograms['a'].quantile(.99))
        self.assertIsNone(windows.at(0))

if __name__=='__main__':
    print('backend:',MODE)
    unittest.main()
