import random

from solution import LFUCache

c = LFUCache(2)
c.put(1, 1)
c.put(2, 2)
assert c.get(1) == 1
c.put(3, 3)                      # evicts 2 (fewest uses)
assert c.get(2) == -1
assert c.get(3) == 3
c.put(4, 4)                      # 1 and 3 tie on 2 uses; 1 is least recently used
assert c.get(1) == -1 and c.get(3) == 3 and c.get(4) == 4
z = LFUCache(0)
z.put(1, 1)
assert z.get(1) == -1
u = LFUCache(2)
u.put(1, "a")
u.put(2, "b")
u.put(1, "A")                    # update counts as a use
u.put(3, "c")                    # evicts 2 (1 use) not 1 (2 uses)
assert u.get(2) == -1 and u.get(1) == "A" and u.get(3) == "c"
assert u.get(99) == -1


class Model:
    def __init__(self, cap):
        self.cap, self.items, self.t = cap, {}, 0    # key -> [value, uses, stamp]

    def _use(self, k):
        self.t += 1
        self.items[k][1] += 1
        self.items[k][2] = self.t

    def get(self, k):
        if k not in self.items:
            return -1
        self._use(k)
        return self.items[k][0]

    def put(self, k, v):
        if self.cap <= 0:
            return
        if k in self.items:
            self.items[k][0] = v
        else:
            if len(self.items) >= self.cap:
                victim = min(self.items, key=lambda x: (self.items[x][1], self.items[x][2]))
                del self.items[victim]
            self.items[k] = [v, 0, 0]
        self._use(k)


rng = random.Random(5)
for cap in (1, 2, 3, 5):
    real, model = LFUCache(cap), Model(cap)
    for step in range(800):
        present = list(model.items)
        r = rng.random()
        # Mostly touch cached keys, so every key reaches 2+ uses and frequency ties (where the
        # least-recently-used tie-break matters) actually occur before the next new key arrives.
        if present and r < 0.75:
            k = rng.choice(present)
            assert real.get(k) == model.get(k), (cap, step, k)
        elif r < 0.9:
            k = rng.randint(1, cap + 3)
            assert real.get(k) == model.get(k), (cap, step, k)
        else:
            k, v = rng.randint(1, cap + 3), rng.randint(0, 99)
            real.put(k, v)
            model.put(k, v)
print("PASS")
