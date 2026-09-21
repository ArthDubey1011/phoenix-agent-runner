from solution import nth_weekday

assert nth_weekday(2024, 11, 4, 3) == "2024-11-28"   # US Thanksgiving 2024
assert nth_weekday(2024, 5, -1, 0) == "2024-05-27"   # Memorial Day 2024
assert nth_weekday(2024, 9, 1, 6) == "2024-09-01"
assert nth_weekday(2024, 2, 5, 3) == "2024-02-29"    # leap day is the 5th Thursday
assert nth_weekday(2024, 2, -1, 3) == "2024-02-29"
assert nth_weekday(2024, 2, -2, 3) == "2024-02-22"
assert nth_weekday(2023, 1, 1, 6) == "2023-01-01"
assert nth_weekday(2023, 12, -1, 6) == "2023-12-31"
for bad in [(2023, 2, 5, 1), (2024, 5, 0, 0), (2024, 5, -6, 0), (2024, 5, 6, 0)]:
    try:
        nth_weekday(*bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"expected ValueError for {bad}")
print("PASS")
