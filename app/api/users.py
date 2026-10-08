"""User administration. Admin only.

Lockout protection is the point of most of the code here. Without it a single
mis-click -- demoting yourself, deactivating the last admin -- leaves nobody
able to administer the system, and recovery means shelling into the container
to run `python -m app.cli`. Every destructive path is therefore checked
against "would this leave zero usable admins?" before it is allowed.
"""

import logging

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import ADMIN_PAGE_SIZE, paginate, resolve_size
from app.api.params import OptInt, OptStr
from app.api.sorting import SortSpec
from app.db import get_session
from app.models import User, UserRole
from app.security import hash_password, require_role
from app.services import audit
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["users"], include_in_schema=False)

MIN_PASSWORD_LEN = 8

USER_SORTS = SortSpec(
    columns={
        "username": User.username,
        "role": User.role,
        "status": User.is_active,
        "created": User.created_at,
    },
    default="username",
    tiebreaker=User.id,
    default_desc=False,
)


async def _active_admin_count(session: AsyncSession, *, excluding: int | None = None) -> int:
    stmt = select(func.count(User.id)).where(
        User.role == UserRole.admin, User.is_active.is_(True)
    )
    if excluding is not None:
        stmt = stmt.where(User.id != excluding)
    return (await session.execute(stmt)).scalar_one()


async def _render(
    request: Request, session: AsyncSession, actor: User,
    *, error=None, ok=None, page: int = 1,
    size=None, sort_key=None, sort_dir=None,
):
    pg = await paginate(
        session, select(User),
        page=page, size=resolve_size(size, ADMIN_PAGE_SIZE),
        spec=USER_SORTS, sort_key=sort_key, sort_dir=sort_dir,
    )
    return templates.TemplateResponse(
        request,
        "ui/users.html",
        {
            "user": actor,
            "page_obj": pg,
            "roles": list(UserRole),
            "error": error,
            "ok": ok,
            "admin_count": await _active_admin_count(session),
        },
        status_code=status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK,
    )


@router.get("/users")
async def users_page(
    request: Request,
    page: int = Query(1, ge=1),
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_role(UserRole.admin)),
):
    return await _render(
        request, session, actor, page=page, size=size, sort_key=sort, sort_dir=dir
    )


@router.post("/users")
async def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
    full_name: str = Form(""),
    email: str = Form(""),
    upn: str = Form(""),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_role(UserRole.admin)),
):
    username = username.strip()
    if not username:
        return await _render(request, session, actor, error="Username is required")
    if len(password) < MIN_PASSWORD_LEN:
        return await _render(
            request, session, actor,
            error=f"Password must be at least {MIN_PASSWORD_LEN} characters",
        )
    exists = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if exists:
        return await _render(request, session, actor, error=f"User {username!r} already exists")

    user = User(
        username=username,
        full_name=full_name.strip() or None,
        email=email.strip() or None,
        upn=upn.strip() or None,
        password_hash=hash_password(password),
        role=UserRole(role),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    await audit.record(
        session, actor, "user.create",
        object_type="user", object_id=user.id, detail={"username": username, "role": role},
    )
    await session.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/role")
async def change_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_role(UserRole.admin)),
):
    target = await session.get(User, user_id)
    if target is None:
        return await _render(request, session, actor, error="No such user")

    new_role = UserRole(role)
    if target.role == new_role:
        return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)

    # You cannot demote yourself: the session you are using would lose the
    # very permission needed to undo the mistake.
    if target.id == actor.id and new_role is not UserRole.admin:
        return await _render(
            request, session, actor,
            error="You cannot change your own role. Ask another admin.",
        )
    # Nor demote the last remaining admin.
    if (
        target.role is UserRole.admin
        and new_role is not UserRole.admin
        and await _active_admin_count(session, excluding=target.id) == 0
    ):
        return await _render(
            request, session, actor,
            error="This is the last active admin — promote someone else first.",
        )

    old = target.role.value
    target.role = new_role
    await audit.record(
        session, actor, "user.role",
        object_type="user", object_id=target.id,
        detail={"username": target.username, "from": old, "to": new_role.value},
    )
    await session.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/active")
async def toggle_active(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_role(UserRole.admin)),
):
    target = await session.get(User, user_id)
    if target is None:
        return await _render(request, session, actor, error="No such user")

    if target.id == actor.id:
        return await _render(
            request, session, actor, error="You cannot deactivate your own account."
        )
    if (
        target.is_active
        and target.role is UserRole.admin
        and await _active_admin_count(session, excluding=target.id) == 0
    ):
        return await _render(
            request, session, actor,
            error="This is the last active admin — promote someone else first.",
        )

    target.is_active = not target.is_active
    await audit.record(
        session, actor, "user.active",
        object_type="user", object_id=target.id,
        detail={"username": target.username, "is_active": target.is_active},
    )
    await session.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/password")
async def reset_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_role(UserRole.admin)),
):
    target = await session.get(User, user_id)
    if target is None:
        return await _render(request, session, actor, error="No such user")
    if len(password) < MIN_PASSWORD_LEN:
        return await _render(
            request, session, actor,
            error=f"Password must be at least {MIN_PASSWORD_LEN} characters",
        )

    target.password_hash = hash_password(password)
    # Never log the password itself, only that it changed.
    await audit.record(
        session, actor, "user.password",
        object_type="user", object_id=target.id, detail={"username": target.username},
    )
    await session.commit()
    return await _render(
        request, session, actor, ok=f"Password updated for {target.username}"
    )
