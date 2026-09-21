from solution import build_order


def cyc(d):
    try:
        build_order(d)
    except ValueError:
        return True
    return False


assert build_order({"c": ["a", "b"], "b": ["a"], "a": []}) == ["a", "b", "c"]
assert build_order({"d": ["b", "c"], "b": ["a"], "c": ["a"], "e": []}) == ["a", "b", "c", "d", "e"]
assert build_order({"b": ["a"]}) == ["a", "b"]
assert build_order({"z": [], "m": [], "a": []}) == ["a", "m", "z"]
assert build_order({}) == []
assert build_order({"b": ["a", "a"], "c": ["b", "a"]}) == ["a", "b", "c"]
assert build_order({"x": ["b"], "y": ["a"], "a": [], "b": []}) == ["a", "b", "x", "y"]
assert cyc({"x": ["y"], "y": ["x"]})
assert cyc({"a": ["a"]})
assert cyc({"a": ["b"], "b": ["c"], "c": ["a"], "d": []})
print("PASS")
