import pytest
from shop.cart import Cart


def test_subtotal_sums_the_quantities():
    cart = Cart()
    cart.add("pen", 150, quantity=2)
    cart.add("pen", 150)
    assert cart.subtotal() == 450


def test_add_rejects_a_zero_quantity():
    with pytest.raises(ValueError):
        Cart().add("pen", 150, quantity=0)


def test_total_without_a_discount():
    cart = Cart()
    cart.add("book", 1999)
    assert cart.total() == 1999


def test_total_with_a_discount():
    cart = Cart(percent_off=10)
    cart.add("book", 1000)
    assert cart.total() == 900
