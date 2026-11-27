import pytest
from dates import days_in_month, is_leap_year


def test_year_divisible_by_4_is_leap():
    assert is_leap_year(2024)


def test_other_years_are_not_leap():
    assert not is_leap_year(2023)


def test_century_is_not_leap():
    assert not is_leap_year(1900)


def test_february_has_29_days_in_a_leap_year():
    assert days_in_month(2024, 2) == 29


def test_days_in_month_rejects_a_bad_month():
    with pytest.raises(ValueError):
        days_in_month(2024, 13)
