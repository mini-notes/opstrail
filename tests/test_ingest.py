"""Intake: the UPSERT, and who owns an alert's label over time.

This file exists because the UPSERT had no tests at all, while being the
thing that decides whether a repeat notification updates a row or multiplies
it, and whether a hand-written label survives.

The rule it pins, in one line:

    manual  >  rule, but payload  >  manual

A manual tag is a patch applied while the source could not say who owns an
alert. Once the alert's own labels resolve, the source takes over -- it is
the system of record. A `rule` match never takes over, because a rule is
OpsTrail guessing too, and a guess must not overwrite a person.
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import (
    Alert,
    AuditLog,
    LabelRule,
    LabelSource,
    Service,
    Team,
)
from app.schemas.alertmanager import AlertmanagerPayload
from app.services.ingest import ingest_payload

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)
STARTS = datetime(2026, 10, 7, 8, 30, tzinfo=UTC)


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


def payload(*, labels: dict, fingerprint="fp1", status="firing",
            starts=STARTS, summary="Postgres down") -> AlertmanagerPayload:
    return AlertmanagerPayload.model_validate({
        "receiver": "opstrail",
        "status": status,
        "alerts": [{
            "status": status,
            "labels": {"alertname": "PostgresqlDown", "severity": "critical",
                       **labels},
            "annotations": {"summary": summary},
            "startsAt": starts.isoformat(),
            "fingerprint": fingerprint,
        }],
    })


async def row(session, fingerprint="fp1") -> Alert:
    # The UPSERT runs as Core SQL, so the session's identity map does not
    # learn about it. Dropping the map first makes the SELECT build a fresh
    # object from the database; without it, a row read back here is whatever
    # the test last set through the ORM, and the assertions would be checking
    # the test's own memory. (expunge, not expire: expiring leaves attribute
    # access to trigger lazy IO, which async SQLAlchemy refuses.)
    session.expunge_all()
    return (
        await session.execute(select(Alert).where(Alert.fingerprint == fingerprint))
    ).scalar_one()


async def actions(session) -> list[str]:
    return list(
        (await session.execute(select(AuditLog.action).order_by(AuditLog.id)))
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# the UPSERT itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_repeat_updates_the_row_instead_of_adding_one(session):
    """Alertmanager re-sends every repeat_interval. INSERT would turn one
    incident into dozens of rows and wreck every statistic built on them."""
    await ingest_payload(session, payload(labels={}))
    for _ in range(5):
        await ingest_payload(session, payload(labels={}, summary="updated"))

    all_rows = (await session.execute(select(Alert))).scalars().all()
    assert len(all_rows) == 1
    assert all_rows[0].summary == "updated"


@pytest.mark.asyncio
async def test_only_a_new_alert_or_a_state_change_is_worth_notifying(session):
    first = await ingest_payload(session, payload(labels={}))
    repeat = await ingest_payload(session, payload(labels={}))
    resolved = await ingest_payload(session, payload(labels={}, status="resolved"))

    assert len(first.notify_ids) == 1
    assert repeat.notify_ids == []
    assert len(resolved.notify_ids) == 1


@pytest.mark.asyncio
async def test_changing_a_label_at_the_source_makes_a_SECOND_row(session):
    """The identity key is Alertmanager's fingerprint, which is a hash of the
    label set. Editing the Prometheus rule to add `service=` therefore does
    not relabel the existing alert -- it creates a new one, and the old row
    keeps its old attribution for ever.

    This is the single most surprising thing about attribution over time, so
    it is pinned rather than left to be rediscovered.
    """
    await ingest_payload(session, payload(labels={}, fingerprint="before"))
    await ingest_payload(
        session, payload(labels={"service": "postgres-main"}, fingerprint="after")
    )

    assert len((await session.execute(select(Alert))).scalars().all()) == 2


# ---------------------------------------------------------------------------
# payload > rule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_alerts_own_label_beats_a_priority_one_rule(session):
    svc_payload = Service(key="postgres-main", name="Postgres")
    svc_rule = Service(key="other", name="Other")
    session.add_all([svc_payload, svc_rule])
    await session.flush()
    session.add(LabelRule(priority=1, match={"alertname": "PostgresqlDown"},
                          service_id=svc_rule.id))
    await session.commit()

    await ingest_payload(session, payload(labels={"service": "postgres-main"}))

    stored = await row(session)
    assert stored.service_id == svc_payload.id
    assert stored.label_source is LabelSource.payload


@pytest.mark.asyncio
async def test_a_rule_claims_an_alert_whose_own_label_resolves_to_nothing(session):
    """`service=unknown-thing` names no Service row, so payload attribution
    yields nothing and the rules do get to run. A rule cannot contradict a
    label that resolved; it can supply one that did not."""
    svc = Service(key="postgres-main", name="Postgres")
    session.add(svc)
    await session.flush()
    session.add(LabelRule(priority=10, match={"alertname": "PostgresqlDown"},
                          service_id=svc.id))
    await session.commit()

    await ingest_payload(session, payload(labels={"service": "unknown-thing"}))

    stored = await row(session)
    assert stored.service_id == svc.id
    assert stored.label_source is LabelSource.rule


# ---------------------------------------------------------------------------
# manual vs the rest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_manual_tag_survives_a_repeat_that_resolves_nothing(session):
    """Nothing in the payload names a service, so there is nothing to hand
    back to. The operator's decision stands."""
    svc = Service(key="chosen-by-hand", name="Chosen")
    session.add(svc)
    await session.flush()
    await ingest_payload(session, payload(labels={}))

    stored = await row(session)
    stored.service_id = svc.id
    stored.label_source = LabelSource.manual
    await session.commit()

    await ingest_payload(session, payload(labels={}))

    stored = await row(session)
    assert stored.service_id == svc.id
    assert stored.label_source is LabelSource.manual


@pytest.mark.asyncio
async def test_a_manual_tag_still_beats_a_rule(session):
    """A rule is OpsTrail guessing. It does not get to overwrite a person."""
    by_hand = Service(key="chosen-by-hand", name="Chosen")
    by_rule = Service(key="claimed-by-rule", name="Rule")
    session.add_all([by_hand, by_rule])
    await session.flush()
    session.add(LabelRule(priority=1, match={"alertname": "PostgresqlDown"},
                          service_id=by_rule.id))
    await ingest_payload(session, payload(labels={}))

    stored = await row(session)
    stored.service_id = by_hand.id
    stored.label_source = LabelSource.manual
    await session.commit()

    await ingest_payload(session, payload(labels={}))

    stored = await row(session)
    assert stored.service_id == by_hand.id
    assert stored.label_source is LabelSource.manual


@pytest.mark.asyncio
async def test_the_source_takes_a_manual_tag_back_once_it_resolves(session):
    """The case this change exists for.

    The alert always carried `service=postgres-main`, but no Service row had
    that key, so it arrived unlabelled and somebody tagged it by hand. When
    the service is finally declared, the payload resolves and the source --
    the system of record -- takes over.
    """
    stopgap = Service(key="stopgap", name="Stopgap")
    session.add(stopgap)
    await session.flush()
    await ingest_payload(session, payload(labels={"service": "postgres-main"}))

    stored = await row(session)
    assert stored.label_source is None  # nothing resolved yet
    stored.service_id = stopgap.id
    stored.label_source = LabelSource.manual
    await session.commit()

    real = Service(key="postgres-main", name="Postgres")
    session.add(real)
    await session.commit()

    await ingest_payload(session, payload(labels={"service": "postgres-main"}))

    stored = await row(session)
    assert stored.service_id == real.id
    assert stored.label_source is LabelSource.payload


@pytest.mark.asyncio
async def test_the_takeover_is_recorded(session):
    """Nobody performed this change, which is exactly why it has to be
    written down: an attribution that moves on its own is otherwise
    indistinguishable from a bug."""
    stopgap = Service(key="stopgap", name="Stopgap")
    session.add(stopgap)
    await session.flush()
    await ingest_payload(session, payload(labels={"service": "postgres-main"}))

    stored = await row(session)
    stored.service_id = stopgap.id
    stored.label_source = LabelSource.manual
    await session.commit()

    session.add(Service(key="postgres-main", name="Postgres"))
    await session.commit()
    await ingest_payload(session, payload(labels={"service": "postgres-main"}))

    assert "alert.label.superseded" in await actions(session)


# ---------------------------------------------------------------------------
# rules changing underneath a live alert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deleting_a_rule_de_labels_a_still_firing_alert_and_says_so(session):
    """Defensible -- the row is re-resolved from scratch -- but it makes the
    attributed count drop with nothing to explain it, so it is audited."""
    svc = Service(key="claimed", name="Claimed")
    session.add(svc)
    await session.flush()
    rule = LabelRule(priority=10, match={"alertname": "PostgresqlDown"},
                     service_id=svc.id)
    session.add(rule)
    await session.commit()

    await ingest_payload(session, payload(labels={}))
    assert (await row(session)).label_source is LabelSource.rule

    await session.delete(rule)
    await session.commit()
    await ingest_payload(session, payload(labels={}))

    stored = await row(session)
    assert stored.label_source is None
    assert stored.service_id is None
    assert "alert.label.lost" in await actions(session)


@pytest.mark.asyncio
async def test_adding_a_rule_retro_labels_an_alert_still_being_re_sent(session):
    """The reason auto-resolved labels are refreshed on every repeat."""
    await ingest_payload(session, payload(labels={}))
    assert (await row(session)).label_source is None

    svc = Service(key="late", name="Late")
    session.add(svc)
    await session.flush()
    session.add(LabelRule(priority=10, match={"alertname": "PostgresqlDown"},
                          service_id=svc.id))
    await session.commit()

    await ingest_payload(session, payload(labels={}))
    assert (await row(session)).service_id == svc.id


@pytest.mark.asyncio
async def test_a_team_label_resolves_alongside_the_service(session):
    team = Team(key="platform", name="Platform")
    session.add(team)
    await session.flush()
    svc = Service(key="postgres-main", name="Postgres", team_id=team.id)
    session.add(svc)
    await session.commit()

    await ingest_payload(session, payload(labels={"service": "postgres-main"}))

    stored = await row(session)
    assert stored.team_id == team.id


# ---------------------------------------------------------------------------
# deleting a service must not strand its alerts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alerts_return_to_the_backlog_when_their_service_is_deleted(session):
    """`service_id` is ON DELETE SET NULL while `label_source` is untouched,
    so without the rescue these rows read `manual` while pointing at nothing
    -- invisible to Unlabelled and to backfill, which both select on
    `label_source IS NULL`. They would sit there for ever."""
    from app.api.labels import _rescue_orphans

    svc = Service(key="doomed", name="Doomed")
    session.add(svc)
    await session.flush()
    await ingest_payload(session, payload(labels={}))

    stored = await row(session)
    stored.service_id = svc.id
    stored.label_source = LabelSource.manual
    await session.commit()

    await session.delete(svc)
    await session.flush()

    assert await _rescue_orphans(session) == 1
    await session.commit()

    stored = await row(session)
    assert stored.label_source is None
    assert stored.service_id is None


@pytest.mark.asyncio
async def test_the_rescue_leaves_alerts_that_still_have_a_team(session):
    """Losing a service but keeping a team is still an attribution."""
    from app.api.labels import _rescue_orphans

    team = Team(key="platform", name="Platform")
    session.add(team)
    await session.flush()
    svc = Service(key="doomed", name="Doomed", team_id=team.id)
    session.add(svc)
    await session.flush()
    await ingest_payload(session, payload(labels={}))

    stored = await row(session)
    stored.service_id = svc.id
    stored.team_id = team.id
    stored.label_source = LabelSource.manual
    await session.commit()

    await session.delete(svc)
    await session.flush()

    assert await _rescue_orphans(session) == 0
    await session.commit()

    stored = await row(session)
    assert stored.label_source is LabelSource.manual
    assert stored.team_id == team.id
