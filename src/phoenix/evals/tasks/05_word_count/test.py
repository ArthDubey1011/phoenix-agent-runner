from solution import word_count

assert word_count("The cat and the hat.") == {"the": 2, "cat": 1, "and": 1, "hat": 1}
assert word_count("It's") == {"it": 1, "s": 1}
assert word_count("") == {}
assert word_count("a A a") == {"a": 3}
print("PASS")
