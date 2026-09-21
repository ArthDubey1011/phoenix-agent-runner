from solution import wrap_text

assert wrap_text("the quick brown fox", 10) == ["the quick", "brown fox"]
assert wrap_text("a b c", 1) == ["a", "b", "c"]
assert wrap_text("supercalifragilistic", 5) == ["super", "calif", "ragil", "istic"]
assert wrap_text("hi supercalifragilistic yo", 6) == ["hi", "superc", "alifra", "gilist", "ic yo"]
assert wrap_text("", 5) == [] and wrap_text("   \n ", 5) == []
assert wrap_text("  spaced   out  ", 7) == ["spaced", "out"]
assert wrap_text("ab cd", 5) == ["ab cd"]
assert wrap_text("ab cd", 4) == ["ab", "cd"]
assert wrap_text("abcdef gh", 3) == ["abc", "def", "gh"]
assert wrap_text("abcdefg h", 3) == ["abc", "def", "g h"]
for line in wrap_text("lorem ipsum dolor sit amet consectetur adipiscing elit " * 3, 11):
    assert 0 < len(line) <= 11 and line == line.strip()
print("PASS")
