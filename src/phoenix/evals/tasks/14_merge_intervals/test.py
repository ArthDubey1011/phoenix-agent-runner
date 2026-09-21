from solution import merge_intervals

assert merge_intervals([[1, 3], [2, 6], [8, 10], [15, 18]]) == [[1, 6], [8, 10], [15, 18]]
assert merge_intervals([[1, 4], [4, 5]]) == [[1, 5]]
assert merge_intervals([]) == []
assert merge_intervals([[5, 6], [1, 2]]) == [[1, 2], [5, 6]]
assert merge_intervals([[1, 10], [2, 3]]) == [[1, 10]]
src = [[3, 4], [1, 3]]
merge_intervals(src)
assert src == [[3, 4], [1, 3]]
print("PASS")
