import random

from solution import decode, encode

assert encode("") == "" and decode("") == ""
assert encode("aaabcc") == "a3bc2"
assert encode("a" * 12) == "a9a3"
assert encode("a" * 10) == "a9a"
assert encode("a" * 9) == "a9"
assert encode("a" * 19) == "a9a9a"
assert encode("ab") == "ab"
assert decode("a9a3") == "a" * 12
assert decode("a9ab") == "a" * 10 + "b"
rng = random.Random(7)
for _ in range(300):
    s = "".join(rng.choice("abc") * rng.randint(1, 25) for _ in range(rng.randint(0, 6)))
    assert decode(encode(s)) == s, s
    assert len(encode(s)) <= len(s) * 2
print("PASS")
