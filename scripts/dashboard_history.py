"""Bounded SQLite history for compact, versioned dashboard observations."""
import json
import math
import os
import sqlite3
import stat
import threading
import time

from dashboard_model import fields


def compact(snapshot):
    if snapshot is None:
        return None
    links = {}
    for row in snapshot['interfaces']:
        egress = row['directions'].get('egress', {})
        ingress = row['directions'].get('ingress', {})
        links[row['id']] = dict(egress_kbit=egress.get('throughput_kbit'),
            ingress_kbit=ingress.get('throughput_kbit'), backlog_bytes=egress.get('backlog_bytes'),
            dropped_packets=egress.get('dropped_packets'), capacity_kbit=egress.get('capacity_kbit'),
            idle_headroom_kbit=egress.get('idle_headroom_kbit'))
    services = {row['id']: fields(row, ('p50_ms', 'p95_ms', 'p99_ms', 'target_ms', 'samples', 'error_rate',
                  'deadline_miss_rate', 'requests_per_second', 'above_target')) for row in snapshot['services']}
    classes = {row['id']: fields(row, ('served_kbit', 'backlog_bytes', 'dropped_packets', 'guaranteed_kbit'))
               for row in snapshot['classes']}
    return dict(window_seconds=snapshot['window_seconds'], scope=snapshot['scope'],
                interface=links, service=services, traffic_class=classes)


class History:
    def __init__(self, directory, *, hours, samples, storage_mib):
        self.lock = threading.Lock()
        self.seconds = hours * 3600
        self.sample_limit = samples
        path = directory / 'history-v1.sqlite'
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info and (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                     or info.st_nlink != 1 or info.st_mode & 0o077):
            raise PermissionError('history must be an owned private regular file')
        self.db = sqlite3.connect(path, timeout=2, check_same_thread=False)
        try:
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA auto_vacuum=FULL')
            pages = storage_mib * 1024 * 1024 // self.db.execute('PRAGMA page_size').fetchone()[0]
            if self.db.execute(f'PRAGMA max_page_count={pages}').fetchone()[0] > pages:
                raise ValueError('history exceeds configured storage cap')
            self.db.execute('CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, data TEXT)')
            self.db.execute('CREATE INDEX IF NOT EXISTS samples_by_time ON samples(ts)')
            with self.db:
                self.prune(time.time_ns()//1_000_000)
        except BaseException:
            self.db.close()
            raise

    def prune(self, now):
        self.db.execute('DELETE FROM samples WHERE ts < ?', (now-self.seconds*1000,))
        self.db.execute('DELETE FROM samples WHERE id <= (SELECT id FROM samples ORDER BY id DESC LIMIT 1 OFFSET ?)',
                        (self.sample_limit,))

    def append(self, now, snapshot):
        payload = compact(snapshot)
        data = json.dumps(payload, separators=(',', ':'), allow_nan=False) if payload is not None else None
        if data is not None and len(data.encode()) > 48*1024:
            raise ValueError('compact sample exceeds history limit')
        with self.lock:
            try:
                self.insert(now, data)
            except sqlite3.OperationalError as error:
                if getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                    raise
                # The failed transaction has rolled back. Make bounded room
                # for newer observations even when the byte limit wins.
                with self.db:
                    self.db.execute('DELETE FROM samples WHERE id IN (SELECT id FROM samples ORDER BY id LIMIT 32)')
                self.insert(now, data)

    def insert(self, now, data):
        with self.db:
            self.prune(now)
            self.db.execute('INSERT INTO samples(ts, data) VALUES (?, ?)', (now, data))
            self.prune(now)

    def series(self, now, kind, identifier, seconds, points):
        begin = now - seconds*1000
        width = max(1, math.ceil((seconds*1000+1)/points))
        # A newest-sample view of time buckets. A stored missing sample stays
        # missing; percentiles are never averaged or reconstructed.
        with self.lock:
            rows = self.db.execute('''
                SELECT ts, data FROM samples WHERE id IN (
                    SELECT max(id) FROM samples WHERE ts BETWEEN ? AND ?
                    GROUP BY (ts-?)/?
                ) ORDER BY ts, id LIMIT ?
                ''', (begin, now, begin, width, points)).fetchall()
        items = []
        for ts, blob in rows:
            data = json.loads(blob) if blob is not None else None
            metrics = data[kind].get(identifier) if data is not None else None
            items.append(dict(time_ms=ts, values=metrics,
                              scope=data['scope'] if data else None,
                              window_seconds=data['window_seconds'] if data else None))
        return dict(points=items, bucket_ms=width, from_ms=begin, to_ms=now)

    def close(self):
        with self.lock:
            self.db.close()
