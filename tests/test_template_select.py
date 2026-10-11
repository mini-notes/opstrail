"""Which template an alert gets.

The invariant that outranks everything else here: with nothing configured,
selection returns None and the built-in file template sends the card exactly
as it did before any of this existed. Alertmanager is the source of truth and
OpsTrail adds context to it -- it never replaces it, and it never withholds a
card because a routing rule was wrong.
"""

import datetime as dt
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import (
    Alert,
    AlertStatus,
    MessageTemplate,
    NotificationChannel,
    Service,
    Team,
    TemplateRule,
)
from app.services import template_select
from app.services.labeling import _rule_matches

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)
MSTEAMS = NotificationChannel.msteams


@pytest.fixture
async def session():
    engine = create_async_engine(DSN)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no test database at {DSN}: {exc}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def make_alert(**kw) -> Alert:
    base = dict(
        id=1, fingerprint="fp1",
        starts_at=dt.datetime(2026, 10, 7, 8, 30, tzinfo=dt.UTC),
        received_at=dt.datetime(2026, 10, 7, 8, 30, tzinfo=dt.UTC),
        status=AlertStatus.firing, alertname="PostgresqlDown",
        severity="critical", labels={}, annotations={},
    )
    base.update(kw)
    return Alert(**base)


async def _template(session, name, body, *, default=False) -> MessageTemplate:
    row = MessageTemplate(
        channel=MSTEAMS, name=name, body=body, is_active=default
    )
    session.add(row)
    await session.flush()
    return row


async def _rule(session, template, match, *, priority=100, enabled=True):
    row = TemplateRule(
        template_id=template.id, match=match, priority=priority, enabled=enabled
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# the invariant: nothing configured changes nothing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_templates_and_no_rules_means_the_builtin(session):
    """A fresh install, or one that never touched templates, must send the
    same card it sent yesterday."""
    body, note = await template_select.select_source(session, make_alert(), MSTEAMS)
    assert body is None
    assert note is None


@pytest.mark.asyncio
async def test_rules_exist_but_none_match_falls_back_to_the_default(session):
    default = await _template(session, "default", '{"d":1}', default=True)
    other = await _template(session, "loud", '{"loud":1}')
    await _rule(session, other, {"severity": "info"})
    await session.commit()

    body, note = await template_select.select_source(session, make_alert(), MSTEAMS)
    assert body == default.body
    assert note is None


@pytest.mark.asyncio
async def test_no_rules_and_no_default_still_means_the_builtin(session):
    await _template(session, "unused", '{"x":1}')  # exists but is not the default
    await session.commit()

    body, _ = await template_select.select_source(session, make_alert(), MSTEAMS)
    assert body is None


@pytest.mark.asyncio
async def test_a_broken_query_degrades_to_the_default_rather_than_raising(
    session, monkeypatch
):
    """Selection must never be the reason a card does not go out."""
    default = await _template(session, "default", '{"d":1}', default=True)
    await session.commit()

    def boom(*a, **kw):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(template_select, "match_context", boom)

    body, note = await template_select.select_source(session, make_alert(), MSTEAMS)
    assert body == default.body
    assert note is not None and "default" in note


# ---------------------------------------------------------------------------
# precedence: the payload wins
# ---------------------------------------------------------------------------

def test_alert_labels_beat_the_resolved_service():
    """Alertmanager sent `service=payments-api-v2`; attribution landed on a
    Service keyed `payments-api`. The label is what was sent, so `service`
    holds the label -- OpsTrail does not rewrite its own input."""
    svc = Service(id=1, key="payments-api", name="Payments", tier=1)
    ctx = template_select.match_context(
        make_alert(labels={"service": "payments-api-v2"}), service=svc
    )
    assert ctx["service"] == "payments-api-v2"
    # ...and the attribution is still matchable, under its own name.
    assert ctx["resolved_service"] == "payments-api"


def test_resolved_service_fills_in_when_the_alert_carries_no_label():
    svc = Service(id=1, key="payments-api", name="Payments")
    ctx = template_select.match_context(make_alert(labels={}), service=svc)
    assert ctx["service"] == "payments-api"
    assert ctx["resolved_service"] == "payments-api"


def test_tier_comes_from_the_service_and_is_a_string():
    svc = Service(id=1, key="x", name="X", tier=1)
    assert template_select.match_context(make_alert(), service=svc)["tier"] == "1"


def test_unclassified_service_has_no_tier_key_at_all():
    """Absent, not empty: a rule with a tier condition must not match a
    service nobody has classified."""
    svc = Service(id=1, key="x", name="X", tier=None)
    ctx = template_select.match_context(make_alert(), service=svc)
    assert "tier" not in ctx
    assert not template_select.rule_matches({"tier": "1"}, ctx)
    assert not template_select.rule_matches({"tier": "*"}, ctx)


def test_severity_and_alertname_come_from_the_labels_unsynthesised():
    ctx = template_select.match_context(
        make_alert(labels={"severity": "critical", "alertname": "PostgresqlDown"})
    )
    assert ctx["severity"] == "critical"
    assert ctx["alertname"] == "PostgresqlDown"


def test_status_is_present_though_it_is_never_a_label():
    ctx = template_select.match_context(make_alert(status=AlertStatus.resolved))
    assert ctx["status"] == "resolved"


def test_team_is_surfaced_from_attribution():
    ctx = template_select.match_context(
        make_alert(), team=Team(id=1, key="payments", name="Payments")
    )
    assert ctx["team"] == "payments"
    assert ctx["resolved_team"] == "payments"


# ---------------------------------------------------------------------------
# matching semantics, kept identical to label rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "match,labels,expected",
    [
        ({}, {"a": "1"}, False),                      # empty matches nothing
        ({"a": "1"}, {"a": "1"}, True),
        ({"a": "1", "b": "2"}, {"a": "1"}, False),    # all must match
        ({"a": "1", "b": "2"}, {"a": "1", "b": "2"}, True),
        ({"A": "X"}, {"a": "x"}, True),               # case-insensitive
        ({"a": "pg-prod-*"}, {"a": "pg-prod-01"}, True),
        ({"a": "pg-prod-*"}, {"a": "pg-dev-01"}, False),
        ({"a": "1"}, {}, False),                      # missing key
        ({"a": "100%"}, {"a": "100%"}, True),         # % is literal
    ],
)
def test_matching_agrees_with_label_rules(match, labels, expected):
    """Two implementations, deliberately separate, that must behave the same.
    Operators learn one syntax; if these ever diverge the rules page would be
    teaching a lie about the template page."""
    assert template_select.rule_matches(match, labels) is expected
    assert _rule_matches(match, labels) is expected


# ---------------------------------------------------------------------------
# priority
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lowest_priority_number_wins(session):
    await _template(session, "default", '{"d":1}', default=True)
    loud = await _template(session, "loud", '{"loud":1}')
    quiet = await _template(session, "quiet", '{"quiet":1}')
    await _rule(session, quiet, {"severity": "critical"}, priority=50)
    await _rule(session, loud, {"severity": "critical"}, priority=1)
    await session.commit()

    body, _ = await template_select.select_source(
        session, make_alert(labels={"severity": "critical"}), MSTEAMS
    )
    assert body == loud.body


@pytest.mark.asyncio
async def test_equal_priority_is_broken_by_id_not_by_chance(session):
    await _template(session, "default", '{"d":1}', default=True)
    first = await _template(session, "first", '{"first":1}')
    second = await _template(session, "second", '{"second":1}')
    await _rule(session, first, {"severity": "critical"}, priority=10)
    await _rule(session, second, {"severity": "critical"}, priority=10)
    await session.commit()

    for _ in range(3):
        body, _n = await template_select.select_source(
            session, make_alert(labels={"severity": "critical"}), MSTEAMS
        )
        assert body == first.body


@pytest.mark.asyncio
async def test_disabled_rules_are_skipped(session):
    default = await _template(session, "default", '{"d":1}', default=True)
    loud = await _template(session, "loud", '{"loud":1}')
    await _rule(session, loud, {"severity": "critical"}, priority=1, enabled=False)
    await session.commit()

    body, _ = await template_select.select_source(
        session, make_alert(labels={"severity": "critical"}), MSTEAMS
    )
    assert body == default.body


@pytest.mark.asyncio
async def test_a_tier_rule_picks_the_loud_card(session):
    """The whole point of the feature, end to end."""
    await _template(session, "default", '{"d":1}', default=True)
    loud = await _template(session, "tier1", '{"tier1":1}')
    await _rule(session, loud, {"severity": "critical", "tier": "1"}, priority=1)
    await session.commit()

    tier1 = Service(id=1, key="payments-api", name="Payments", tier=1)
    tier3 = Service(id=2, key="batch", name="Batch", tier=3)
    crit = make_alert(labels={"severity": "critical"})

    hit, _ = await template_select.select_source(session, crit, MSTEAMS, service=tier1)
    miss, _ = await template_select.select_source(session, crit, MSTEAMS, service=tier3)
    assert hit == loud.body
    assert miss == '{"d":1}'


@pytest.mark.asyncio
async def test_explain_reports_the_context_and_the_winner(session):
    await _template(session, "default", '{"d":1}', default=True)
    loud = await _template(session, "loud", '{"loud":1}')
    await _rule(session, loud, {"severity": "critical"}, priority=1)
    await session.commit()

    out = await template_select.explain(
        session, make_alert(labels={"severity": "critical"}), MSTEAMS
    )
    assert out["context"]["severity"] == "critical"
    assert out["winner"].name == "loud"
    assert out["effective"].name == "loud"
    assert out["rules"][0]["matched"] is True
