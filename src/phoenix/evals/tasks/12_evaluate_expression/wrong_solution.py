import re


def evaluate(expr):
    tokens = re.findall(r"\d+\.?\d*|[-+*/()]", expr)
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def parse_expr():
        value = parse_factor()
        while peek() in ("+", "-", "*", "/"):
            op = take()
            rhs = parse_factor()
            value = {"+": value + rhs, "-": value - rhs, "*": value * rhs, "/": value / rhs}[op]
        return value

    def parse_factor():
        tok = take()
        if tok == "-":
            return -parse_factor()
        if tok == "(":
            value = parse_expr()
            take()
            return value
        return float(tok)

    return float(parse_expr())
