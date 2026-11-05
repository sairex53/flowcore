"""Bounded rolling telemetry. Timestamps use CLOCK_MONOTONIC."""
import math
from collections import Counter


class Histogram:
    # Eight logarithmic bins per power of two, 1 us to above 600 s.
    bins = 257

    def __init__(self):
        self.counts = [0] * self.bins
        self.count = 0
        self.overflow = 0

    def add(self, microseconds):
        index = 1 + int(math.log2(max(1, microseconds)) * 8) if microseconds else 0
        if index >= self.bins:
            self.overflow += 1
        else:
            self.counts[index] += 1
        self.count += 1

    def merge(self, other):
        self.count += other.count
        self.overflow += other.overflow
        for index, count in enumerate(other.counts):
            self.counts[index] += count

    def quantile(self, fraction):
        rank = math.ceil(self.count * fraction)
        if not rank:
            return None
        for index, count in enumerate(self.counts):
            rank -= count
            if rank <= 0:
                return math.ceil(2 ** (index / 8)) if index else 0
        return None

    def summary(self, unit='us'):
        return {'samples': self.count, 'overflow_samples': self.overflow, **{f'p{p}_{unit}': self.quantile(p / 100) for p in (50, 95, 99)}}


class Bucket:
    def __init__(self):
        self.counters = Counter()
        self.histograms = {}

    def observe(self, key, value):
        if key not in self.histograms:
            self.histograms[key] = Histogram()
        self.histograms[key].add(value)


class Windows:
    def __init__(self, maximum):
        self.maximum = maximum
        self.buckets = {}
        self.latest = -1
        self.late_drops = 0

    def at(self, ns):
        second = ns // 1_000_000_000
        if second > self.latest:
            self.latest = second
            for old in list(self.buckets):
                if old <= second - self.maximum:
                    del self.buckets[old]
        if second <= self.latest - self.maximum:
            self.late_drops += 1
            return None
        if second not in self.buckets:
            self.buckets[second] = Bucket()
        return self.buckets[second]

    def summarize(self, ns, seconds):
        end = ns // 1_000_000_000
        result = Bucket()
        for tick, bucket in self.buckets.items():
            if not end - seconds < tick <= end:
                continue
            result.counters.update(bucket.counters)
            for key, histogram in bucket.histograms.items():
                if key not in result.histograms:
                    result.histograms[key] = Histogram()
                result.histograms[key].merge(histogram)
        return result
