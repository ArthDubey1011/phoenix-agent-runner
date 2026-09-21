DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def convert(s, from_base, to_base):
    if not (2 <= from_base <= 36 and 2 <= to_base <= 36):
        raise ValueError("bad base")
    neg = s.startswith("-")
    body = s[1:] if neg else s
    if not body:
        raise ValueError("empty")
    value = 0
    for c in body.lower():
        d = DIGITS.find(c)
        if d < 0 or d >= from_base:
            raise ValueError("bad digit")
        value = value * from_base + d
    if value == 0:
        return "0"
    out = []
    while value:
        value, r = divmod(value, to_base)
        out.append(DIGITS[r])
    return ("-" if neg else "") + "".join(reversed(out))
