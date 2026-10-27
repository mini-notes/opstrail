"""Shared result type for outbound notification channels."""

from dataclasses import dataclass


@dataclass(slots=True)
class DeliveryResult:
    ok: bool
    status_code: int | None = None
    duration_ms: float | None = None
    error: str | None = None
    retry_after_seconds: float | None = None


def retry_after(response) -> float | None:
    from datetime import UTC, datetime
    from email.utils import parsedate_to_datetime

    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(max(float(value), 0), 86400)
    except ValueError:
        try:
            return min(
                max((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(), 0), 86400
            )
        except (TypeError, ValueError, OverflowError):
            return None
