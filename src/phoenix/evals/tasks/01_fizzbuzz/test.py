from solution import fizzbuzz

assert fizzbuzz(0) == []
assert fizzbuzz(5) == ["1", "2", "Fizz", "4", "Buzz"]
assert fizzbuzz(15)[-1] == "FizzBuzz"
assert len(fizzbuzz(100)) == 100
print("PASS")
