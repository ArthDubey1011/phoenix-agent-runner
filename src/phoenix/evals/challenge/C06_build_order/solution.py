import heapq


def build_order(deps):
    tasks = set(deps)
    for ps in deps.values():
        tasks.update(ps)
    prereq = {t: set(deps.get(t, ())) for t in tasks}
    dependents = {t: [] for t in tasks}
    for t, ps in prereq.items():
        for p in ps:
            dependents[p].append(t)
    ready = [t for t in tasks if not prereq[t]]
    heapq.heapify(ready)
    order = []
    while ready:
        t = heapq.heappop(ready)
        order.append(t)
        for d in dependents[t]:
            prereq[d].discard(t)
            if not prereq[d]:
                heapq.heappush(ready, d)
    if len(order) != len(tasks):
        raise ValueError("cycle")
    return order
