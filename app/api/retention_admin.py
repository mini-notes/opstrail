"""Retention status, and a button to run it now.

The purge runs on a schedule, which means the only evidence it works is a log
line at 03:00. That is a poor place to find out it has been silently disabled
for a month, so this page shows the same numbers the job acts on and lets an
admin trigger a run and watch the count change.
"""

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ui import unacked_count, unlabelled_count
from app.config import get_settings
from app.db import get_session
from app.models import User, UserRole
from app.security import current_user, require_role
from app.services import audit, retention
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/settings/retention", tags=["retention"],
                   include_in_schema=False)


async def _page(request, session, user, *, ok=None):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "ui/retention.html",
        {
            "user": user,
            "unlabelled_count": await unlabelled_count(session),
            "unacked_count": await unacked_count(session),
            "state": await retention.preview(),
            "hour": settings.retention_hour_utc,
            "can_run": user.role is UserRole.admin,
            "ok": ok,
        },
    )


@router.get("")
async def retention_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    return await _page(request, session, user)


@router.post("/run")
async def run_now(
    request: Request,
    what: str = "",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.admin)),
):
    """Run a purge immediately.

    Admin-only and audited: it deletes history, and "who cleared the delivery
    log" is a question that gets asked after the fact.
    """
    if what == "notifications":
        removed = await retention.purge_old_notifications()
        label = "bản ghi gửi"
    else:
        what = "alerts"
        removed = await retention.purge_old_alerts()
        label = "alert"

    await audit.record(
        session, user, "retention.run", object_type=what, detail={"removed": removed}
    )
    await session.commit()
    return await _page(
        request, session, user, ok=f"Đã dọn {removed} {label}."
    )
