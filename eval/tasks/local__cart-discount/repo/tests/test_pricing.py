import pytest
from shop.pricing import apply_percent_off


def test_percent_off_rejects_more_than_100():
    with pytest.raises(ValueError):
        apply_percent_off(1000, 101)
