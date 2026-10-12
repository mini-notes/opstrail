"""Label administration: services, teams and the rules that assign them.

This is the primary way attribution gets configured. Operators who cannot
edit their Prometheus alerting rules have no other lever, so everything here
has to work without touching SQL -- including fixing history, which is what
the backfill endpoints are for.
"""

import logging

from fastapi import APIRouter, Depends, Form, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import ADMIN_PAGE_SIZE, paginate, resolve_size
from app.api.params import OptInt, OptStr
from app.api.sorting import SortSpec
from app.db import get_session
from app.models import (
    Alert,
    LabelRule,
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
from app.services import audit, backfill
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/labels", tags=["labels"], include_in_schema=False)

# Label keys offered in the rule builder are harvested from real alerts, not
# hard-coded: an operator should not have to guess what their own alerts
# carry when OpsTrail already stores it.
KEY_SUGGESTION_DAYS = 30
MAX_VALUE_SUGGESTIONS = 50

# Sorting services/teams by their alert count answers "which one is noisiest"
# straight from the table. It needs the count inside the query, so it is a
# scalar subquery rather than the dict the template uses for display.
_SERVICE_ALERTS = (
    select(func.count(Alert.id)).where(Alert.service_id == Service.id).scalar_subquery()
)
_TEAM_ALERTS = (
    select(func.count(Alert.id)).where(Alert.team_id == Team.id).scalar_subquery()
)
_TEAM_SERVICES = (
    select(func.count(Service.id)).where(Service.team_id == Team.id).scalar_subquery()
)

SERVICE_SORTS = SortSpec(
    columns={"key": Service.key, "name": Service.name,
             "team": Service.team_id, "type": Service.app_type,
             "alerts": _SERVICE_ALERTS},
    default="key", tiebreaker=Service.id, default_desc=False,
)
TEAM_SORTS = SortSpec(
    columns={"key": Team.key, "name": Team.name,
             "services": _TEAM_SERVICES, "alerts": _TEAM_ALERTS},
    default="key", tiebreaker=Team.id, default_desc=False,
)
RULE_SORTS = SortSpec(
    columns={"priority": LabelRule.priority, "status": LabelRule.enabled,
             "created": LabelRule.created_at},
    default="priority", tiebreaker=LabelRule.id, default_desc=False,
)


async def _owner_map(session: AsyncSession, link_model, fk) -> dict[int, list]:
    """{service_id|team_id: [(owner, role), ...]} in one query, not N+1."""
    rows = (await session.execute(select(link_model))).unique().scalars().all()
    out: dict[int, list] = {}
    for r in rows:
        if r.owner:
            out.setdefault(getattr(r, fk), []).append((r.owner, r.role.value))
    return out


async def _all_owners(session: AsyncSession) -> list:
    return (
        (
            await session.execute(
                select(Owner).where(Owner.is_active.is_(True)).order_by(Owner.name)
            )
        )
        .scalars()
        .all()
    )


async def _alert_counts(session: AsyncSession, column) -> dict[int, int]:
    rows = (
        await session.execute(
            select(column, func.count(Alert.id)).where(column.is_not(None)).group_by(column)
        )
    ).all()
    return {r[0]: r[1] for r in rows}


async def _label_keys(session: AsyncSession) -> list[dict]:
    """Label keys actually present on recent alerts, commonest first."""
    sql = """
        SELECT k AS key, count(*) AS alerts, count(DISTINCT labels->>k) AS values
        FROM alerts, jsonb_object_keys(labels) k
        WHERE starts_at >= now() - make_interval(days => :days)
        GROUP BY k ORDER BY alerts DESC
    """
    from sqlalchemy import text

    rows = (await session.execute(text(sql), {"days": KEY_SUGGESTION_DAYS})).all()
    return [{"key": r[0], "alerts": r[1], "values": r[2]} for r in rows]


async def _label_values(session: AsyncSession, key: str) -> list[str]:
    from sqlalchemy import text

    sql = """
        SELECT DISTINCT labels->>:key AS v
        FROM alerts
        WHERE labels ? :key AND starts_at >= now() - make_interval(days => :days)
        ORDER BY v LIMIT :limit
    """
    rows = (
        await session.execute(
            text(sql),
            {"key": key, "days": KEY_SUGGESTION_DAYS, "limit": MAX_VALUE_SUGGESTIONS},
        )
    ).all()
    return [r[0] for r in rows if r[0]]


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------

@router.get("/services")
async def services_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: OptStr = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    stmt = select(Service)
    if q:
        stmt = stmt.where(Service.key.ilike(f"%{q}%") | Service.name.ilike(f"%{q}%"))
    pg = await paginate(
        session, stmt, page=page, size=resolve_size(size, ADMIN_PAGE_SIZE),
        spec=SERVICE_SORTS, sort_key=sort, sort_dir=dir,
    )
    teams = (await session.execute(select(Team).order_by(Team.key))).scalars().all()

    return templates.TemplateResponse(
        request,
        "ui/services.html",
        {
            "user": user,
            "page_obj": pg,
            "teams": teams,
            "counts": await _alert_counts(session, Alert.service_id),
            "rule_counts": await _alert_counts(session, LabelRule.service_id),
            "owners_by_service": await _owner_map(session, ServiceOwner, "service_id"),
            "all_owners": await _all_owners(session),
            "owner_roles": [r.value for r in OwnerRole],
            "q": q,
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
        },
    )


@router.post("/services")
async def create_service(
    key: str = Form(...),
    name: str = Form(""),
    team_id: OptInt = Form(None),
    app_type: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    key = key.strip()
    if key:
        exists = (
            await session.execute(select(Service).where(Service.key == key))
        ).scalar_one_or_none()
        if not exists:
            svc = Service(
                key=key,
                name=name.strip() or key,
                team_id=team_id,
                app_type=app_type.strip() or None,
            )
            session.add(svc)
            await session.flush()
            await audit.record(
                session, user, "service.create",
                object_type="service", object_id=svc.id, detail={"key": key},
            )
            await session.commit()
    return RedirectResponse("/labels/services", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/services/{service_id}/threshold")
async def set_service_threshold(
    service_id: int,
    mention_severity: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Empty means inherit from the team, which then falls back to global."""
    svc = await session.get(Service, service_id)
    if svc:
        svc.mention_severity = mention_severity.strip() or None
        await audit.record(
            session, user, "service.threshold",
            object_type="service", object_id=service_id,
            detail={"key": svc.key, "mention_severity": svc.mention_severity},
        )
        await session.commit()
    return RedirectResponse("/labels/services", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/threshold")
async def set_team_threshold(
    team_id: int,
    mention_severity: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    team = await session.get(Team, team_id)
    if team:
        team.mention_severity = mention_severity.strip() or None
        await audit.record(
            session, user, "team.threshold",
            object_type="team", object_id=team_id,
            detail={"key": team.key, "mention_severity": team.mention_severity},
        )
        await session.commit()
    return RedirectResponse("/labels/teams", status_code=status.HTTP_303_SEE_OTHER)


async def _rescue_orphans(session: AsyncSession) -> int:
    """Send alerts that lost their attribution back to the unlabelled backlog.

    `alerts.service_id` and `alerts.team_id` are ON DELETE SET NULL, but
    nothing clears `label_source`. Deleting a service therefore leaves rows
    reading `label_source='manual'` while pointing at nothing -- and both the
    Unlabelled page and the backfill select on `label_source IS NULL`, so
    those rows become invisible to the two things that exist to find them.
    They would sit there for ever.

    Run after the delete has been flushed, so the FK has already nulled the
    columns. Returns how many were rescued, for the audit entry.
    """
    result = await session.execute(
        update(Alert)
        .where(
            Alert.label_source.is_not(None),
            Alert.service_id.is_(None),
            Alert.team_id.is_(None),
        )
        .values(label_source=None)
    )
    return result.rowcount or 0


@router.post("/services/{service_id}/delete")
async def delete_service(
    service_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Deleting a service is destructive in two directions.

    `alerts.service_id` is ON DELETE SET NULL -- alerts survive but lose their
    label. `label_rules.service_id` is ON DELETE CASCADE -- any rule pointing
    here is deleted outright. The template warns about both before asking.
    """
    svc = await session.get(Service, service_id)
    if svc:
        await audit.record(
            session, user, "service.delete",
            object_type="service", object_id=service_id, detail={"key": svc.key},
        )
        await session.delete(svc)
        await session.flush()
        rescued = await _rescue_orphans(session)
        if rescued:
            await audit.record(
                session, user, "alert.label.orphaned",
                object_type="service", object_id=service_id,
                detail={"returned_to_backlog": rescued},
            )
        await session.commit()
    return RedirectResponse("/labels/services", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# teams
# ---------------------------------------------------------------------------

@router.get("/teams")
async def teams_page(
    request: Request,
    page: int = Query(1, ge=1),
    q: OptStr = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    stmt = select(Team)
    if q:
        stmt = stmt.where(Team.key.ilike(f"%{q}%") | Team.name.ilike(f"%{q}%"))
    pg = await paginate(
        session, stmt, page=page, size=resolve_size(size, ADMIN_PAGE_SIZE),
        spec=TEAM_SORTS, sort_key=sort, sort_dir=dir,
    )
    return templates.TemplateResponse(
        request,
        "ui/teams.html",
        {
            "user": user,
            "page_obj": pg,
            "counts": await _alert_counts(session, Alert.team_id),
            "service_counts": await _alert_counts(session, Service.team_id),
            "owners_by_team": await _owner_map(session, TeamOwner, "team_id"),
            "all_owners": await _all_owners(session),
            "owner_roles": [r.value for r in OwnerRole],
            "q": q,
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
        },
    )


@router.post("/teams")
async def create_team(
    key: str = Form(...),
    name: str = Form(""),
    msteams_webhook_url: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    key = key.strip()
    if key:
        exists = (
            await session.execute(select(Team).where(Team.key == key))
        ).scalar_one_or_none()
        if not exists:
            team = Team(
                key=key,
                name=name.strip() or key,
                msteams_webhook_url=msteams_webhook_url.strip() or None,
            )
            session.add(team)
            await session.flush()
            await audit.record(
                session, user, "team.create",
                object_type="team", object_id=team.id, detail={"key": key},
            )
            await session.commit()
    return RedirectResponse("/labels/teams", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/webhook")
async def set_team_webhook(
    team_id: int,
    msteams_webhook_url: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Per-team Power Automate URL; empty falls back to the global setting."""
    team = await session.get(Team, team_id)
    if team:
        team.msteams_webhook_url = msteams_webhook_url.strip() or None
        await audit.record(
            session, user, "team.webhook",
            object_type="team", object_id=team_id,
            detail={"key": team.key, "set": bool(team.msteams_webhook_url)},
        )
        await session.commit()
    return RedirectResponse("/labels/teams", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/teams/{team_id}/delete")
async def delete_team(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    team = await session.get(Team, team_id)
    if team:
        await audit.record(
            session, user, "team.delete",
            object_type="team", object_id=team_id, detail={"key": team.key},
        )
        await session.delete(team)
        await session.flush()
        rescued = await _rescue_orphans(session)
        if rescued:
            await audit.record(
                session, user, "alert.label.orphaned",
                object_type="team", object_id=team_id,
                detail={"returned_to_backlog": rescued},
            )
        await session.commit()
    return RedirectResponse("/labels/teams", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

@router.get("/rules")
async def rules_page(
    request: Request,
    page: int = Query(1, ge=1),
    from_alert: OptInt = None,
    size: OptInt = None,
    sort: OptStr = None,
    dir: OptStr = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    pg = await paginate(
        session, select(LabelRule),
        page=page, size=resolve_size(size, ADMIN_PAGE_SIZE),
        spec=RULE_SORTS, sort_key=sort, sort_dir=dir,
    )
    services = (await session.execute(select(Service).order_by(Service.key))).scalars().all()
    teams = (await session.execute(select(Team).order_by(Team.key))).scalars().all()

    # "Create rule from this alert": prefill the builder with that alert's
    # own labels, which is the natural move after spotting it on /unlabelled.
    prefill = {}
    if from_alert:
        alert = await session.get(Alert, from_alert)
        if alert:
            prefill = {
                k: v for k, v in (alert.labels or {}).items()
                if k.lower() not in ("severity",)
            }

    return templates.TemplateResponse(
        request,
        "ui/rules.html",
        {
            "user": user,
            "page_obj": pg,
            "services": {s.id: s for s in services},
            "teams": {t.id: t for t in teams},
            "service_list": services,
            "team_list": teams,
            "label_keys": await _label_keys(session),
            "prefill": prefill,
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
        },
    )


@router.get("/rules/values")
async def rule_value_suggestions(
    key: str,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
) -> dict:
    """Values seen for one label key, to fill the builder's datalist."""
    return {"key": key, "values": await _label_values(session, key)}


def _parse_conditions(keys: list[str], values: list[str]) -> dict:
    """Pair up the builder's parallel key/value inputs, dropping blank rows."""
    out = {}
    for k, v in zip(keys, values, strict=False):
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


@router.post("/rules")
async def create_rule(
    priority: int = Form(100),
    match_key: list[str] = Form(default=[]),
    match_value: list[str] = Form(default=[]),
    service_id: OptInt = Form(None),
    team_id: OptInt = Form(None),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    match = _parse_conditions(match_key, match_value)
    # An empty match would claim every alert; labeling._rule_matches already
    # refuses it, but there is no reason to store one.
    if match and (service_id or team_id):
        rule = LabelRule(
            priority=priority,
            match=match,
            service_id=service_id,
            team_id=team_id,
            description=description.strip() or None,
            enabled=True,
        )
        session.add(rule)
        await session.flush()
        await audit.record(
            session, user, "rule.create",
            object_type="label_rule", object_id=rule.id,
            detail={"match": match, "service_id": service_id, "team_id": team_id},
        )
        await session.commit()
    return RedirectResponse("/labels/rules", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rules/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    rule = await session.get(LabelRule, rule_id)
    if rule:
        rule.enabled = not rule.enabled
        await audit.record(
            session, user, "rule.toggle",
            object_type="label_rule", object_id=rule_id,
            detail={"enabled": rule.enabled},
        )
        await session.commit()
    return RedirectResponse("/labels/rules", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rules/{rule_id}/delete")
async def delete_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    rule = await session.get(LabelRule, rule_id)
    if rule:
        await audit.record(
            session, user, "rule.delete",
            object_type="label_rule", object_id=rule_id, detail={"match": rule.match},
        )
        await session.delete(rule)
        await session.commit()
    return RedirectResponse("/labels/rules", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------

@router.get("/rules/backfill")
async def backfill_preview(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Show what applying the current rules to stored alerts would do."""
    return templates.TemplateResponse(
        request,
        "ui/rules_preview.html",
        {
            "user": user,
            "preview": await backfill.preview(session),
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
        },
    )


@router.post("/rules/backfill")
async def backfill_apply(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    changed = await backfill.apply(session)
    await audit.record(
        session, user, "rules.backfill",
        object_type="alert", detail={"labelled": changed},
    )
    await session.commit()
    log.info("backfill by %s: %d alert(s) labelled", user.username, changed)
    return RedirectResponse(
        f"/labels/rules?backfilled={changed}", status_code=status.HTTP_303_SEE_OTHER
    )
