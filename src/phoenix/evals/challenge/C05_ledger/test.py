from solution import Ledger


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


L = Ledger()
L.open("a", 100)
L.open("b")
assert raises(L.open, "a")
assert L.transfer("t1", "a", "b", 40) is True
assert (L.balance("a"), L.balance("b")) == (60, 40)
assert L.transfer("t1", "a", "b", 40) is False          # idempotent replay
assert L.transfer("t1", "nope", "nope", -5) is False    # replay wins over validation
assert (L.balance("a"), L.balance("b")) == (60, 40)
assert L.transfer("t2", "a", "b", 60) is True           # draining to exactly zero is allowed
assert L.balance("a") == 0
assert raises(L.transfer, "t3", "a", "b", 1)            # insufficient funds
assert raises(L.transfer, "t3", "a", "a", 1)            # same account
assert raises(L.transfer, "t3", "a", "b", 0)
assert raises(L.transfer, "t3", "a", "b", -3)
assert raises(L.transfer, "t3", "a", "zzz", 1)
assert raises(L.transfer, "t3", "zzz", "a", 1)
assert (L.balance("a"), L.balance("b")) == (0, 100)     # failures changed nothing
L.transfer("t9", "b", "a", 10)
assert L.transfer("t3", "a", "b", 5) is True            # a failed txid can be retried after top-up
assert L.history("a") == [("t1", -40), ("t2", -60), ("t9", 10), ("t3", -5)]
assert L.history("b") == [("t1", 40), ("t2", 60), ("t9", -10), ("t3", 5)]
h = L.history("a")
h.append(("x", 1))
assert len(L.history("a")) == 4
assert raises(L.transfer, "t4", "a", "b", 6) and L.balance("a") == 5
print("PASS")
