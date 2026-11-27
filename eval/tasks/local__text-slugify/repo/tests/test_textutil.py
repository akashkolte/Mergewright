from textutil import slugify, truncate


def test_slugify_lowercases_and_joins_words():
    assert slugify("Hello World") == "hello-world"


def test_slugify_custom_separator():
    assert slugify("Hello World", separator="_") == "hello_world"


def test_slugify_strips_leading_and_trailing_punctuation():
    assert slugify("  Hello! ") == "hello"


def test_truncate_keeps_short_text():
    assert truncate("short", 10) == "short"


def test_truncate_cuts_long_text():
    assert truncate("a long sentence", 9) == "a long..."
