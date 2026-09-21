class LFUCache:
    def __init__(self, capacity):
        self.cap = capacity
        self.vals = {}
        self.freq = {}
        self.last = {}
        self.tick = 0

    def _touch(self, k):
        self.tick += 1
        self.freq[k] = self.freq.get(k, 0) + 1
        self.last[k] = self.tick

    def get(self, k):
        if k not in self.vals:
            return -1
        self._touch(k)
        return self.vals[k]

    def put(self, k, v):
        if self.cap <= 0:
            return
        if k not in self.vals and len(self.vals) >= self.cap:
            victim = min(self.vals, key=lambda x: (self.freq[x], self.last[x]))
            for d in (self.vals, self.freq, self.last):
                del d[victim]
        self.vals[k] = v
        self._touch(k)
