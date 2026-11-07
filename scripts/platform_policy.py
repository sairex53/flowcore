"""Validated policy overlays and a revisioned atomic policy/audit store."""
import json
import os
import re
import tempfile
import time
from intents import DEFAULT
from service_config import keys, number, unique

LABEL = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}')
SCOPES = ('global','device','interface','service','container','cgroup')
FIELDS = {'priority','target_p99_ms','desired_kbit','min_kbit','max_kbit','background_min_pct','reliability','deadline_ms'}
EMPTY = {'version':1,**{key:{} for key in SCOPES}}


def overlay(row):
    if not isinstance(row,dict) or set(row)-FIELDS:
        raise ValueError('unknown policy field')
    for key in ('desired_kbit','min_kbit','max_kbit'):
        if key in row:
            number(row[key],0,1000000,True)
    if 'background_min_pct' in row:
        number(row['background_min_pct'],20,100,True)
    for key in ('target_p99_ms','deadline_ms'):
        if key in row and (key!='deadline_ms' or row[key] is not None):
            number(row[key],.001,600000)
    if 'priority' in row and row['priority'] not in ('critical','interactive','standard'):
        raise ValueError('unsupported priority')
    if 'reliability' in row and row['reliability'] not in ('balanced','prefer_reliable'):
        raise ValueError('unsupported reliability preference')


def merged(policy, device=None, interface=None, service=None, container=None, cgroup=None):
    fields = {key:DEFAULT[key] for key in FIELDS}
    sources = {key:'built-in' for key in fields}
    levels = [('global',policy['global'])]
    for scope,selector in zip(SCOPES[1:],(device,interface,service,container,cgroup)):
        if selector is not None and not isinstance(selector,str):
            raise ValueError('policy selectors must be strings')
        if selector in policy.get(scope,{}):
            levels.append((scope,policy[scope][selector]))
    for scope,row in levels:
        fields.update(row)
        sources.update({key:scope for key in row})
    if fields['max_kbit'] and max(fields['min_kbit'],fields['desired_kbit'])>fields['max_kbit']:
        raise ValueError('inherited bandwidth minimum or target exceeds maximum')
    return dict(fields=fields,provenance=sources)


def validate(policy):
    keys(policy, {'version',*(scope for scope in SCOPES if scope!='cgroup' or 'cgroup' in policy)})
    number(policy['version'],1,1,True)
    overlay(policy['global'])
    for scope in SCOPES[1:]:
        rows = policy.get(scope,{})
        if not isinstance(rows,dict) or len(rows)>12:
            raise ValueError(f'{scope} supports at most 12 policy selectors')
        for selector,row in rows.items():
            if (not isinstance(selector,str) or
                (scope=='cgroup' and (not selector.startswith('/') or len(selector)>4096 or
                 any(part in ('','.','..') for part in selector[1:].split('/')))) or
                (scope!='cgroup' and not LABEL.fullmatch(selector))):
                raise ValueError('invalid policy selector')
            overlay(row)
    bandwidth = ('min_kbit','desired_kbit','max_kbit')
    triples = {tuple(DEFAULT[key] for key in bandwidth)}
    for rows in ([policy['global']], *([{},*policy.get(scope,{}).values()] for scope in SCOPES[1:])):
        triples = {tuple(row.get(key,old[i]) for i,key in enumerate(bandwidth)) for row in rows for old in triples}
    if any(ceil and max(floor,wanted)>ceil for floor,wanted,ceil in triples):
        raise ValueError('some inherited bandwidth policies conflict')
    return policy


def load_json(path, limit=131072):
    with path.open('rb') as stream:
        data = stream.read(limit+1)
    if len(data)>limit:
        raise ValueError('policy file is oversized')
    return json.loads(data,object_pairs_hook=unique)


class PolicyStore:
    def __init__(self):
        self.path = None
        self.state = dict(revision=0,policy=EMPTY,audit=[])

    def restore(self, directory):
        self.path = directory/'platform-policy.json'
        if self.path.exists():
            from durable_state import read_json
            state = read_json(self.path)
            keys(state, ('revision','policy','audit'))
            number(state['revision'],0,2**63-1,True)
            validate(state['policy'])
            if not isinstance(state['audit'],list) or len(state['audit'])>128:
                raise ValueError('invalid policy audit file')
            self.state = state

    def commit(self, policy, expected, uid):
        validate(policy)
        if type(expected) is not int or expected!=self.state['revision']:
            raise ValueError('policy revision conflict')
        if policy==self.state['policy']:
            return dict(changed=False,revision=expected)
        if self.path is None:
            raise RuntimeError('policy store not ready')
        if expected == 2**63-1:
            raise ValueError('policy revision exhausted')
        revision = expected+1
        state = dict(revision=revision,policy=policy,audit=(
            self.state['audit']+[dict(revision=revision,time_unix=time.time(),owner_uid=uid,
                                       operation='replace_profiles')])[-128:])
        from durable_state import atomic_json
        atomic_json(self.path,state,on_commit=lambda:setattr(self,'state',state))
        return dict(changed=True,revision=revision)
