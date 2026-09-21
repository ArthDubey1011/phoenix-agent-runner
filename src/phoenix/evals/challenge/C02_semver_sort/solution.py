def _key(v):
    v = v.split("+", 1)[0]
    core, _, pre = v.partition("-")
    nums = tuple(int(x) for x in core.split("."))
    if not pre:
        return (nums, 1, ())
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (nums, 0, ids)


def sort_versions(versions):
    return sorted(versions, key=_key)
