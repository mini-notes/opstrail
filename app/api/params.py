"""Query-parameter types that tolerate what an HTML form actually sends.

A `<form method="get">` submits every control, including the ones left at
their placeholder option, so the browser sends `?service_id=&team_id=&days=`.
FastAPI then tries to parse "" as an int and answers 422 -- which means the
Filter button was broken in its most common case: nothing selected.

Switching the annotations to `str` and parsing by hand would fix the symptom
and throw away validation with it. Instead a BeforeValidator maps the empty
string to None *before* validation, so a genuinely bad value (`?page=0`,
`?service_id=abc`) is still rejected.
"""

from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated

from pydantic import BeforeValidator


def _blank_to_none(value):
    """Treat an empty/whitespace-only submission as 'not filtered'."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptInt = Annotated[int | None, BeforeValidator(_blank_to_none)]
OptStr = Annotated[str | None, BeforeValidator(_blank_to_none)]


OptDateTime = Annotated[datetime | None, BeforeValidator(_blank_to_none)]


def parse_window(
    from_: str | None, to: str | None, tz_offset: int | None
) -> tuple[datetime | None, datetime | None, int]:
    """Turn the time picker's fields into an absolute window in UTC.

    The picker sends local wall-clock strings (what `datetime-local` gives)
    plus the viewer's offset, rather than pre-converted instants: the offset
    is needed anyway for day bucketing, and sending both keeps one source of
    truth instead of two that can disagree.

    Anything unparseable is treated as "no absolute window" rather than an
    error. A malformed `?from=` in a pasted URL should fall back to the
    default view, not refuse to draw the dashboard.
    """
    offset = tz_offset if tz_offset is not None and abs(tz_offset) <= 840 else 0
    tz = timezone(timedelta(minutes=offset))

    def one(raw: str | None) -> datetime | None:
        if not raw or not raw.strip():
            return None
        try:
            moment = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
        # A value that already carries an offset is an instant; a bare
        # wall-clock reading is in the viewer's zone.
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        return moment.astimezone(UTC)

    start, end = one(from_), one(to)
    if start is None or end is None:
        return None, None, offset
    if start > end:
        start, end = end, start
    return start, end, offset
