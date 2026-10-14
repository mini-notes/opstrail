"""One severity table, three readers, and the proof they agree.

This file exists because they did not. Before app/services/severity.py there
were three independent tables -- the mention threshold, the card banding and
the SQL sort order -- and 8 of 18 aliases disagreed. The worst case was real
and silent: an alerting rule writing `severity: sev1` produced a CRITICAL
card and mentioned nobody, because the threshold table had never heard of
`sev1` and ranked it below `info`.

So the properties pinned here are not about any one function. They are about
the three staying in step.
"""

import os

import pytest
from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import severity

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)


@pytest.fixture
async def session():
    engine = create_async_engine(DSN)
    try:
        async with engine.connect() as conn:
            await conn.execute(select(literal(1)))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no test database at {DSN}: {exc}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# the three readers agree
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_python_and_sql_rank_every_alias_identically(session):
    """The SQL ordering is generated from the same table, but generated code
    can still be wrong. Ask Postgres about every alias and compare."""
    mismatches = []
    for alias in severity.ALIASES:
        in_sql = (
            await session.execute(select(severity.sql_rank(literal(alias))))
        ).scalar_one()
        if in_sql != severity.rank(alias):
            mismatches.append((alias, severity.rank(alias), in_sql))
    assert not mismatches, f"python vs SQL: {mismatches}"


@pytest.mark.asyncio
async def test_sql_rank_folds_case_and_whitespace_like_python(session):
    for raw in (" CRITICAL ", "Critical", "cRiTiCaL"):
        in_sql = (
            await session.execute(select(severity.sql_rank(literal(raw))))
        ).scalar_one()
        assert in_sql == severity.rank(raw) == 1


@pytest.mark.asyncio
async def test_unknown_values_sort_last_in_both(session):
    in_sql = (
        await session.execute(select(severity.sql_rank(literal("banana"))))
    ).scalar_one()
    assert in_sql == severity.rank("banana") == severity.UNKNOWN_RANK


def test_banding_and_rank_never_disagree():
    """A value that bands into CRITICAL must rank as CRITICAL. This is the
    exact pairing that broke: `sev1` banded CRITICAL and ranked unknown, so
    the card was red and nobody was called."""
    for alias, expected_band in severity.ALIASES.items():
        assert severity.band(alias) == expected_band
        assert severity.rank(alias) == severity.BANDS.index(expected_band) + 1


@pytest.mark.parametrize(
    "alias", ["sev1", "p1", "crit", "emerg", "disaster", "alarm", "fatal"]
)
def test_every_spelling_of_the_worst_thing_wakes_someone(alias):
    """Regression for the shipped bug. Each of these used to rank below
    `info` in at least one of the three tables."""
    assert severity.rank(alias) == 1
    assert severity.band(alias) == "CRITICAL"
    assert severity.meets_threshold(alias, "critical")


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,threshold,expected",
    [
        ("critical", "critical", True),
        ("critical", "warning", True),
        ("warning", "critical", False),
        ("info", "info", True),
        ("warning", "info", True),
        # Alphabetically "critical" < "warning", which is the opposite of
        # what an operator means; this is why comparison is by rank.
        ("critical", "info", True),
        ("banana", "info", False),
        ("critical", None, False),
        (None, "info", False),
    ],
)
def test_threshold_is_compared_by_rank_not_as_strings(value, threshold, expected):
    assert severity.meets_threshold(value, threshold) is expected


# ---------------------------------------------------------------------------
# shift: the guard rails a rule cannot break
# ---------------------------------------------------------------------------

def test_the_sign_follows_how_operators_speak():
    """"Raise by one band" is shift(v, 1). A caller having to flip the sign
    is how a quieting rule becomes an escalating one."""
    assert severity.shift("warning", 1) == "ERROR"
    assert severity.shift("warning", -1) == "INFO"


def test_nothing_can_be_pushed_past_the_ends():
    assert severity.shift("critical", 5) == "CRITICAL"
    # The one that matters: a rule quieting an alert out of hours must never
    # be able to push it below INFO into nothing at all.
    assert severity.shift("critical", -5) == "INFO"
    assert severity.shift("info", -99) == "INFO"


def test_an_unrecognised_severity_has_no_band_to_move():
    assert severity.shift("banana", 1) is None
    assert severity.shift(None, 1) is None


def test_band_passes_unknown_values_through_rather_than_inventing_one():
    """Showing the operator the word their rule actually wrote beats
    guessing which band it belongs to."""
    assert severity.band("banana") == "BANANA"
    assert severity.band("") == ""
