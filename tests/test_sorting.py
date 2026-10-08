"""Table ordering: safety, stability and sensible defaults.

The stability test is the valuable one. Sorting by a column full of ties and
paging with LIMIT/OFFSET is a classic way to show the same row twice and lose
another, and it does not reproduce reliably -- so the invariant is asserted
directly rather than left to be noticed in production.
"""

import os
from datetime import UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.pagination import PAGE_SIZES, paginate, resolve_size
from app.api.sorting import ASC, DESC, SortSpec, severity_rank
from app.db import Base
from app.models import Alert, AlertStatus

DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opstrail:opstrail@localhost:5432/opstrail_test",
)

SPEC = SortSpec(
    columns={
        "started": Alert.starts_at,
        "severity": severity_rank(Alert.severity),
        "duration": Alert.ends_at - Alert.starts_at,
    },
    default="started",
    tiebreaker=Alert.id,
    default_desc=True,
)


# ---------------------------------------------------------------------------
# resolution: nothing from the URL reaches SQL unchecked
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        None, "", "nope", "1;--", "; DROP TABLE alerts--",
        "starts_at", "alerts.id", "(SELECT 1)",
    ],
)
def test_unknown_sort_key_falls_back_to_default(bad):
    """A column name is never taken from the URL: an unknown key must land on
    the page default instead of reaching order_by()."""
    assert SPEC.resolve(bad, DESC).key == "started"


def test_known_key_is_honoured():
    assert SPEC.resolve("severity", ASC).key == "severity"
    assert SPEC.resolve("severity", ASC).desc is False
    assert SPEC.resolve("severity", DESC).desc is True


def test_unknown_direction_uses_the_column_default():
    assert SPEC.resolve("severity", "sideways").desc is True
    assert SPEC.resolve("severity", None).desc is True


def test_clicking_the_active_column_flips_it():
    s = SPEC.resolve("severity", DESC)
    assert s.next_direction("severity") == ASC
    assert SPEC.resolve("severity", ASC).next_direction("severity") == DESC


def test_clicking_another_column_starts_descending():
    """'Newest/biggest first' is what people expect from a fresh click."""
    assert SPEC.resolve("severity", ASC).next_direction("started") == DESC


def test_order_by_always_ends_with_the_primary_key():
    """Without this the sequence is not total and paging can repeat rows."""
    clauses = SPEC.resolve("severity", DESC).clauses()
    assert len(clauses) == 2
    assert "alerts.id" in str(clauses[-1])


@pytest.mark.parametrize(
    "requested,expected", [(25, 25), (50, 50), (100, 100), (7, 50), (9999, 50), (None, 50), (0, 50)]
)
def test_page_size_whitelist(requested, expected):
    assert resolve_size(requested, 50) == expected
    assert set(PAGE_SIZES) == {25, 50, 100}


# ---------------------------------------------------------------------------
# behaviour against a real database
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


async def _seed(session, n=120):
    from datetime import datetime, timedelta

    base = datetime.now(UTC) - timedelta(days=1)
    sevs = ["critical", "error", "warning", "info"]
    for i in range(n):
        a = Alert(
            fingerprint=f"s{i}",
            starts_at=base + timedelta(minutes=i),
            status=AlertStatus.resolved,
            alertname="X",
            severity=sevs[i % 4],          # heavy ties on purpose
            labels={},
            annotations={},
        )
        if i % 10:                          # every 10th stays firing -> NULL duration
            a.ends_at = a.starts_at + timedelta(minutes=i % 37 + 1)
        session.add(a)
    await session.commit()


async def test_paging_a_tie_heavy_sort_loses_nothing(session):
    """Every row appears exactly once across all pages.

    `severity` has four distinct values over 120 rows, so without the id
    tiebreaker the database is free to order ties differently per query and
    OFFSET would skip and repeat.
    """
    await _seed(session, 120)
    collected = []
    page = 1
    while True:
        pg = await paginate(
            session, select(Alert), page=page, size=25,
            spec=SPEC, sort_key="severity", sort_dir=DESC,
        )
        collected.extend(a.id for a in pg.items)
        if not pg.has_next:
            break
        page += 1

    assert len(collected) == 120
    assert len(set(collected)) == 120


async def test_severity_sorts_by_rank_not_alphabetically(session):
    """Alphabetically `critical` would come after `warning`, which is
    meaningless to whoever is on call."""
    await _seed(session, 40)
    pg = await paginate(
        session, select(Alert), page=1, size=40,
        spec=SPEC, sort_key="severity", sort_dir=ASC,
    )
    order = []
    for a in pg.items:
        if not order or order[-1] != a.severity:
            order.append(a.severity)
    assert order == ["critical", "error", "warning", "info"]


async def test_null_durations_sort_last(session):
    """A still-firing alert has no duration; it must not head a
    'longest first' list."""
    await _seed(session, 40)
    pg = await paginate(
        session, select(Alert), page=1, size=40,
        spec=SPEC, sort_key="duration", sort_dir=DESC,
    )
    durations = [a.ends_at for a in pg.items]
    assert durations[0] is not None
    assert durations[-1] is None


async def test_page_is_clamped_to_the_last_page(session):
    await _seed(session, 30)
    pg = await paginate(session, select(Alert), page=999, size=25, spec=SPEC)
    assert pg.page == pg.pages == 2
    assert len(pg.items) == 5
