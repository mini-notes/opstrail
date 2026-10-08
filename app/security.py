"""Password hashing and request authorisation.

Hashing uses the `bcrypt` package directly. `passlib`, the usual choice, is
unmaintained (last release 2020) and is broken against bcrypt 5.x: it reads
`bcrypt.__about__.__version__`, which was removed, and then raises
`ValueError: password cannot be longer than 72 bytes` on every hash call.

bcrypt itself truncates at 72 bytes, which silently makes two different long
passwords equivalent. We pre-hash with SHA-256 and base64-encode, so the input
to bcrypt is always 44 bytes and a long password keeps its full entropy.
"""

import base64
import hashlib

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User, UserRole

SESSION_USER_KEY = "uid"

# Role hierarchy: a higher role satisfies every requirement below it.
_ROLE_RANK = {UserRole.viewer: 0, UserRole.editor: 1, UserRole.admin: 2}


def _prehash(password: str) -> bytes:
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), hashed.encode("ascii"))
    except (ValueError, TypeError):
        # A malformed stored hash must read as "wrong password", never as a
        # 500 that tells an attacker the account exists.
        return False


class NotAuthenticated(HTTPException):
    """Raised when no session is present.

    Carries 401; an exception handler turns it into a redirect to /login for
    browser navigations so operators are not shown a raw JSON error.
    """

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise NotAuthenticated()
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        # Deactivating a user must log them out immediately, not at cookie
        # expiry -- that is the whole point of offboarding.
        request.session.clear()
        raise NotAuthenticated()
    return user


def require_role(minimum: UserRole):
    """Dependency factory: caller must hold `minimum` or higher."""

    async def _check(user: User = Depends(current_user)) -> User:
        if _ROLE_RANK[user.role] < _ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires role {minimum.value} or higher",
            )
        return user

    return _check
