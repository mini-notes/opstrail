"""Applying label rules to alerts that are already stored.

Two properties matter most and both are easy to get wrong:

* a manual tag is an operator decision and must survive a backfill;
* `payload` attribution beats `rule` attribution, even against a rule with
  the highest priority -- an alert whose `job` label happens to equal a
  service key is claimed before any rule is consulted.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Alert, AlertStatus, LabelRule, LabelSource, Service, Team
from app.services import backfill

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)


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


def alert(fp: str, labels: dict, *, source=None, service_id=None) -> Alert:
    return Alert(
        fingerprint=fp,
        starts_at=datetime.now(UTC) - timedelta(hours=1),
        status=AlertStatus.firing,
        alertname=labels.get("alertname", "X"),
        severity=labels.get("severity"),
        labels=labels,
        annotations={},
        label_source=source,
        service_id=service_id,
    )


async def _fixtures(session):
    team = Team(key="db", name="DB Team")
    session.add(team)
    await session.flush()
    pg = Service(key="postgres-main", name="Postgres", team_id=team.id)
    other = Service(key="checkout-api", name="Checkout", team_id=team.id)
    session.add_all([pg, other])
    await session.flush()
    return pg, other, team


async def test_wildcard_rule_labels_matching_history(session):
    pg, _, _ = await _fixtures(session)
    session.add(LabelRule(priority=10, match={"instance": "pg-prod-*"}, service_id=pg.id))
    session.add_all([
        alert("a", {"alertname": "Down", "instance": "pg-prod-01"}),
        alert("b", {"alertname": "Down", "instance": "pg-prod-02"}),
        alert("c", {"alertname": "Down", "instance": "pg-stg-01"}),   # no match
    ])
    await session.commit()

    pre = await backfill.preview(session)
    assert pre.matched == 2
    assert pre.unmatched == 1

    assert await backfill.apply(session) == 2
    rows = {a.fingerprint: a for a in (await session.execute(select(Alert))).scalars().all()}
    assert rows["a"].label_source is LabelSource.rule
    assert rows["a"].service_id == pg.id
    assert rows["c"].label_source is None


async def test_manual_labels_are_never_overwritten(session):
    """The whole point of tagging by hand is that it sticks."""
    pg, other, _ = await _fixtures(session)
    session.add(LabelRule(priority=10, match={"instance": "pg-prod-*"}, service_id=pg.id))
    session.add(
        alert("manual", {"instance": "pg-prod-01"},
              source=LabelSource.manual, service_id=other.id)
    )
    await session.commit()

    assert (await backfill.preview(session)).matched == 0
    assert await backfill.apply(session) == 0

    kept = (await session.execute(select(Alert))).scalars().one()
    assert kept.label_source is LabelSource.manual
    assert kept.service_id == other.id


async def test_payload_attribution_beats_even_priority_one_rule(session):
    """A service keyed `node-exporter` claims every alert carrying
    `job=node-exporter`, before any rule runs. This surprises people, so it
    is pinned here as deliberate behaviour."""
    pg, _, _ = await _fixtures(session)
    node = Service(key="node-exporter", name="Node Exporter")
    session.add(node)
    await session.flush()
    session.add(LabelRule(priority=1, match={"alertname": "Trap"}, service_id=pg.id))
    session.add(alert("trap", {"alertname": "Trap", "job": "node-exporter"}))
    await session.commit()

    await backfill.apply(session)
    row = (await session.execute(select(Alert))).scalars().one()
    assert row.service_id == node.id, "payload attribution must win"


async def test_lowest_priority_number_wins(session):
    pg, other, _ = await _fixtures(session)
    session.add(LabelRule(priority=50, match={"alertname": "Both"}, service_id=other.id))
    session.add(LabelRule(priority=5, match={"alertname": "Both"}, service_id=pg.id))
    session.add(alert("both", {"alertname": "Both"}))
    await session.commit()

    await backfill.apply(session)
    row = (await session.execute(select(Alert))).scalars().one()
    assert row.service_id == pg.id


async def test_disabled_rule_is_ignored(session):
    pg, _, _ = await _fixtures(session)
    session.add(
        LabelRule(priority=10, match={"alertname": "Off"}, service_id=pg.id, enabled=False)
    )
    session.add(alert("off", {"alertname": "Off"}))
    await session.commit()

    assert (await backfill.preview(session)).matched == 0
    assert await backfill.apply(session) == 0


async def test_apply_terminates_when_nothing_matches(session):
    """Unmatched alerts stay NULL. A re-query loop would hand back the same
    rows for ever, so apply() walks a primary-key cursor instead."""
    await _fixtures(session)
    session.add_all([alert(f"n{i}", {"alertname": "NoRule"}) for i in range(5)])
    await session.commit()

    assert await backfill.apply(session) == 0
    assert (await backfill.preview(session)).unlabelled_total == 5
