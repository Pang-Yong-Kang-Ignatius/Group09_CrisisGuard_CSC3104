"""Opt-in evaluation policies; normal replay and grant contracts stay unchanged."""
import json
import os


def replay_settings():
    """Retain the tuple API for older evaluation callers."""
    from edge.scheduler.config import load_scheduler_settings
    settings = load_scheduler_settings()
    return settings.enable_inheritance, settings.selection_policy


def controller_settings():
    policy = os.getenv('CONTROLLER_SCHEDULING_POLICY', 'bounded')
    limit = int(os.getenv('CONTROLLER_BYPASS_LIMIT', '1'))
    if policy not in ('bounded', 'fifo', 'priority', 'drr') or limit < 0:
        raise ValueError('Invalid experimental controller policy')
    if (policy != 'bounded' or limit != 1) and os.getenv('CRISISGUARD_EXPERIMENTAL_POLICIES') != '1':
        raise ValueError('Experimental controller policies require explicit opt-in')
    costs = json.loads(os.getenv('CONTROLLER_DRR_EVENT_COSTS', '{}'))
    quantum = int(os.getenv('CONTROLLER_DRR_QUANTUM', '1'))
    if not isinstance(costs, dict) or any(not isinstance(k, str) or type(v) is not int or not 1 <= v <= 1000 for k, v in costs.items()):
        raise ValueError('DRR costs must map event IDs to integer units in 1..1000')
    if not 1 <= quantum <= 1000:
        raise ValueError('DRR quantum must be in 1..1000')
    return policy, limit, costs, quantum


def choose(tickets, policy, limit, state=None, costs=None, quantum=1):
    """Choose from queue-position order; DRR state is committed with the grant."""
    if policy == 'fifo':
        return 0, state
    if policy != 'drr':
        highest = max(range(len(tickets)), key=lambda i: tickets[i]['priority'])
        allowed = policy == 'priority' or all(t['bypass_count'] < limit for t in tickets[:highest])
        return (highest if allowed else 0), state
    state = state or {'queue': [], 'deficits': {}, 'credited': False}
    active = {t['hospital_id']: i for i, t in enumerate(tickets)}
    queue = [site for site in state['queue'] if site in active]
    deficits = {site: state['deficits'].get(site, 0) for site in queue}
    credited = state.get('credited', False) and bool(queue) and queue[0] == (state['queue'] or [None])[0]
    for site in active:
        if site not in queue:
            queue.append(site)
            deficits[site] = 0
    while True:
        site = queue[0]
        if not credited:
            deficits[site] += quantum
        cost = (costs or {}).get(tickets[active[site]]['event_id'], 1)
        if deficits[site] >= cost:
            deficits[site] -= cost
            return active[site], {'queue': queue, 'deficits': deficits, 'credited': True}
        queue.append(queue.pop(0))
        credited = False


def withdraw_drr(state, site):
    if state is None:
        return None
    queue = [s for s in state['queue'] if s != site]
    return {'queue': queue, 'deficits': {s: state['deficits'][s] for s in queue},
            'credited': state.get('credited', False) and bool(queue) and queue[0] == (state['queue'] or [None])[0]}
