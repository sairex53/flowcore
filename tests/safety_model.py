"""Finite safety specification. Imports no FLOWCORE implementation."""
from collections import deque
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class State:
    daemon: bool = False
    resource: str = 'absent'
    controller: str = 'off'
    policy: bool = False
    lease: bool = False
    capacity: str = 'normal'
    transaction: str = 'idle'
    verified: bool = False
    helper: str = 'ready'
    endpoint: str = 'absent'


def successors(s):
    if not s.daemon:
        yield 'restart', replace(s,daemon=True,helper='ready')
        return
    yield 'crash', State(policy=s.policy)
    yield 'shutdown', State(policy=s.policy)
    yield 'unauthorized_start', s
    yield 'unauthorized_helper', s
    yield 'create_policy', replace(s,policy=True)
    yield 'update_policy', replace(s,policy=True)
    yield 'remove_policy', replace(s,policy=False)
    if s.helper=='ready':
        yield 'reject_helper_mutation', replace(s,helper='failed')
    if s.resource=='absent':
        yield 'create_lab', replace(s,resource='owned',capacity='normal')
        return
    yield 'remove_interface', replace(s,resource='absent',endpoint='absent',
                                      controller='off',transaction='idle',verified=False)
    if s.endpoint=='absent':
        yield 'register_path', replace(s,endpoint='owned')
    if s.endpoint=='owned':
        yield 'path_backup', s
        yield 'remove_path', replace(s,endpoint='absent')
        yield 'reuse_path_id', replace(s,endpoint='foreign')
    if s.endpoint=='foreign':
        yield 'reject_stale_path', s
    if s.capacity=='normal':
        yield 'change_capacity', replace(s,capacity='drift',controller='fallback',
                                        transaction='idle',verified=False)
    if not s.lease:
        yield 'register_service', replace(s,lease=True)
    else:
        yield 'expire_service', replace(s,lease=False,controller='off',transaction='idle',verified=False)
    if s.controller!='off':
        yield 'stop', replace(s,controller='off',transaction='idle',verified=False)
    if s.controller=="off" and s.capacity=='normal':
        yield 'authorized_start', replace(s,controller='active',verified=True)
    if s.controller=='active':
        if s.transaction=='idle':
            yield 'plan', replace(s,transaction='planned')
        elif s.transaction=='planned':
            yield 'first_mutation', replace(s,transaction='partial',verified=False)
            yield 'reject_before_mutation', replace(s,controller='fallback',transaction='idle',verified=False)
        elif s.transaction=='partial':
            yield 'verified_commit', replace(s,transaction='idle',verified=True)
            yield 'reject_kernel_operation', replace(s,transaction='compensating')
        elif s.transaction=='compensating':
            for action in ('verified_compensation','reject_rollback'):
                yield action, replace(s,transaction='idle',controller='fallback',verified=False)


def invariant(s):
    if s.controller=='active':
        assert s.daemon and s.resource=="owned" and s.capacity=='normal'
    if s.verified:
        assert s.controller=='active' and s.transaction in ('idle','planned')
    if s.transaction!='idle':
        assert s.controller=='active' and s.resource=='owned'
    if not s.daemon:
        assert not s.lease and s.resource=='absent' and s.controller=='off'
    if s.endpoint!='absent':
        assert s.resource=='owned'


def explore():
    initial = State()
    queue, reached, edges = deque([initial]), {initial}, 0
    actions = set()
    while queue:
        s = queue.popleft()
        invariant(s)
        for action, target in successors(s):
            actions.add(action)
            edges += 1
            if action.startswith('unauthorized') or action=='reject_stale_path':
                assert s==target
            if action in ('first_mutation','path_backup','remove_path'):
                assert s.resource=='owned'
            if target not in reached:
                queue.append(target)
                reached.add(target)
    return dict(states=len(reached),edges=edges,actions=len(actions))


if __name__=='__main__':
    print(explore())
