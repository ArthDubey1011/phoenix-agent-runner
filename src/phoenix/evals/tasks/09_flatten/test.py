from solution import flatten

assert flatten([1, [2, [3, [4]], 5]]) == [1, 2, 3, 4, 5]
assert flatten([]) == []
assert flatten([[], [[]]]) == []
assert flatten(["ab", ["c"]]) == ["ab", "c"]
print("PASS")
