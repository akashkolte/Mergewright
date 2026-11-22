"""Text helpers for building URLs."""

import re

_NON_WORD = re.compile(r"[^a-z0-9]")


def slugify(text: str, separator: str = "-") -> str:
    """Return a URL slug: lower-case words joined by separator."""
    slug = _NON_WORD.sub(separator, text.strip().lower())
    return slug.strip(separator)


def truncate(text: str, width: int, suffix: str = "...") -> str:
    """Return text cut to at most width characters, ending in suffix if it was cut."""
    if len(text) <= width:
        return text
    return text[: max(width - len(suffix), 0)] + suffix
