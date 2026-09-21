from solution import top_k_frequent

assert top_k_frequent(["i", "love", "leetcode", "i", "love", "coding"], 2) == ["i", "love"]
assert top_k_frequent(
    ["the", "day", "is", "sunny", "the", "the", "the", "sunny", "is", "is"], 4
) == ["the", "is", "sunny", "day"]
assert top_k_frequent([], 3) == []
assert top_k_frequent(["a", "b"], 5) == ["a", "b"]
assert top_k_frequent(["b", "a"], 1) == ["a"]
print("PASS")
