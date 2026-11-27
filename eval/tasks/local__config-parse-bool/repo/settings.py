"""Typed settings from string values, such as environment variables."""

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


def parse_bool(value: str) -> bool:
    """Parse a boolean setting such as "yes" or "0". Raise ValueError for other words."""
    word = value.strip().lower()
    if word not in TRUE_WORDS | FALSE_WORDS:
        raise ValueError(f"not a boolean: {value!r}")
    return bool(word)


def parse_int(value: str, default: int = 0) -> int:
    """Parse an integer setting. An empty value gives default."""
    value = value.strip()
    return int(value) if value else default
