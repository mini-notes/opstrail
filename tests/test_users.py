"""User administration guards.

The point of these is lockout prevention. An admin who demotes or disables
themselves cannot undo it with the session that did it, and an install with
zero usable admins can only be rescued by shelling into the container to run
the CLI. Each guard below is the difference between a mis-click and an
outage of the console itself.
"""

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.users import _active_admin_count
from app.db import Base
from app.models import User, UserRole
from app.security import hash_password, verify_password

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


def make_user(name: str, role: UserRole, active: bool = True) -> User:
    return User(
        username=name,
        password_hash=hash_password("placeholder-password"),
        role=role,
        is_active=active,
    )


async def test_counts_only_active_admins(session):
    session.add_all([
        make_user("a1", UserRole.admin),
        make_user("a2", UserRole.admin, active=False),   # disabled: does not count
        make_user("e1", UserRole.editor),                # wrong role
        make_user("v1", UserRole.viewer),
    ])
    await session.commit()
    assert await _active_admin_count(session) == 1


async def test_excluding_the_only_admin_leaves_none(session):
    """This is the check that stands between a mis-click and an install
    nobody can administer."""
    only = make_user("solo", UserRole.admin)
    session.add(only)
    await session.commit()
    assert await _active_admin_count(session) == 1
    assert await _active_admin_count(session, excluding=only.id) == 0


async def test_excluding_one_of_two_admins_still_leaves_one(session):
    a1, a2 = make_user("a1", UserRole.admin), make_user("a2", UserRole.admin)
    session.add_all([a1, a2])
    await session.commit()
    assert await _active_admin_count(session, excluding=a1.id) == 1


async def test_disabled_admin_does_not_keep_the_door_open(session):
    """A deactivated admin cannot log in, so it must not be counted as the
    one remaining admin -- otherwise the guard lets the last usable admin go."""
    active = make_user("a1", UserRole.admin)
    session.add_all([active, make_user("ghost", UserRole.admin, active=False)])
    await session.commit()
    assert await _active_admin_count(session, excluding=active.id) == 0


async def test_created_user_can_authenticate(session):
    """End-to-end on the hashing path the admin page uses."""
    u = User(
        username="new",
        password_hash=hash_password("a-real-password"),
        role=UserRole.editor,
    )
    session.add(u)
    await session.commit()

    stored = await session.get(User, u.id)
    assert verify_password("a-real-password", stored.password_hash)
    assert not verify_password("a-real-password ", stored.password_hash)
