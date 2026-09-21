def encode(s):
    out = []
    i = 0
    while i < len(s):
        j = i
        while j < len(s) and s[j] == s[i]:
            j += 1
        n = j - i
        while n > 0:
            k = min(n, 9)
            out.append(s[i] + (str(k) if k > 1 else ""))
            n -= k
        i = j
    return "".join(out)


def decode(t):
    out = []
    i = 0
    while i < len(t):
        c = t[i]
        i += 1
        if i < len(t) and t[i].isdigit():
            out.append(c * int(t[i]))
            i += 1
        else:
            out.append(c)
    return "".join(out)
