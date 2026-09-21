from solution import group_anagrams

assert group_anagrams(["eat", "tea", "tan", "ate", "nat", "bat"]) == [
    ["ate", "eat", "tea"],
    ["bat"],
    ["nat", "tan"],
]
assert group_anagrams([]) == []
assert group_anagrams([""]) == [[""]]
assert group_anagrams(["a", "a"]) == [["a", "a"]]
print("PASS")
