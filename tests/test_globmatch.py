"""Glob matching, and the equivalence of its Python and SQL forms.

The literal-percent case is the one that matters most: an alert label like
`disk_usage=100%` must not turn a rule into a catch-all.
"""

import pytest

from app.services.globmatch import has_wildcard, matches, to_like


@pytest.mark.parametrize(
    "pattern,value,expected",
    [
        ("pg-prod-01", "pg-prod-01", True),
        ("pg-prod-01", "pg-prod-02", False),
        ("pg-prod-*", "pg-prod-01", True),
        ("pg-prod-*", "pg-stg-01", False),
        ("*api*", "payments-api", True),
        ("*api*", "worker", False),
        ("node-?", "node-1", True),
        ("node-?", "node-12", False),
        ("*", "anything", True),
    ],
)
def test_glob_matching(pattern, value, expected):
    assert matches(pattern, value) is expected


def test_matching_is_case_insensitive():
    assert matches("PG-Prod-*", "pg-prod-01")
    assert matches("pg-prod-*", "PG-PROD-01")


def test_empty_pattern_matches_nothing():
    """A half-filled form must not become a rule that claims every alert."""
    assert not matches("", "anything")
    assert not matches("", "")


def test_missing_label_never_matches():
    """`value is None` means the alert has no such label at all."""
    assert not matches("*", None)
    assert not matches("exact", None)


def test_has_wildcard():
    assert has_wildcard("pg-*")
    assert has_wildcard("node-?")
    assert not has_wildcard("pg-prod-01")


@pytest.mark.parametrize(
    "pattern,like",
    [
        ("pg-prod-*", "pg-prod-%"),
        ("node-?", "node-_"),
        ("exact", "exact"),
        ("*", "%"),
    ],
)
def test_to_like_translation(pattern, like):
    assert to_like(pattern) == like


@pytest.mark.parametrize(
    "pattern,like",
    [
        ("100%", r"100\%"),          # literal percent, not a wildcard
        ("a_b", r"a\_b"),            # literal underscore, not single-char
        ("back\\slash", r"back\\slash"),
        ("100%-*", r"100\%-%"),      # literal AND wildcard in one pattern
    ],
)
def test_like_metacharacters_are_escaped(pattern, like):
    """Escaping must happen BEFORE `*`/`?` become `%`/`_`, or it would
    neuter the wildcards too."""
    assert to_like(pattern) == like
