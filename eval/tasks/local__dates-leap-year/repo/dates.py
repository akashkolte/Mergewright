"""Gregorian calendar helpers."""

_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def is_leap_year(year: int) -> bool:
    """Return True if year is a leap year in the Gregorian calendar."""
    return year % 4 == 0 and year % 100 != 0


def days_in_month(year: int, month: int) -> int:
    """Return the number of days in month (1-12) of year."""
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1-12, got {month}")
    if month == 2 and is_leap_year(year):
        return 29
    return _DAYS_IN_MONTH[month - 1]
