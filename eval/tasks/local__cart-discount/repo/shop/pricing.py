"""Price helpers shared by the shop modules. Prices are in cents."""


def apply_percent_off(cents: int, percent: int) -> int:
    """Return the price after percent (0-100) off, rounded to the nearest cent."""
    if not 0 <= percent <= 100:
        raise ValueError(f"percent must be 0-100, got {percent}")
    return cents - cents * percent // 100
