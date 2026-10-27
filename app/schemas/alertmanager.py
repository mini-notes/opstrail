"""Alertmanager webhook payload, format version 4.

Schema per https://prometheus.io/docs/alerting/latest/configuration/#webhook_config

Validation is deliberately lenient about EXTRA fields (Alertmanager has added
`notification_reason` and `truncatedAlerts` over time) but strict about the
ones we key on: an alert without `fingerprint` or `startsAt` cannot be stored
idempotently, so it is rejected loudly rather than silently duplicated.
"""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: AwareDatetime  # noqa: N815 - wire format is camelCase
    endsAt: AwareDatetime | None = None  # noqa: N815
    generatorURL: str | None = None  # noqa: N815
    fingerprint: str

    @field_validator("status")
    @classmethod
    def _known_status(cls, v: str) -> str:
        if v not in ("firing", "resolved"):
            raise ValueError(f"unexpected alert status {v!r}")
        return v

    @field_validator("endsAt")
    @classmethod
    def _zero_time_is_none(cls, v: datetime | None) -> datetime | None:
        """Alertmanager sends the Go zero time for an unresolved alert.

        `0001-01-01T00:00:00Z` is not a real timestamp; storing it would make
        every firing alert look like it resolved in year 1.
        """
        if v is not None and v.year <= 1:
            return None
        return v


class AlertmanagerPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "4"
    groupKey: str | None = None  # noqa: N815
    truncatedAlerts: int = 0  # noqa: N815
    status: str = "firing"
    receiver: str | None = None
    groupLabels: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    commonLabels: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    commonAnnotations: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    externalURL: str | None = None  # noqa: N815
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)

    def effective_labels(self, alert: AlertmanagerAlert) -> dict[str, str]:
        """Labels visible for one alert, most specific first.

        Alertmanager puts the labels that identify ONE alert in
        `alerts[].labels`, and only the labels shared by the whole group in
        `commonLabels`. Reading just the top-level maps therefore loses
        exactly the labels that distinguish one alert from another: a payload
        carrying `alerts[0].labels.service = "payments-api"` would be stored
        with no service at all.

        Precedence: the alert's own labels win over the group's.
        """
        merged = dict(self.commonLabels)
        merged.update(self.groupLabels)
        merged.update(alert.labels)
        return merged

    def effective_annotations(self, alert: AlertmanagerAlert) -> dict[str, str]:
        merged = dict(self.commonAnnotations)
        merged.update(alert.annotations)
        return merged
