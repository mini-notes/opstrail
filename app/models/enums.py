"""Enumerations shared by models and schemas.

Stored as native Postgres enums so the database rejects bad values.
"""

import enum


class AlertStatus(str, enum.Enum):
    firing = "firing"
    resolved = "resolved"


class LabelSource(str, enum.Enum):
    """Where an alert's service/team attribution came from.

    `payload` means the Prometheus rule carried the label itself -- the
    cheapest and most trustworthy source. `rule` means a label_rules row
    matched. `manual` means an operator tagged it by hand, which is also a
    signal that a label_rule is missing.
    """

    payload = "payload"
    rule = "rule"
    manual = "manual"


class IncidentStatus(str, enum.Enum):
    open = "open"
    acked = "acked"
    resolved = "resolved"


class NotificationChannel(str, enum.Enum):
    msteams = "msteams"
    webhook = "webhook"


class NotificationStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class OwnerRole(str, enum.Enum):
    """How an owner relates to a service or team.

    `primary` is who gets mentioned first. `backup` and `escalation` are
    declared now so the on-call / SMS work in a later phase has somewhere to
    read from instead of needing another migration.
    """

    primary = "primary"
    backup = "backup"
    escalation = "escalation"


class UserRole(str, enum.Enum):
    admin = "admin"
    editor = "editor"
    viewer = "viewer"
