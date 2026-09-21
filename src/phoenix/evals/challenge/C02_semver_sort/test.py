import random

from solution import sort_versions

ordered = [
    "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
    "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
]
for seed in range(5):
    shuffled = ordered[:]
    random.Random(seed).shuffle(shuffled)
    assert sort_versions(shuffled) == ordered, shuffled
assert sort_versions(["1.10.0", "1.9.0", "1.2.10", "1.2.9"]) == ["1.2.9", "1.2.10", "1.9.0", "1.10.0"]
assert sort_versions(["1.0.0+b", "1.0.0+a", "1.0.0"]) == ["1.0.0+b", "1.0.0+a", "1.0.0"]
assert sort_versions(["2.0.0", "1.9.9+zzz", "1.9.9-rc.1+abc"]) == ["1.9.9-rc.1+abc", "1.9.9+zzz", "2.0.0"]
assert sort_versions(["1.0.0-1", "1.0.0-a"]) == ["1.0.0-1", "1.0.0-a"]
assert sort_versions([]) == []
print("PASS")
