"""Small statistics helpers. Demo input for the Mergewright agent."""


def mean(values: list[float]) -> float:
    """Return the arithmetic mean of values."""
    if not values:
        raise ValueError("mean() requires at least one value")
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    """Return the median of values."""
    if not values:
        raise ValueError("median() requires at least one value")
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def value_range(values: list[float]) -> float:
    """Return the difference between the largest and smallest value."""
    if not values:
        raise ValueError("value_range() requires at least one value")
    return max(values) - min(values)
