import random

from solution import convert

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"  # the test must not depend on solution internals

assert convert("ff", 16, 2) == "11111111"
assert convert("FF", 16, 10) == "255"
assert convert("-255", 10, 16) == "-ff"
assert convert("0", 2, 10) == "0"
assert convert("-0", 10, 2) == "0"
assert convert("000", 10, 7) == "0"
assert convert("z", 36, 10) == "35"
assert convert("0012", 10, 10) == "12"
assert convert("10", 2, 36) == "2"
for bad in [("", 10, 2), ("-", 10, 2), ("2", 2, 10), ("1_0", 10, 2), (" 10", 10, 2), ("10 ", 10, 2),
            ("+1", 10, 2), ("1.0", 10, 2), ("g", 16, 10), ("--1", 10, 2), ("1", 1, 10), ("1", 10, 37)]:
    try:
        convert(*bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"expected ValueError for {bad}")


def to_base(n, b):
    if n == 0:
        return "0"
    s, m = "", abs(n)
    while m:
        m, r = divmod(m, b)
        s = DIGITS[r] + s
    return ("-" if n < 0 else "") + s


rng = random.Random(11)
for _ in range(200):
    n = rng.randint(-10**rng.randint(1, 60), 10**rng.randint(1, 60))
    a, b = rng.randint(2, 36), rng.randint(2, 36)
    assert convert(to_base(n, a), a, b) == to_base(n, b), (n, a, b)
print("PASS")
