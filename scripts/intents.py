"""Bounded, authenticated service leases and persistent operator intents."""
import copy
import json
import re
import secrets
import time
from pathlib import Path

from service_config import number

DEFAULT = dict(priority='critical', target_p99_ms=30, desired_kbit=0, min_kbit=0, max_kbit=0,
               background_min_pct=20, reliability='balanced', deadline_ms=None,
               ttl_seconds=300, persistent=False)


class Registry:
    def __init__(self, observer):
        self.observer = observer
        self.rows = {}
        self.base = copy.deepcopy(observer.cfg['services'])
        self.path = None
        self.revision = 0

    def validate(self, value):
        if not isinstance(value, dict) or set(value) - {'name', *DEFAULT}:
            raise ValueError('invalid intent fields')
        row = dict(DEFAULT, **value)
        name = row.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name):
            raise ValueError('invalid service name')
        if row['priority'] not in ('critical', 'interactive', 'standard'):
            raise ValueError('unsupported intent priority')
        for key in ('desired_kbit', 'min_kbit', 'max_kbit'):
            number(row[key], 0, 1000000, True)
        if row['max_kbit'] and max(row['min_kbit'], row['desired_kbit']) > row['max_kbit']:
            raise ValueError('intent minimum or target exceeds maximum')
        number(row['target_p99_ms'], .001, 600000)
        number(row['ttl_seconds'], 1, 86400, True)
        number(row['background_min_pct'], 20, 100, True)
        if row['deadline_ms'] is not None:
            number(row['deadline_ms'], .001, 600000)
        if type(row['persistent']) is not bool or row['reliability'] not in ('balanced', 'prefer_reliable'):
            raise ValueError('invalid persistence or reliability preference')
        return row

    def sync(self):
        self.observer.cfg['services'].clear()
        self.observer.cfg['services'].update(self.base)
        for name, row in self.rows.items():
            intent = row['intent']
            self.observer.cfg['services'][name] = dict(
                **{'class': intent['priority']}, slo_ms=intent['target_p99_ms'])

    def persist(self, rows):
        if not self.path:
            return
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({name: {'intent': row['intent'], 'owner_uid': row['owner_uid'],
                                          'expires': row['expires']}
                                  for name, row in rows.items() if row['intent']['persistent']}))
        tmp.replace(self.path)

    def restore(self, runtime):
        self.path = Path(runtime) / 'intents.json'
        if not self.path.exists():
            return
        with self.path.open() as stream:
            data = json.loads(stream.read(65536))
        for name, row in data.items():
            if row['expires'] > time.time():
                self.admit(self.validate(row['intent']))
                self.rows[name] = dict(row, instances={}, monotonic_expiry=time.monotonic()+row['expires']-time.time())
        self.sync()

    def admit(self, intent):
        name, priority = intent['name'], intent['priority']
        if name not in self.rows and name not in self.base and len(set(self.rows) | set(self.base)) >= 16:
            raise ValueError('service registry full')
        if name in self.base and self.base[name]['class'] != priority:
            raise ValueError('intent conflicts with configured service class')
        if not getattr(self, 'shared_classes', False) and any(other != name and row['intent']['priority'] == priority for other, row in self.rows.items()):
            raise ValueError('class already assigned to another intent')

    def register(self, value, uid, pid=None):
        self.expire()
        intent = self.validate(value)
        self.admit(intent)
        name = intent['name']
        row = self.rows.get(name)
        if row and (row['owner_uid'] != uid or row['intent'] != intent):
            raise ValueError('conflicting intent or owner')
        if row is None:
            row = dict(intent=intent, owner_uid=uid, expires=time.time()+intent['ttl_seconds'], instances={},
                       monotonic_expiry=time.monotonic()+intent['ttl_seconds'])
            if intent['persistent']:
                self.persist(dict(self.rows, **{name: row}))
            self.rows[name] = row
            self.revision += 1
        if len(row['instances']) >= 64:
            raise ValueError('too many service instances')
        cgroup = None
        if pid:
            try:
                with open(f'/proc/{pid}/cgroup') as stream:
                    cgroup = stream.read(4096).strip()
            except OSError:
                pass
        token = secrets.token_hex(16)
        row['instances'][token] = dict(peer_pid=pid, cgroup_at_registration=cgroup)
        self.sync()
        return dict(instance_token=token, **self.inspect(name))

    def inspect(self, name):
        if not isinstance(name, str) or name not in self.rows:
            raise ValueError('unknown intent')
        row = self.rows[name]
        return dict(intent=row['intent'], owner_uid=row['owner_uid'], expires_unix=row['expires'],
                    instances=list(row['instances'].values()))

    def clear_samples(self, name):
        prefix = f'app.{name}.'
        for bucket in self.observer.windows.buckets.values():
            for mapping in (bucket.counters, bucket.histograms):
                for key in list(mapping):
                    if key.startswith(prefix):
                        del mapping[key]
        for token, item in list(self.observer.requests.items()):
            if item[1] == name:
                del self.observer.requests[token]

    def remove(self, name, uid, token=None, expired=False):
        self.inspect(name)
        row = self.rows[name]
        if row['owner_uid'] != uid:
            raise ValueError('intent owner mismatch')
        if token is not None:
            if not isinstance(token, str) or token not in row['instances']:
                raise ValueError('invalid instance token')
            del row['instances'][token]
            if row['instances'] or row['intent']['persistent']:
                return
        if row['intent']['persistent'] and not expired:
            self.persist({key: value for key, value in self.rows.items() if key != name})
        del self.rows[name]
        self.clear_samples(name)
        self.revision += 1
        self.sync()
        if expired and row['intent']['persistent']:
            try:
                self.persist(self.rows)
            except OSError:
                pass  # The persisted lease is already expired.

    def expire(self):
        for name, row in list(self.rows.items()):
            if row['monotonic_expiry'] <= time.monotonic():
                self.remove(name, row['owner_uid'], expired=True)
