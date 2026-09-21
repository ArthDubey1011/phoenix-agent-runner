import collections


class SlidingWindowLimiter:
    def __init__(self, limit, window):
        self.limit = limit
        self.window = window
        self.events = {}

    def allow(self, key, t):
        q = self.events.setdefault(key, collections.deque())
        while q and q[0] <= t - self.window:
            q.popleft()
        if len(q) < self.limit:
            q.append(t)
            return True
        return False
