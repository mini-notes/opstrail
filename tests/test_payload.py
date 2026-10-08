"""Alertmanager payload parsing.

The label-merge test guards the easiest mistake to make with this payload:
reading only the top-level `labels` / `commonLabels` maps and silently
dropping `alerts[].labels`, which is where Alertmanager puts the labels that
identify an individual alert.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.alertmanager import AlertmanagerPayload


def _payload(**overrides) -> dict:
    base = {
        "version": "4",
        "status": "firing",
        "receiver": "opstrail",
        "groupLabels": {"alertname": "PostgresqlDown"},
        "commonLabels": {"alertname": "PostgresqlDown", "severity": "critical"},
        "commonAnnotations": {"summary": "group summary"},
        "externalURL": "http://alertmanager.local",
        "alerts": [
            {
                "status": "firing",
                "labels": {"service": "payments-api", "instance": "pg-01"},
                "annotations": {"description": "instance down"},
                "startsAt": "2026-10-05T08:30:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.local/graph",
                "fingerprint": "abc123",
            }
        ],
    }
    base.update(overrides)
    return base


def test_per_alert_labels_are_merged_in():
    """`service` lives only in alerts[].labels -- it must survive."""
    p = AlertmanagerPayload.model_validate(_payload())
    labels = p.effective_labels(p.alerts[0])
    assert labels["service"] == "payments-api"
    assert labels["instance"] == "pg-01"
    # group-level labels are still visible
    assert labels["alertname"] == "PostgresqlDown"
    assert labels["severity"] == "critical"


def test_alert_labels_win_over_common_labels():
    data = _payload()
    data["commonLabels"]["severity"] = "warning"
    data["alerts"][0]["labels"]["severity"] = "critical"
    p = AlertmanagerPayload.model_validate(data)
    assert p.effective_labels(p.alerts[0])["severity"] == "critical"


def test_annotations_merge_with_alert_winning():
    data = _payload()
    data["alerts"][0]["annotations"]["summary"] = "alert summary"
    p = AlertmanagerPayload.model_validate(data)
    ann = p.effective_annotations(p.alerts[0])
    assert ann["summary"] == "alert summary"
    assert ann["description"] == "instance down"


def test_go_zero_time_becomes_none():
    """A firing alert carries endsAt=0001-01-01; storing it verbatim would
    make every alert look resolved in year 1 and produce negative durations."""
    p = AlertmanagerPayload.model_validate(_payload())
    assert p.alerts[0].endsAt is None


def test_real_ends_at_is_kept():
    data = _payload()
    data["alerts"][0]["endsAt"] = "2026-10-05T09:12:00Z"
    p = AlertmanagerPayload.model_validate(data)
    assert p.alerts[0].endsAt == datetime(2026, 10, 5, 9, 12, tzinfo=UTC)


def test_missing_fingerprint_is_rejected():
    """Without a fingerprint the row cannot be stored idempotently, so the
    payload must fail loudly rather than duplicate on every repeat."""
    data = _payload()
    del data["alerts"][0]["fingerprint"]
    with pytest.raises(ValidationError):
        AlertmanagerPayload.model_validate(data)


def test_unknown_status_is_rejected():
    data = _payload()
    data["alerts"][0]["status"] = "flapping"
    with pytest.raises(ValidationError):
        AlertmanagerPayload.model_validate(data)


def test_unknown_top_level_fields_are_tolerated():
    """Alertmanager has added fields over time (truncatedAlerts,
    notification_reason); a new one must not break intake."""
    data = _payload(notification_reason="resolved", somethingNew=123)
    p = AlertmanagerPayload.model_validate(data)
    assert p.alerts[0].fingerprint == "abc123"
