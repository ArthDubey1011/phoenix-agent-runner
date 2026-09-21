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
        value = parse_term()
        while peek() in ("+", "-"):
            if take() == "+":
                value += parse_term()
            else:
                value -= parse_term()
        return value

    def parse_term():
        value = parse_factor()
        while peek() in ("*", "/"):
            if take() == "*":
                value *= parse_factor()
            else:
                value /= parse_factor()
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
