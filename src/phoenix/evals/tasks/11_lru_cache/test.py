from solution import LRUCache

c = LRUCache(2)
c.put(1, 1)
c.put(2, 2)
assert c.get(1) == 1
c.put(3, 3)
assert c.get(2) == -1
c.put(4, 4)
assert c.get(1) == -1
assert c.get(3) == 3
assert c.get(4) == 4
d = LRUCache(1)
d.put(1, 1)
d.put(1, 2)
assert d.get(1) == 2
e = LRUCache(2)
e.put(1, 1)
e.put(2, 2)
e.put(1, 10)
e.put(3, 3)
assert e.get(2) == -1 and e.get(1) == 10
print("PASS")
