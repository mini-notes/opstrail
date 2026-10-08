"""Shared result type for outbound notification channels."""

from dataclasses import dataclass


@dataclass(slots=True)
class DeliveryResult:
    ok: bool
    status_code: int | None = None
    duration_ms: float | None = None
    error: str | None = None
