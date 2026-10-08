"""Retention window arithmetic."""

from datetime import UTC, datetime, timedelta

from app.services.retention import cutoff_for

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def test_default_six_months_is_about_180_days():
    assert (NOW - cutoff_for(6, now=NOW)) == timedelta(days=180)


def test_cutoff_scales_with_months():
    assert cutoff_for(1, now=NOW) > cutoff_for(12, now=NOW)


def test_cutoff_is_timezone_aware():
    """A naive cutoff compared against timestamptz raises at query time."""
    assert cutoff_for(6, now=NOW).tzinfo is not None
