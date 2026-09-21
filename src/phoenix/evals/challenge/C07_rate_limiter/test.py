import random

from solution import SlidingWindowLimiter

L = SlidingWindowLimiter(2, 10)
assert [L.allow("a", 0), L.allow("a", 1), L.allow("a", 5)] == [True, True, False]
assert L.allow("a", 10) is True      # the request at t=0 is exactly window old: no longer counted
assert L.allow("a", 10.5) is False   # (0.5, 10.5] holds t=1 and t=10
assert L.allow("a", 11) is True      # (1, 11] holds only t=10
assert L.allow("b", 5) is True       # keys are independent

rng = random.Random(3)
for limit, window in [(1, 1.0), (3, 5.0), (5, 2.5)]:
    lim = SlidingWindowLimiter(limit, window)
    allowed = {}
    t = 0.0
    for _ in range(400):
        t += rng.choice([0, 0.25, 0.5, 1.0, 2.5])
        key = rng.choice("xyz")
        recent = [x for x in allowed.get(key, []) if t - window < x <= t]
        expect = len(recent) < limit
        assert lim.allow(key, t) is expect, (limit, window, key, t)
        if expect:
            allowed.setdefault(key, []).append(t)
print("PASS")
