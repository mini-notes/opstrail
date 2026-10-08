"""Server-rendered admin pages.

`/alerts/{id}` is not a free choice of route: app/notifiers/msteams.py already
builds `detail_url = f"{public_host}/alerts/{id}"` into the "Open in OpsTrail"
button of every Adaptive Card it has sent. Renaming it would turn every card
already sitting in a Teams channel into a 404.
"""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.pagination import ALERT_PAGE_SIZE, paginate, resolve_size
from app.api.params import OptInt, OptStr
from app.api.sorting import SortSpec, severity_rank
from app.db import get_session
from app.models import Alert, LabelSource, Notification, Service, Team, User, UserRole
from app.security import current_user, require_role
from app.services import audit, stats
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["ui"], include_in_schema=False)

# Sortable columns of the alert table. Keys are what appear in `?sort=`;
# anything else falls back to `started`. Severity sorts by how bad it is,
# not alphabetically -- see sorting.severity_rank.
ALERT_SORTS = SortSpec(
    columns={
        "started": Alert.starts_at,
        "severity": severity_rank(Alert.severity),
        "alert": Alert.alertname,
        "status": Alert.status,
        # NULL while an alert is still firing; nullslast() keeps those out of
        # the way when sorting "longest first".
        "duration": Alert.ends_at - Alert.starts_at,
        "label": Alert.label_source,
    },
    default="started",
    tiebreaker=Alert.id,
    default_desc=True,
)


async def unlabelled_count(session: AsyncSession) -> int:
    """Badge on the sidebar. Turning the backlog into something visible beats
    relying on anyone remembering to go and look at it."""
    return (
        await session.execute(
            select(func.count(Alert.id)).where(Alert.label_source.is_(None))
        )
    ).scalar_one()


@router.get("/")
async def index():
    return RedirectResponse("/alerts", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


def _filtered_query(
    *,
    service_id: int | None,
    team_id: int | None,
    severity: str | None,
    alert_status: str | None,
    label_source: str | None,
    q: str | None,
    days: int | None,
):
    stmt = select(Alert)
    if service_id:
        stmt = stmt.where(Alert.service_id == service_id)
    if team_id:
        stmt = stmt.where(Alert.team_id == team_id)
    if severity:
        stmt = stmt.where(func.lower(Alert.severity) == severity.lower())
    if alert_status:
        stmt = stmt.where(Alert.status == alert_status)
    if label_source == "none":
        stmt = stmt.where(Alert.label_source.is_(None))
    elif label_source:
        stmt = stmt.where(Alert.label_source == label_source)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Alert.alertname.ilike(like),
                Alert.summary.ilike(like),
                Alert.description.ilike(like),
            )
        )
    if days:
        stmt = stmt.where(
            Alert.starts_at >= datetime.now(UTC) - timedelta(days=days)
        )
    return stmt


async def _render_alert_list(
    request: Request,
    session: AsyncSession,
    user: User,
    *,
    template: str,
    page: int,
    force_unlabelled: bool = False,
    size: int | None = None,
    sort_key: str | None = None,
    sort_dir: str | None = None,
    **filters,
):
    """Render the list page, or just its table when HTMX asked for it.

    HTMX sets `HX-Request: true`; answering with only the table partial lets a
    dropdown change swap rows in place instead of reloading the whole page.
    """
    if force_unlabelled:
        filters["label_source"] = "none"

    stmt = _filtered_query(**filters).options(
        selectinload(Alert.service), selectinload(Alert.team)
    )
    pg = await paginate(
        session,
        stmt,
        page=page,
        size=resolve_size(size, ALERT_PAGE_SIZE),
        spec=ALERT_SORTS,
        sort_key=sort_key,
        sort_dir=sort_dir,
    )

    services = (
        (await session.execute(select(Service).order_by(Service.key))).scalars().all()
    )
    teams = (await session.execute(select(Team).order_by(Team.key))).scalars().all()

    ctx = {
        "user": user,
        "unlabelled_count": await unlabelled_count(session),
        "alerts": pg.items,
        "services": services,
        "teams": teams,
        "page_obj": pg,
        "filters": filters,
    }
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(request, "ui/_table.html", ctx)
    return templates.TemplateResponse(request, template, ctx)


@router.get("/alerts")
async def alerts_page(
    request: Request,
    page: int = Query(1, ge=1),
    service_id: OptInt = None,
    team_id: OptInt = None,
    severity: OptStr = None,
    alert_status: OptStr = None,
    label_source: OptStr = None,
    q: OptStr = None,
    days: OptInt = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002 - the query parameter is named `dir`
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    return await _render_alert_list(
        request, session, user,
        template="ui/alerts.html",
        page=page, size=size, sort_key=sort, sort_dir=dir,
        service_id=service_id, team_id=team_id, severity=severity,
        alert_status=alert_status, label_source=label_source, q=q, days=days,
    )


@router.get("/unlabelled")
async def unlabelled_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: OptStr = None,
    days: OptInt = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Alerts no rule and no payload label claimed.

    This is the actionable backlog: each distinct alertname here is a missing
    `service`/`team` label on a Prometheus rule, or a missing label_rule.
    """
    return await _render_alert_list(
        request, session, user,
        template="ui/unlabelled.html",
        page=page, size=size, sort_key=sort, sort_dir=dir, force_unlabelled=True,
        service_id=None, team_id=None, severity=None,
        alert_status=None, q=q, days=days,
    )


@router.get("/alerts/{alert_id}")
async def alert_detail(
    alert_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    alert = (
        await session.execute(
            select(Alert)
            .options(selectinload(Alert.service), selectinload(Alert.team))
            .where(Alert.id == alert_id)
        )
    ).scalar_one_or_none()
    if alert is None:
        return templates.TemplateResponse(
            request, "ui/not_found.html", {"user": user, "alert_id": alert_id},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    notifications = (
        (
            await session.execute(
                select(Notification)
                .where(Notification.alert_id == alert_id)
                .order_by(Notification.id)
            )
        )
        .scalars()
        .all()
    )
    services = (
        (await session.execute(select(Service).order_by(Service.key))).scalars().all()
    )
    teams = (await session.execute(select(Team).order_by(Team.key))).scalars().all()

    return templates.TemplateResponse(
        request,
        "ui/alert_detail.html",
        {
            "user": user,
            "unlabelled_count": await unlabelled_count(session),
            "alert": alert,
            "notifications": notifications,
            "services": services,
            "teams": teams,
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
        },
    )


@router.post("/alerts/{alert_id}/label")
async def set_label(
    alert_id: int,
    request: Request,
    service_id: str = Form(""),
    team_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Tag an alert by hand.

    Writes `label_source='manual'`, which app/services/ingest.py protects:
    its ON CONFLICT clause keeps a manual tag when Alertmanager re-sends the
    same alert, while refreshing anything that was auto-resolved.
    """
    alert = await session.get(Alert, alert_id)
    if alert is None:
        return RedirectResponse("/alerts", status_code=status.HTTP_303_SEE_OTHER)

    svc_id = int(service_id) if service_id else None
    tm_id = int(team_id) if team_id else None

    # Picking a service with no explicit team inherits that service's team,
    # mirroring how app/services/labeling.py resolves attribution.
    if svc_id and not tm_id:
        service = await session.get(Service, svc_id)
        if service:
            tm_id = service.team_id

    before = {"service_id": alert.service_id, "team_id": alert.team_id,
              "label_source": alert.label_source.value if alert.label_source else None}

    alert.service_id = svc_id
    alert.team_id = tm_id
    alert.label_source = LabelSource.manual if (svc_id or tm_id) else None

    await audit.record(
        session, user, "alert.label",
        object_type="alert", object_id=alert_id,
        detail={"from": before,
                "to": {"service_id": svc_id, "team_id": tm_id,
                       "label_source": alert.label_source.value if alert.label_source else None}},
    )
    await session.commit()
    return RedirectResponse(
        f"/alerts/{alert_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/dashboard")
async def dashboard_page(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    service_id: OptInt = None,
    team_id: OptInt = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    scope = stats.Scope(days=days, service_id=service_id, team_id=team_id)
    services = (
        (await session.execute(select(Service).order_by(Service.key))).scalars().all()
    )
    teams = (await session.execute(select(Team).order_by(Team.key))).scalars().all()
    return templates.TemplateResponse(
        request,
        "ui/dashboard.html",
        {
            "user": user,
            "unlabelled_count": await unlabelled_count(session),
            "days": days,
            "service_id": service_id,
            "team_id": team_id,
            "services": services,
            "teams": teams,
            "headline": await stats.headline(session, scope),
        },
    )
