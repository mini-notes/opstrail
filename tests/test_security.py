"""Password hashing and role checks.

These exist because the obvious choice -- passlib -- is broken against
bcrypt 5.x (it reads the removed `bcrypt.__about__` and then raises on every
hash). The suite pins the behaviour of the replacement so a future dependency
bump cannot silently reintroduce the problem.
"""

import pytest
from fastapi import HTTPException

from app.models import UserRole
from app.security import _ROLE_RANK, hash_password, verify_password


def test_hash_then_verify():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)


def test_wrong_password_rejected():
    assert not verify_password("nope", hash_password("secret"))


def test_same_password_hashes_differently():
    """Distinct salts; identical hashes would leak which users share a password."""
    assert hash_password("secret") != hash_password("secret")


def test_long_password_keeps_full_entropy():
    """bcrypt truncates at 72 bytes. Pre-hashing means two long passwords
    sharing a 72-byte prefix must NOT be interchangeable."""
    base = "x" * 100
    h = hash_password(base + "AAAA")
    assert verify_password(base + "AAAA", h)
    assert not verify_password(base + "BBBB", h)


def test_unicode_password():
    pw = "mật-khẩu-🔥-2026"
    assert verify_password(pw, hash_password(pw))


def test_malformed_stored_hash_reads_as_wrong_password():
    """Never a 500: a corrupt row must not tell an attacker the user exists."""
    assert not verify_password("anything", "not-a-bcrypt-hash")
    assert not verify_password("anything", "")


def test_role_hierarchy():
    assert _ROLE_RANK[UserRole.admin] > _ROLE_RANK[UserRole.editor]
    assert _ROLE_RANK[UserRole.editor] > _ROLE_RANK[UserRole.viewer]


@pytest.mark.parametrize(
    "role,minimum,allowed",
    [
        (UserRole.viewer, UserRole.viewer, True),
        (UserRole.viewer, UserRole.editor, False),
        (UserRole.editor, UserRole.editor, True),
        (UserRole.editor, UserRole.admin, False),
        (UserRole.admin, UserRole.editor, True),
        (UserRole.admin, UserRole.admin, True),
    ],
)
def test_require_role_matrix(role, minimum, allowed):
    from app.security import require_role

    class FakeUser:
        pass

    user = FakeUser()
    user.role = role
    checker = require_role(minimum)

    import asyncio

    if allowed:
        assert asyncio.run(checker(user=user)) is user
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(checker(user=user))
        assert exc.value.status_code == 403
