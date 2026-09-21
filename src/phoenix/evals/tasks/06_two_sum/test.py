from solution import two_sum

assert two_sum([2, 7, 11, 15], 9) == (0, 1)
assert two_sum([3, 2, 4], 6) == (1, 2)
assert two_sum([3, 3], 6) == (0, 1)
assert two_sum([1, 2], 10) is None
assert two_sum([1, 2, 3, 4], 5) == (0, 3)
print("PASS")
