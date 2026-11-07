"""Small OTLP/HTTP JSON exporter used by the real application examples."""
import json
import math
import time
import urllib.error
import urllib.request

BOUNDS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]


class OtlpMetrics:
    def __init__(self, service, max_samples=50000):
        self.service = service
        self.started = time.time_ns()
        self.max_samples = max_samples
        self.counters = {}
        self.histograms = {}
        self.samples = 0

    def add_counter(self, name, value=1, attributes=None):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return
        key = (name, tuple(sorted((attributes or {}).items())))
        self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, name, value, attributes=None):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return
        key = (name, tuple(sorted((attributes or {}).items())))
        if self.samples < self.max_samples:
            self.histograms.setdefault(key, []).append(float(value))
            self.samples += 1

    @staticmethod
    def _attrs(attributes):
        return [{'key': key, 'value': {'stringValue': str(value)}}
                for key, value in attributes]

    def payload(self):
        end = time.time_ns()
        metrics = []
        for (name, attrs), values in self.histograms.items():
            buckets = [0] * (len(BOUNDS) + 1)
            for value in values:
                index = next((i for i, bound in enumerate(BOUNDS) if value <= bound), len(BOUNDS))
                buckets[index] += 1
            dp = {'attributes': self._attrs(attrs), 'startTimeUnixNano': str(self.started),
                  'timeUnixNano': str(end), 'count': str(len(values)),
                  'sum': sum(values), 'explicitBounds': BOUNDS,
                  'bucketCounts': list(map(str, buckets))}
            metrics.append({'name': name, 'unit': 's', 'histogram': {
                'aggregationTemporality': 2, 'dataPoints': [dp]}})
        for (name, attrs), value in self.counters.items():
            dp = {'attributes': self._attrs(attrs), 'startTimeUnixNano': str(self.started),
                  'timeUnixNano': str(end), 'asInt': str(int(value))}
            metrics.append({'name': name, 'unit': '{request}', 'sum': {
                'aggregationTemporality': 2, 'isMonotonic': True, 'dataPoints': [dp]}})
        return {'resourceMetrics': [{'resource': {'attributes': [
                    {'key': 'service.name', 'value': {'stringValue': self.service}}]},
                'scopeMetrics': [{'scope': {'name': 'flowcore.real_apps', 'version': '1'},
                                  'metrics': metrics}]}]}

    def export(self, endpoint, token=None, timeout=2):
        if not endpoint or not self.counters and not self.histograms:
            return {'exported': False, 'reason': 'no endpoint or samples'}
        data = json.dumps(self.payload(), separators=(',', ':')).encode()
        request = urllib.request.Request(endpoint, data=data, method='POST',
                                         headers={'Content-Type': 'application/json'})
        if token:
            request.add_header('Authorization', 'Bearer ' + token)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return {'exported': response.status in (200, 202, 204), 'status': response.status}
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            return {'exported': False, 'reason': type(error).__name__}
