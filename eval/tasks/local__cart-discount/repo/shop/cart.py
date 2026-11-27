"""A shopping cart. Prices are in cents."""

from shop.pricing import apply_percent_off


class Cart:
    def __init__(self, percent_off: int = 0) -> None:
        self.percent_off = percent_off
        self.lines: dict[str, tuple[int, int]] = {}  # sku -> (unit price, quantity)

    def add(self, sku: str, price_cents: int, quantity: int = 1) -> None:
        if quantity < 1:
            raise ValueError(f"quantity must be at least 1, got {quantity}")
        _, held = self.lines.get(sku, (price_cents, 0))
        self.lines[sku] = (price_cents, held + quantity)

    def subtotal(self) -> int:
        return sum(price * quantity for price, quantity in self.lines.values())

    def total(self) -> int:
        """The subtotal with the cart's discount applied."""
        return apply_percent_off(self.subtotal(), self.percent_off)
