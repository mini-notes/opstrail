"""Who gets @mentioned, and how the Teams mention block is built.

The two properties that matter:

* the threshold is compared by SEVERITY RANK, not alphabetically -- string
  comparison puts "critical" below "warning" and would mention on the wrong
  alerts;
* the `<at>…</at>` token in the TextBlock and the `text` in
  `msteams.entities` must match character for character, or Teams renders the
  raw markup instead of a highlighted name.
"""

import os
from datetime import UTC

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import (
    Alert,
    AlertStatus,
    Owner,
    OwnerRole,
    Service,
    ServiceOwner,
    Team,
    TeamOwner,
)
from app.services import mention

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)


# ---------------------------------------------------------------------------
# threshold arithmetic (no database)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "severity,threshold,expected",
    [
        ("critical", "critical", True),
        ("error", "critical", False),
        ("critical", "error", True),     # worse than the bar -> mention
        ("error", "error", True),
        ("warning", "error", False),
        ("info", "info", True),
        ("critical", "info", True),
        ("warning", "critical", False),
    ],
)
def test_threshold_uses_rank_not_alphabet(severity, threshold, expected):
    """Alphabetically "critical" < "warning", which would invert the whole
    feature."""
    assert mention.meets_threshold(severity, threshold) is expected


def test_unknown_severity_never_reaches_a_real_threshold():
    assert not mention.meets_threshold("banana", "critical")
    assert not mention.meets_threshold(None, "critical")


def test_absent_threshold_means_never_mention():
    assert not mention.meets_threshold("critical", None)
    assert not mention.meets_threshold("critical", "")


def test_aliases_are_ranked():
    assert mention.meets_threshold("p1", "critical")
    assert mention.meets_threshold("fatal", "critical")
    assert not mention.meets_threshold("p3", "critical")


# ---------------------------------------------------------------------------
# entity construction (no database)
# ---------------------------------------------------------------------------

def _owner(id_, name, upn=None, email=None, aad=None):
    return Owner(id=id_, name=name, upn=upn, email=email, aad_object_id=aad, is_active=True)


def test_entities_and_text_match_exactly():
    """Teams joins the two by comparing `text`; any drift breaks the mention."""
    owners = [
        _owner(1, "Nguyen Van A", upn="a@c.com"),
        _owner(2, "Le Thi B", upn="b@c.com"),
    ]
    entities = mention.build_entities(owners)
    text = mention.build_text(entities)

    assert len(entities) == 2
    for e in entities:
        assert e["text"] in text


def test_owner_without_any_identifier_is_skipped():
    """A mention pointing at nothing renders as raw <at> markup."""
    entities = mention.build_entities([_owner(1, "No Contact")])
    assert entities == []


def test_identifier_falls_back_through_upn_email_aad():
    only_email = mention.build_entities([_owner(1, "A", email="a@c.com")])
    assert only_email[0]["mentioned"]["id"] == "a@c.com"
    only_aad = mention.build_entities([_owner(2, "B", aad="guid-1")])
    assert only_aad[0]["mentioned"]["id"] == "guid-1"


# ---------------------------------------------------------------------------
# owner resolution (needs a database)
# ---------------------------------------------------------------------------

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


async def _fixture(session, *, service_threshold=None, team_threshold="critical"):
    from datetime import datetime, timedelta

    team = Team(key="pay", name="Payments", mention_severity=team_threshold)
    session.add(team)
    await session.flush()
    svc = Service(
        key="api", name="API", team_id=team.id, mention_severity=service_threshold
    )
    session.add(svc)
    await session.flush()
    alert = Alert(
        fingerprint="f1",
        starts_at=datetime.now(UTC) - timedelta(minutes=5),
        status=AlertStatus.firing,
        alertname="X",
        severity="critical",
        labels={},
        annotations={},
        service_id=svc.id,
        team_id=team.id,
    )
    session.add(alert)
    await session.flush()
    return svc, team, alert


async def test_service_owners_win_over_team_owners(session):
    """Mentioning the whole team as well as the named owner is the fastest
    way to get the channel muted."""
    svc, team, alert = await _fixture(session)
    svc_owner = _owner(None, "Service Person", upn="svc@c.com")
    team_owner = _owner(None, "Team Person", upn="team@c.com")
    session.add_all([svc_owner, team_owner])
    await session.flush()
    session.add(ServiceOwner(service_id=svc.id, owner_id=svc_owner.id, role=OwnerRole.primary))
    session.add(TeamOwner(team_id=team.id, owner_id=team_owner.id, role=OwnerRole.primary))
    await session.commit()

    plan = await mention.plan_for(session, alert)
    assert plan.source == "service"
    assert [o.name for o in plan.owners] == ["Service Person"]


async def test_falls_back_to_team_owners(session):
    svc, team, alert = await _fixture(session)
    team_owner = _owner(None, "Team Person", upn="team@c.com")
    session.add(team_owner)
    await session.flush()
    session.add(TeamOwner(team_id=team.id, owner_id=team_owner.id, role=OwnerRole.primary))
    await session.commit()

    plan = await mention.plan_for(session, alert)
    assert plan.source == "team"
    assert plan.should_mention is True


async def test_no_owners_means_no_mention_but_not_an_error(session):
    """An install with an empty directory must still deliver its alerts."""
    _, _, alert = await _fixture(session)
    await session.commit()

    plan = await mention.plan_for(session, alert)
    assert plan.owners == []
    assert plan.should_mention is False


async def test_service_threshold_overrides_the_team(session):
    svc, team, alert = await _fixture(session, service_threshold="error")
    owner = _owner(None, "A", upn="a@c.com")
    session.add(owner)
    await session.flush()
    session.add(ServiceOwner(service_id=svc.id, owner_id=owner.id))
    alert.severity = "error"
    await session.commit()

    plan = await mention.plan_for(session, alert)
    assert plan.threshold == "error"
    assert plan.should_mention is True


async def test_inactive_owner_is_not_mentioned(session):
    svc, _, alert = await _fixture(session)
    owner = _owner(None, "Left The Company", upn="gone@c.com")
    owner.is_active = False
    session.add(owner)
    await session.flush()
    session.add(ServiceOwner(service_id=svc.id, owner_id=owner.id))
    await session.commit()

    plan = await mention.plan_for(session, alert)
    assert plan.owners == []
    assert plan.should_mention is False
