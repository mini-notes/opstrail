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
from app.api.params import OptInt, OptStr, parse_window
from app.api.sorting import SortSpec, severity_rank
from app.config import get_settings
from app.db import get_session
from app.models import Alert, LabelSource, Notification, Service, Team, User, UserRole
from app.security import current_user, require_role
from app.services import audit, retention, stats
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


# Sortable columns of the delivery table. The tiebreaker matters more here
# than on alerts: `status` and `channel` have two and three distinct values
# across the whole table, so without it a page boundary lands in the middle of
# a huge tie and rows duplicate or vanish between pages.
DELIVERY_SORTS = SortSpec(
    columns={
        "created": Notification.created_at,
        "sent": Notification.sent_at,
        "channel": Notification.channel,
        "status": Notification.status,
        "attempts": Notification.attempts,
        "http": Notification.response_status,
        "duration": Notification.duration_ms,
        "alert": Alert.alertname,
        "severity": severity_rank(Alert.severity),
    },
    default="created",
    tiebreaker=Notification.id,
    default_desc=True,
)


def empty_delivery_reason(
    alert: Alert, cutoff: datetime | None, months: int
) -> str | None:
    """Why a delivery table is empty, or None when it is not empty.

    An empty table is ambiguous and the two meanings matter: an alert nobody
    was told about is a configuration bug to chase, while an alert whose
    records aged out is nothing at all. Rendering the same blank table for
    both makes the first one invisible.

    `notify_count` is the strong signal -- it lives on the alert, so it
    survives the purge, and a non-zero count with no rows PROVES the records
    were deleted. With a zero count all we have is the alert's age, and past
    the cutoff "a failed attempt was purged" and "nothing was ever queued"
    are genuinely indistinguishable. Say that, rather than claiming the
    stronger of the two.
    """
    if alert.notify_count:
        return (
            f"Đã gửi {alert.notify_count}× — bản ghi chi tiết đã bị xoá theo "
            f"retention {months} tháng."
        )
    if cutoff is not None and alert.starts_at < cutoff:
        return (
            f"Không còn bản ghi gửi nào được giữ (retention {months} tháng). "
            "Alert này cũ hơn mốc đó, nên không phân biệt được 'từng gửi "
            "thất bại' với 'chưa từng gửi'."
        )
    return "Chưa từng gửi — không có channel nào được cấu hình cho alert này."


def _delivery_query(
    *,
    channel: str | None,
    delivery_status: str | None,
    service_id: int | None,
    team_id: int | None,
    q: str | None,
    days: int | None,
    start: datetime | None = None,
    end: datetime | None = None,
):
    """Deliveries, filtered.

    The time window is on `Notification.created_at`, NOT `Alert.starts_at` --
    deliberately different from stats.notification_health, which scopes by
    when the alert started. A retry of a two-day-old alert happened today, and
    on a page whose subject is deliveries "last 24h" has to mean deliveries.
    Do not "fix" one to match the other.
    """
    stmt = select(Notification).join(Alert, Alert.id == Notification.alert_id)
    if channel:
        stmt = stmt.where(Notification.channel == channel)
    if delivery_status:
        stmt = stmt.where(Notification.status == delivery_status)
    if service_id:
        stmt = stmt.where(Alert.service_id == service_id)
    if team_id:
        stmt = stmt.where(Alert.team_id == team_id)
    if q:
        like = f"%{q}%"
        # `target` is searchable but never displayed in full: finding every
        # delivery to one team's webhook is a normal thing to want.
        stmt = stmt.where(
            or_(
                Alert.alertname.ilike(like),
                Notification.last_error.ilike(like),
                Notification.target.ilike(like),
            )
        )
    if start and end:
        stmt = stmt.where(
            Notification.created_at >= start, Notification.created_at <= end
        )
    elif days:
        stmt = stmt.where(
            Notification.created_at >= datetime.now(UTC) - timedelta(days=days)
        )
    return stmt


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
    start: datetime | None = None,
    end: datetime | None = None,
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
    if start and end:
        stmt = stmt.where(Alert.starts_at >= start, Alert.starts_at <= end)
    elif days:
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
    from_: OptStr = Query(None, alias="from"),
    to: OptStr = None,
    tz_offset: OptInt = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002 - the query parameter is named `dir`
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    # The picker sends either a relative `days` or an absolute pair. Parsing
    # here rather than in the query keeps the "unparseable means no window"
    # rule in one place.
    start, end, tz_offset_minutes = parse_window(from_, to, tz_offset)
    return await _render_alert_list(
        request, session, user,
        template="ui/alerts.html",
        page=page, size=size, sort_key=sort, sort_dir=dir,
        service_id=service_id, team_id=team_id, severity=severity,
        alert_status=alert_status, label_source=label_source, q=q, days=days,
        start=start, end=end,
    )


@router.get("/unlabelled")
async def unlabelled_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: OptStr = None,
    days: OptInt = None,
    from_: OptStr = Query(None, alias="from"),
    to: OptStr = None,
    tz_offset: OptInt = None,
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
    # The picker sends either a relative `days` or an absolute pair. Parsing
    # here rather than in the query keeps the "unparseable means no window"
    # rule in one place.
    start, end, tz_offset_minutes = parse_window(from_, to, tz_offset)
    return await _render_alert_list(
        request, session, user,
        template="ui/unlabelled.html",
        page=page, size=size, sort_key=sort, sort_dir=dir, force_unlabelled=True,
        service_id=None, team_id=None, severity=None,
        alert_status=None, q=q, days=days, start=start, end=end,
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

    months = get_settings().notification_retention_months
    return templates.TemplateResponse(
        request,
        "ui/alert_detail.html",
        {
            "user": user,
            "unlabelled_count": await unlabelled_count(session),
            "alert": alert,
            "notifications": notifications,
            "empty_reason": (
                None
                if notifications
                else empty_delivery_reason(
                    alert, retention.notification_cutoff(), months
                )
            ),
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
    from_: OptStr = Query(None, alias="from"),
    to: OptStr = None,
    tz_offset: OptInt = None,
    service_id: OptInt = None,
    team_id: OptInt = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    start, end, tz_offset_minutes = parse_window(from_, to, tz_offset)
    scope = stats.Scope(
        days=days, start=start, end=end, tz_offset_minutes=tz_offset_minutes,
        service_id=service_id, team_id=team_id,
    )
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
            "filters": {
                "days": days, "start": start, "end": end,
                "tz_offset": tz_offset_minutes,
            },
            "service_id": service_id,
            "team_id": team_id,
            "services": services,
            "teams": teams,
            "headline": await stats.headline(session, scope),
            "notification_months": get_settings().notification_retention_months,
        },
    )


# ---------------------------------------------------------------------------
# delivery history
# ---------------------------------------------------------------------------

@router.get("/deliveries")
async def deliveries_page(
    request: Request,
    page: int = Query(1, ge=1),
    channel: OptStr = None,
    delivery_status: OptStr = None,
    service_id: OptInt = None,
    team_id: OptInt = None,
    q: OptStr = None,
    days: OptInt = None,
    from_: OptStr = Query(None, alias="from"),
    to: OptStr = None,
    tz_offset: OptInt = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Every delivery OpsTrail has attempted, newest first.

    The dashboard chart answers "how many failed"; this answers "which one,
    and what did the other end say".
    """
    start, end, tz_offset_minutes = parse_window(from_, to, tz_offset)
    filters = {
        "channel": channel,
        "delivery_status": delivery_status,
        "service_id": service_id,
        "team_id": team_id,
        "q": q,
        "days": days,
        "start": start,
        "end": end,
    }
    stmt = _delivery_query(**filters).options(
        selectinload(Notification.alert).selectinload(Alert.service),
        selectinload(Notification.alert).selectinload(Alert.team),
    )
    pg = await paginate(
        session, stmt, page=page, size=resolve_size(size, ALERT_PAGE_SIZE),
        spec=DELIVERY_SORTS, sort_key=sort, sort_dir=dir,
    )
    services = (
        (await session.execute(select(Service).order_by(Service.key))).scalars().all()
    )
    teams = (await session.execute(select(Team).order_by(Team.key))).scalars().all()

    ctx = {
        "user": user,
        "unlabelled_count": await unlabelled_count(session),
        "deliveries": pg.items,
        "services": services,
        "teams": teams,
        "page_obj": pg,
        "filters": filters,
    }
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(request, "ui/_delivery_table.html", ctx)
    return templates.TemplateResponse(request, "ui/deliveries.html", ctx)


@router.get("/deliveries/{note_id}")
async def delivery_detail(
    note_id: int,
    request: Request,
    reveal: bool = False,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """One delivery in full.

    A page rather than a row that expands: `last_error` holds up to 2000
    characters and a failed custom template puts its entire rendered output
    in there, which is both the most useful thing here and unreadable in a
    table cell. It is also a link somebody can paste into a ticket.
    """
    note = (
        await session.execute(
            select(Notification)
            .options(
                selectinload(Notification.alert).selectinload(Alert.service),
                selectinload(Notification.alert).selectinload(Alert.team),
            )
            .where(Notification.id == note_id)
        )
    ).scalar_one_or_none()
    if note is None:
        return templates.TemplateResponse(
            request, "ui/not_found.html", {"user": user, "alert_id": note_id},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # The full target is the credential that can post into the channel, so
    # revealing it is an admin action and is recorded like one.
    show_target = reveal and user.role is UserRole.admin
    if show_target:
        await audit.record(
            session, user, "notification.target.reveal",
            object_type="notification", object_id=note.id,
        )
        await session.commit()

    return templates.TemplateResponse(
        request,
        "ui/delivery_detail.html",
        {
            "user": user,
            "unlabelled_count": await unlabelled_count(session),
            "note": note,
            "show_target": show_target,
            "can_reveal": user.role is UserRole.admin,
        },
    )
