import pytest
from calc import mean, median, value_range


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_empty_raises():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd_length():
    assert median([3, 1, 2]) == 2


def test_median_even_length():
    assert median([4, 1, 3, 2]) == 2.5


def test_median_empty_raises():
    with pytest.raises(ValueError):
        median([])


def test_value_range():
    assert value_range([5, -2, 9]) == 11
