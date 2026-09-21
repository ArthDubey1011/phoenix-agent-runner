def _parse(path):
    tokens = []
    buf = ""
    i = 0
    while i < len(path):
        c = path[i]
        if c == "\\" and i + 1 < len(path) and path[i + 1] == ".":
            buf += "."
            i += 2
            continue
        if c == ".":
            if buf != "":
                tokens.append(buf)
                buf = ""
            i += 1
            continue
        if c == "[":
            if buf != "":
                tokens.append(buf)
                buf = ""
            j = path.index("]", i)
            tokens.append(int(path[i + 1 : j]))
            i = j + 1
            continue
        buf += c
        i += 1
    if buf != "":
        tokens.append(buf)
    return tokens


def get_path(obj, path, default=None):
    cur = obj
    for t in _parse(path):
        if isinstance(t, int):
            if isinstance(cur, list) and -len(cur) <= t < len(cur):
                cur = cur[t]
            else:
                return default
        else:
            if isinstance(cur, dict) and t in cur:
                cur = cur[t]
            else:
                return default
    return cur
