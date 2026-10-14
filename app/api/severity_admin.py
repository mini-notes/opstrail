"""Severity rules: adjust how serious an alert is treated as, by context.

Separate page rather than another section on the labels screen, because the
question it answers is different: labels say who owns an alert, these say
how loudly to shout about it right now.

Writes are editor-only and audited, like every other rule table.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.params import OptInt
from app.api.ui import unlabelled_count
from app.config import get_settings
from app.db import get_session
from app.models import Alert, Service, SeverityRule, Team, User, UserRole
from app.security import current_user, require_role
from app.services import audit, severity, severity_rules, template_select
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/labels/severity", tags=["severity"],
                   include_in_schema=False)

# What the "create the trading-hours baseline" button writes. A starting
# point, not a special case: it produces an ordinary rule that can then be
# edited or deleted like any other. Every company's session differs, which
# is why this is a seed and not something compiled in.
TRADING_BASELINE = {
    "name": "Giờ giao dịch",
    "priority": 100,
    "match": {"severity": "*"},
    "time_window": {"days": [1, 2, 3, 4, 5], "from": "09:00", "to": "15:00"},
    "action": "bump",
    "value": "1",
    "description": "Trong phiên thì nâng mọi alert lên một bậc",
}


def _parse_conditions(keys: list[str], values: list[str]) -> dict:
    """Pair up the builder's parallel key/value inputs, dropping blank rows."""
    out = {}
    for k, v in zip(keys, values, strict=False):
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


async def _page(request, session, user, *, error=None, ok=None):
    rules = list(
        (
            await session.execute(
                select(SeverityRule).order_by(
                    SeverityRule.priority, SeverityRule.id
                )
            )
        )
        .scalars()
        .all()
    )

    # The most recent alert, used to show what the rules would do to
    # something real. "Why doesn't my rule fire" is answered by looking.
    sample = (
        await session.execute(
            select(Alert).order_by(Alert.received_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    context, evaluated, winner = {}, [], None
    tz = severity_rules.business_tz()
    now = datetime.now(tz)
    if sample is not None:
        service = (
            await session.get(Service, sample.service_id)
            if sample.service_id else None
        )
        team = await session.get(Team, sample.team_id) if sample.team_id else None
        context = dict(
            sorted(
                template_select.match_context(
                    sample, service=service, team=team
                ).items()
            )
        )
        for rule in rules:
            matches = template_select.rule_matches(rule.match or {}, context)
            open_now = severity_rules.in_window(rule.time_window, now)
            active = bool(rule.enabled) and matches and open_now
            if active and winner is None:
                winner = rule
            evaluated.append({
                "rule": rule,
                "matches": matches,
                "open_now": open_now,
                "winner": active and winner is rule,
                "window_text": severity_rules.describe_window(rule.time_window),
            })

    return templates.TemplateResponse(
        request,
        "ui/severity.html",
        {
            "user": user,
            "unlabelled_count": await unlabelled_count(session),
            "rules": evaluated,
            "sample": sample,
            "context": context,
            "winner": winner,
            "now": now,
            "tz_name": get_settings().business_timezone,
            "bands": severity.BANDS,
            "weekdays": list(enumerate(severity_rules.WEEKDAY_NAMES, start=1)),
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
            "error": error,
            "ok": ok,
        },
        status_code=status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK,
    )


@router.get("")
async def severity_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    return await _page(request, session, user)


@router.post("/baseline")
async def create_baseline(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    existing = await session.scalar(
        select(SeverityRule.id).where(SeverityRule.name == TRADING_BASELINE["name"])
    )
    if existing:
        return await _page(
            request, session, user,
            error=f"Đã có rule tên {TRADING_BASELINE['name']!r}.",
        )
    rule = SeverityRule(**TRADING_BASELINE)
    session.add(rule)
    await session.flush()
    await audit.record(
        session, user, "severity_rule.create",
        object_type="severity_rule", object_id=rule.id,
        detail={"baseline": True, **TRADING_BASELINE},
    )
    await session.commit()
    return RedirectResponse("/labels/severity", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/new")
async def create_rule(
    request: Request,
    name: str = Form(...),
    priority: int = Form(100),
    match_key: list[str] = Form(default=[]),
    match_value: list[str] = Form(default=[]),
    days: list[int] = Form(default=[]),
    window_from: str = Form(""),
    window_to: str = Form(""),
    action: str = Form("bump"),
    value: str = Form("1"),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    match = _parse_conditions(match_key, match_value)
    if not match:
        return await _page(
            request, session, user,
            error="Rule cần ít nhất một điều kiện — rule rỗng sẽ nhận mọi alert.",
        )
    if action not in severity_rules.ACTIONS:
        return await _page(request, session, user, error=f"Hành động lạ: {action}")

    # Refuse a rule that could never produce anything, here rather than
    # letting it sit in the table being silently skipped at 3am.
    if severity_rules.apply_action("warning", action, value) is None:
        return await _page(
            request, session, user,
            error=f"Giá trị {value!r} không dùng được với hành động {action!r}.",
        )

    window: dict = {}
    if days:
        window["days"] = sorted({int(d) for d in days if 1 <= int(d) <= 7})
    if window_from.strip() and window_to.strip():
        window["from"] = window_from.strip()
        window["to"] = window_to.strip()

    rule = SeverityRule(
        name=name.strip() or "rule", priority=priority, match=match,
        time_window=window, action=action, value=str(value).strip(),
        description=description.strip() or None,
    )
    session.add(rule)
    await session.flush()
    await audit.record(
        session, user, "severity_rule.create",
        object_type="severity_rule", object_id=rule.id,
        detail={"match": match, "window": window, "action": action, "value": value},
    )
    await session.commit()
    return RedirectResponse("/labels/severity", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/toggle")
async def toggle_rule(
    id: OptInt = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    rule = await session.get(SeverityRule, id) if id else None
    if rule:
        rule.enabled = not rule.enabled
        await audit.record(
            session, user, "severity_rule.toggle",
            object_type="severity_rule", object_id=id,
            detail={"enabled": rule.enabled},
        )
        await session.commit()
    return RedirectResponse("/labels/severity", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/delete")
async def delete_rule(
    id: OptInt = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    rule = await session.get(SeverityRule, id) if id else None
    if rule:
        await audit.record(
            session, user, "severity_rule.delete",
            object_type="severity_rule", object_id=id,
            detail={"name": rule.name},
        )
        await session.delete(rule)
        await session.commit()
    return RedirectResponse("/labels/severity", status_code=status.HTTP_303_SEE_OTHER)
