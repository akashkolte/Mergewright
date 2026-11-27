import pytest
from settings import parse_bool, parse_int


def test_parse_bool_true_words():
    assert all(parse_bool(word) is True for word in ["1", "true", "Yes", " on "])


def test_parse_bool_rejects_other_words():
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_parse_int():
    assert parse_int(" 42 ") == 42


def test_parse_int_empty_gives_the_default():
    assert parse_int("", default=7) == 7
