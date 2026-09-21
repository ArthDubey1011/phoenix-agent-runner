from solution import evaluate

assert evaluate("1 + 2 * 3") == 7
assert evaluate("(1+2)*3") == 9
assert evaluate("-2 * -3") == 6
assert evaluate("10 / 4") == 2.5
assert evaluate("2 * (3 + 4) - 5 / (1 + 1)") == 11.5
assert evaluate("-(2+3)") == -5
assert evaluate("1 - 2 - 3") == -4
assert evaluate("8 / 2 / 2") == 2
print("PASS")
