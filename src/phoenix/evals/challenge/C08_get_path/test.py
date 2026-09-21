from solution import get_path

D = {"a": {"b": [10, {"c": "deep"}, 30]}, "a.b": "dotted", "n": None, "z": 0, "l": [[1, 2], [3]]}
assert get_path(D, "a.b[1].c") == "deep"
assert get_path(D, "a.b[-1]") == 30
assert get_path(D, "a.b[-3]") == 10
assert get_path(D, "a\\.b") == "dotted"
assert get_path(D, "a.b[3]", "dflt") == "dflt"
assert get_path(D, "a.b[-4]", "dflt") == "dflt"
assert get_path(D, "nope", "dflt") == "dflt"
assert get_path(D, "a[0]", "dflt") == "dflt"       # indexing a dict
assert get_path(D, "l.x", "dflt") == "dflt"         # key lookup in a list
assert get_path(D, "n", "dflt") is None              # present but None
assert get_path(D, "z", "dflt") == 0                 # present but falsy
assert get_path(D, "") is D
assert get_path(D, "l[1][0]") == 3
assert get_path([[1, 2], [3]], "[0][1]") == 2
assert get_path([[1, 2], [3]], "[5]", "d") == "d"
assert get_path({"a": 1}, "a.b", "d") == "d"        # descending into an int
assert get_path(D, "a.b[1].c.d.e", "d") == "d"
print("PASS")
