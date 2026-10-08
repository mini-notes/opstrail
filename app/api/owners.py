"""Owner directory and assignment.

Owners are who gets @mentioned when something breaks. They are kept separate
from `users` on purpose -- see the docstring on the model.
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
from app.models import (
    Owner,
    OwnerRole,
    Service,
    ServiceOwner,
    Team,
    TeamOwner,
    User,
    UserRole,
)
from app.security import current_user, require_role
from app.services import audit
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/labels/owners", tags=["owners"], include_in_schema=False)

_ASSIGNED_SERVICES = (
    select(func.count(ServiceOwner.service_id))
    .where(ServiceOwner.owner_id == Owner.id)
    .scalar_subquery()
)
_ASSIGNED_TEAMS = (
    select(func.count(TeamOwner.team_id))
    .where(TeamOwner.owner_id == Owner.id)
    .scalar_subquery()
)

OWNER_SORTS = SortSpec(
    columns={
        "name": Owner.name,
        "email": Owner.email,
        "title": Owner.title,
        "status": Owner.is_active,
        "services": _ASSIGNED_SERVICES,
        "teams": _ASSIGNED_TEAMS,
    },
    default="name",
    tiebreaker=Owner.id,
    default_desc=False,
)


@router.get("")
async def owners_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: OptStr = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    stmt = select(Owner)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Owner.name.ilike(like) | Owner.email.ilike(like) | Owner.upn.ilike(like)
        )
    pg = await paginate(
        session, stmt, page=page, size=resolve_size(size, ADMIN_PAGE_SIZE),
        spec=OWNER_SORTS, sort_key=sort, sort_dir=dir,
    )

    svc_counts = {
        r[0]: r[1]
        for r in (
            await session.execute(
                select(ServiceOwner.owner_id, func.count()).group_by(ServiceOwner.owner_id)
            )
        ).all()
    }
    team_counts = {
        r[0]: r[1]
        for r in (
            await session.execute(
                select(TeamOwner.owner_id, func.count()).group_by(TeamOwner.owner_id)
            )
        ).all()
    }

    return templates.TemplateResponse(
        request,
        "ui/owners.html",
        {
            "user": user,
            "page_obj": pg,
            "q": q,
            "svc_counts": svc_counts,
            "team_counts": team_counts,
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
        },
    )


@router.post("")
async def create_owner(
    name: str = Form(...),
    email: str = Form(""),
    upn: str = Form(""),
    aad_object_id: str = Form(""),
    phone: str = Form(""),
    title: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    name = name.strip()
    if name:
        owner = Owner(
            name=name,
            email=email.strip() or None,
            upn=upn.strip() or None,
            aad_object_id=aad_object_id.strip() or None,
            phone=phone.strip() or None,
            title=title.strip() or None,
        )
        session.add(owner)
        await session.flush()
        # Audit records the id and name only: email and phone are personal
        # data and the log is read by more people than the directory.
        await audit.record(
            session, user, "owner.create",
            object_type="owner", object_id=owner.id, detail={"name": name},
        )
        await session.commit()
    return RedirectResponse("/labels/owners", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{owner_id}/update")
async def update_owner(
    owner_id: int,
    email: str = Form(""),
    upn: str = Form(""),
    aad_object_id: str = Form(""),
    phone: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    owner = await session.get(Owner, owner_id)
    if owner:
        owner.email = email.strip() or None
        owner.upn = upn.strip() or None
        owner.aad_object_id = aad_object_id.strip() or None
        owner.phone = phone.strip() or None
        await audit.record(
            session, user, "owner.update",
            object_type="owner", object_id=owner_id, detail={"name": owner.name},
        )
        await session.commit()
    return RedirectResponse("/labels/owners", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{owner_id}/active")
async def toggle_owner(
    owner_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    owner = await session.get(Owner, owner_id)
    if owner:
        owner.is_active = not owner.is_active
        await audit.record(
            session, user, "owner.active",
            object_type="owner", object_id=owner_id,
            detail={"name": owner.name, "is_active": owner.is_active},
        )
        await session.commit()
    return RedirectResponse("/labels/owners", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{owner_id}/delete")
async def delete_owner(
    owner_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    owner = await session.get(Owner, owner_id)
    if owner:
        await audit.record(
            session, user, "owner.delete",
            object_type="owner", object_id=owner_id, detail={"name": owner.name},
        )
        await session.delete(owner)
        await session.commit()
    return RedirectResponse("/labels/owners", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# assignment
# ---------------------------------------------------------------------------

@router.post("/assign/service/{service_id}")
async def assign_service(
    service_id: int,
    owner_id: int = Form(...),
    role: str = Form("primary"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    service = await session.get(Service, service_id)
    owner = await session.get(Owner, owner_id)
    if service and owner:
        existing = await session.get(ServiceOwner, {"service_id": service_id, "owner_id": owner_id})
        if existing:
            existing.role = OwnerRole(role)
        else:
            session.add(
                ServiceOwner(service_id=service_id, owner_id=owner_id, role=OwnerRole(role))
            )
        await audit.record(
            session, user, "service.owner.assign",
            object_type="service", object_id=service_id,
            detail={"service": service.key, "owner": owner.name, "role": role},
        )
        await session.commit()
    return RedirectResponse("/labels/services", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/unassign/service/{service_id}/{owner_id}")
async def unassign_service(
    service_id: int,
    owner_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    link = await session.get(ServiceOwner, {"service_id": service_id, "owner_id": owner_id})
    if link:
        await session.delete(link)
        await audit.record(
            session, user, "service.owner.unassign",
            object_type="service", object_id=service_id, detail={"owner_id": owner_id},
        )
        await session.commit()
    return RedirectResponse("/labels/services", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/assign/team/{team_id}")
async def assign_team(
    team_id: int,
    owner_id: int = Form(...),
    role: str = Form("primary"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    team = await session.get(Team, team_id)
    owner = await session.get(Owner, owner_id)
    if team and owner:
        existing = await session.get(TeamOwner, {"team_id": team_id, "owner_id": owner_id})
        if existing:
            existing.role = OwnerRole(role)
        else:
            session.add(TeamOwner(team_id=team_id, owner_id=owner_id, role=OwnerRole(role)))
        await audit.record(
            session, user, "team.owner.assign",
            object_type="team", object_id=team_id,
            detail={"team": team.key, "owner": owner.name, "role": role},
        )
        await session.commit()
    return RedirectResponse("/labels/teams", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/unassign/team/{team_id}/{owner_id}")
async def unassign_team(
    team_id: int,
    owner_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    link = await session.get(TeamOwner, {"team_id": team_id, "owner_id": owner_id})
    if link:
        await session.delete(link)
        await audit.record(
            session, user, "team.owner.unassign",
            object_type="team", object_id=team_id, detail={"owner_id": owner_id},
        )
        await session.commit()
    return RedirectResponse("/labels/teams", status_code=status.HTTP_303_SEE_OTHER)
