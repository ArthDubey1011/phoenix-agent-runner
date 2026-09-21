import calendar
import datetime


def nth_weekday(year, month, n, weekday):
    last = calendar.monthrange(year, month)[1]
    days = [d for d in range(1, last + 1) if datetime.date(year, month, d).weekday() == weekday]
    if n == 0 or abs(n) > len(days):
        raise ValueError("no such day")
    return datetime.date(year, month, days[n - 1] if n > 0 else days[n]).isoformat()
