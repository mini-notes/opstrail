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
