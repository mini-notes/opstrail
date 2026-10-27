"""Edit MS Teams card templates, and the rules that choose between them.

Templates live in the database so changing a card does not need a redeploy.
There can be several: one catch-all plus whatever a `template_rules` row
points at, because a tier-1 payment outage and a disk filling up on staging
do not deserve the same layout.

Two safety layers keep a typo from silencing every alert, and both survive
having many templates:

* saving renders the template against the latest real alert AND a
  fully-populated sample, so broken JSON never reaches the table -- and it is
  checked per template, because a rule makes a template reachable by alerts
  the person editing it never looked at;
* sending falls back to the built-in template if a saved one fails on a
  particular alert (notifiers/render.render_card_safe), and selection falls
  back to the catch-all if the rules themselves misbehave
  (services/template_select).

Routes use `?id=` rather than a path segment so that `/settings/template/rules`
can exist without colliding with a template whose id happens to be "rules".
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.params import OptInt
from app.config import get_settings
from app.db import get_session
from app.models import (
    Alert,
    AlertStatus,
    MessageTemplate,
    NotificationChannel,
    Service,
    Team,
    TemplateRule,
    User,
    UserRole,
)
from app.notifiers import render
from app.security import current_user, require_role
from app.services import audit, mention, template_select
from app.templating import templates as ui_templates

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/settings/template", tags=["template"], include_in_schema=False
)

MSTEAMS = NotificationChannel.msteams


def _sample_alert() -> Alert:
    """A representative alert for preview and save-time validation.

    Carries the optional fields a template is likely to touch, so a template
    that only works when everything is present fails at save rather than at
    3am.
    """
    now = datetime.now(UTC)
    return Alert(
        id=0,
        fingerprint="sample0001",
        starts_at=now - timedelta(minutes=12),
        ends_at=None,
        status=AlertStatus.firing,
        alertname="PostgresqlDown",
        severity="critical",
        summary="Postgres down (instance pg-prod-01)",
        description="Kết nối tới primary thất bại 3 lần liên tiếp.",
        labels={
            "alertname": "PostgresqlDown", "severity": "critical",
            "instance": "pg-prod-01", "cluster": "prod-hcm",
            "namespace": "payments", "environment": "production",
        },
        annotations={
            "summary": "Postgres down (instance pg-prod-01)",
            # value/threshold only exist when the alerting rule puts them
            # there; the sample carries them so the preview shows the line.
            "value": "0 connection",
            "threshold": "1 connection",
            "runbook_url": "https://wiki.company.local/runbooks/postgres-down",
            "impact": "Thanh toán không xử lý được giao dịch mới.",
        },
        generator_url="http://prometheus.local/graph?g0.expr=up",
        external_url="http://alertmanager.local",
        notify_count=1,
    )


async def _all_templates(session: AsyncSession) -> list[MessageTemplate]:
    return list(
        (
            await session.execute(
                select(MessageTemplate)
                .where(MessageTemplate.channel == MSTEAMS)
                .order_by(MessageTemplate.is_active.desc(), MessageTemplate.name)
            )
        )
        .scalars()
        .all()
    )


async def _default(session: AsyncSession) -> MessageTemplate | None:
    return (
        await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.channel == MSTEAMS,
                MessageTemplate.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def _rules(session: AsyncSession) -> list[TemplateRule]:
    return list(
        (
            await session.execute(
                select(TemplateRule)
                .join(MessageTemplate, MessageTemplate.id == TemplateRule.template_id)
                .where(MessageTemplate.channel == MSTEAMS)
                .options(selectinload(TemplateRule.template))
                .order_by(TemplateRule.priority, TemplateRule.id)
            )
        )
        .scalars()
        .all()
    )


async def _preview_alert(session: AsyncSession, *, use_sample: bool) -> Alert:
    """The alert the preview renders against.

    Defaults to the most recent REAL one: a preview against invented data
    hides the shape of your own. But a real alert often lacks `value` /
    `threshold` (the rule has not been updated yet), and then the preview
    shows no measurement line and looks broken. `use_sample` switches to a
    fully-populated alert so both can be compared.
    """
    alert = None
    if not use_sample:
        alert = (
            await session.execute(
                select(Alert).order_by(Alert.received_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    return alert or _sample_alert()


async def _attribution(
    session: AsyncSession, alert: Alert
) -> tuple[Service | None, Team | None]:
    service = await session.get(Service, alert.service_id) if alert.service_id else None
    team = await session.get(Team, alert.team_id) if alert.team_id else None
    return service, team


async def _preview_context(session: AsyncSession, alert: Alert) -> dict:
    service, team = await _attribution(session, alert)

    entities, text, names = [], "", []
    if alert.id:
        try:
            plan = await mention.plan_for(session, alert)
            names = [o.mention_name for o in plan.owners]
            if plan.should_mention:
                entities = mention.build_entities(plan.owners)
                text = mention.build_text(entities) if entities else ""
        except Exception:  # noqa: BLE001 - preview must never 500
            log.exception("preview: mention resolution failed")

    return render.build_context(
        alert,
        service_name=service.name if service else None,
        team_name=team.name if team else None,
        # The same URL the real send builds. Hardcoding localhost here meant
        # the preview showed a link nobody could click.
        detail_url=(
            f"{get_settings().public_host.rstrip('/')}/alerts/{alert.id or 0}"
        ),
        mention_entities=entities,
        mention_text=text,
        owner_names=names,
    )


def _render_preview(source: str, context: dict) -> tuple[str | None, str | None]:
    try:
        return (
            json.dumps(
                render.render_source(source, context), indent=2, ensure_ascii=False
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


async def _page(
    request: Request,
    session: AsyncSession,
    user: User,
    *,
    tid: int | None = None,
    body: str | None = None,
    error: str | None = None,
    ok: str | None = None,
    use_sample: bool = False,
    partial: bool = False,
):
    all_templates = await _all_templates(session)
    selected = None
    if tid is not None:
        selected = next((t for t in all_templates if t.id == tid), None)
    if selected is None:
        selected = await _default(session) or (all_templates[0] if all_templates else None)

    source = body if body is not None else (
        selected.body if selected else render.builtin_source()
    )

    alert = await _preview_alert(session, use_sample=use_sample)
    context = await _preview_context(session, alert)
    preview, preview_error = _render_preview(source, context)

    ctx = {
        "user": user,
        "templates": all_templates,
        "selected": selected,
        "source": source,
        "builtin": render.builtin_source(),
        "preview": preview,
        "preview_error": preview_error,
        "use_sample": use_sample,
        # Tell the operator when their own alerts carry no measurement, so an
        # empty value line reads as "fix your rule" rather than "this is
        # broken".
        "has_value": context.get("value_line") is not None,
        "preview_alert": alert,
        "context_vars": render.CONTEXT_VARS,
        "derived_keys": template_select.DERIVED_KEYS,
        "error": error,
        "ok": ok,
        "can_edit": user.role in (UserRole.editor, UserRole.admin),
    }
    if partial:
        return ui_templates.TemplateResponse(request, "ui/_template_preview.html", ctx)

    ctx["rules"] = await _rules(session)
    service, team = await _attribution(session, alert)
    ctx["explain"] = await template_select.explain(
        session, alert, MSTEAMS, service=service, team=team
    )
    return ui_templates.TemplateResponse(
        request,
        "ui/template_editor.html",
        ctx,
        status_code=status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK,
    )


async def _validates(session: AsyncSession, body: str) -> str | None:
    """None when the body renders against both contexts, else why not.

    Layer 1, kept per template: a rule makes a template reachable by alerts
    whoever wrote it never previewed.
    """
    for use_sample in (False, True):
        alert = await _preview_alert(session, use_sample=use_sample)
        try:
            render.render_source(body, await _preview_context(session, alert))
        except Exception as exc:  # noqa: BLE001
            which = "alert mẫu đầy đủ" if use_sample else "alert thật gần nhất"
            return f"Template không hợp lệ với {which}, chưa lưu: {exc}"
    return None


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@router.get("")
async def editor(
    request: Request,
    id: OptInt = None,
    sample: bool = False,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    return await _page(request, session, user, tid=id, use_sample=sample)


@router.post("/preview")
async def preview(
    request: Request,
    body: str = Form(...),
    id: OptInt = Form(None),
    sample: bool = Form(False),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Render without saving.

    Answers with just the preview pane when HTMX asks, so typing re-renders
    in place instead of reloading the page and losing the cursor.
    """
    partial = request.headers.get("hx-request") == "true"
    return await _page(
        request, session, user, tid=id, body=body,
        use_sample=sample, partial=partial,
    )


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------

@router.post("/new")
async def create_template(
    request: Request,
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    name = name.strip()
    if not name:
        return await _page(request, session, user, error="Tên template không được rỗng.")
    if await session.scalar(
        select(MessageTemplate.id).where(
            MessageTemplate.channel == MSTEAMS, MessageTemplate.name == name
        )
    ):
        return await _page(request, session, user, error=f"Đã có template tên {name!r}.")

    # A new template starts as a copy of the built-in rather than empty: an
    # empty body cannot render, so the first save would fail validation and
    # the editor would look broken.
    row = MessageTemplate(
        channel=MSTEAMS,
        name=name,
        body=render.builtin_source(),
        is_active=not await _default(session),
        updated_by_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session, user, "template.create",
        object_type="message_template", object_id=row.id, detail={"name": name},
    )
    await session.commit()
    return RedirectResponse(
        f"/settings/template?id={row.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/save")
async def save(
    request: Request,
    id: int = Form(...),
    body: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    row = await session.get(MessageTemplate, id)
    if row is None:
        return await _page(request, session, user, error="Template không còn tồn tại.")

    if problem := await _validates(session, body):
        return await _page(request, session, user, tid=id, body=body, error=problem)

    row.body = body
    row.updated_by_id = user.id
    await audit.record(
        session, user, "template.save",
        object_type="message_template", object_id=row.id,
        detail={"name": row.name, "bytes": len(body)},
    )
    await session.commit()
    return RedirectResponse(
        f"/settings/template?id={id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/default")
async def make_default(
    id: int = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Make this the catch-all used when no rule matches.

    One UPDATE, not clear-then-set: the partial unique index is checked per
    statement, so setting the new default before clearing the old one would
    raise mid-transaction.
    """
    await session.execute(
        update(MessageTemplate)
        .where(MessageTemplate.channel == MSTEAMS)
        .values(is_active=(MessageTemplate.id == id))
    )
    await audit.record(
        session, user, "template.default",
        object_type="message_template", object_id=id,
    )
    await session.commit()
    return RedirectResponse(
        f"/settings/template?id={id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/delete")
async def delete_template(
    request: Request,
    id: int = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    row = await session.get(MessageTemplate, id)
    if row is None:
        return RedirectResponse("/settings/template", status_code=status.HTTP_303_SEE_OTHER)

    others = [t for t in await _all_templates(session) if t.id != id]
    if row.is_active and others:
        # Being left with no catch-all is survivable -- the built-in file
        # takes over -- but it silently discards the card this person wrote,
        # which is worse than an error they can act on.
        return await _page(
            request, session, user, tid=id,
            error=(
                "Đây đang là template mặc định. Chọn template khác làm mặc định "
                "trước khi xoá."
            ),
        )

    name = row.name
    await session.delete(row)  # template_rules rows follow by ON DELETE CASCADE
    await audit.record(
        session, user, "template.delete",
        object_type="message_template", object_id=id, detail={"name": name},
    )
    await session.commit()
    return RedirectResponse("/settings/template", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/reset")
async def reset(
    id: int = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Put the built-in template's text back into this row."""
    row = await session.get(MessageTemplate, id)
    if row:
        row.body = render.builtin_source()
        row.updated_by_id = user.id
        await audit.record(
            session, user, "template.reset",
            object_type="message_template", object_id=id,
        )
        await session.commit()
    return RedirectResponse(
        f"/settings/template?id={id}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

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
    request: Request,
    template_id: int = Form(...),
    priority: int = Form(100),
    match_key: list[str] = Form(default=[]),
    match_value: list[str] = Form(default=[]),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    match = _parse_conditions(match_key, match_value)
    if not match:
        # An empty match would claim every alert. rule_matches already refuses
        # it, but there is no reason to store one.
        return await _page(
            request, session, user,
            error="Rule cần ít nhất một điều kiện — rule rỗng sẽ nhận mọi alert.",
        )

    rule = TemplateRule(
        template_id=template_id,
        priority=priority,
        match=match,
        description=description.strip() or None,
    )
    session.add(rule)
    await session.flush()
    await audit.record(
        session, user, "template_rule.create",
        object_type="template_rule", object_id=rule.id,
        detail={"match": match, "priority": priority},
    )
    await session.commit()
    return RedirectResponse("/settings/template", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rules/toggle")
async def toggle_rule(
    id: int = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    rule = await session.get(TemplateRule, id)
    if rule:
        rule.enabled = not rule.enabled
        await audit.record(
            session, user, "template_rule.toggle",
            object_type="template_rule", object_id=id,
            detail={"enabled": rule.enabled},
        )
        await session.commit()
    return RedirectResponse("/settings/template", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rules/delete")
async def delete_rule(
    id: int = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    rule = await session.get(TemplateRule, id)
    if rule:
        await session.delete(rule)
        await audit.record(
            session, user, "template_rule.delete",
            object_type="template_rule", object_id=id,
        )
        await session.commit()
    return RedirectResponse("/settings/template", status_code=status.HTTP_303_SEE_OTHER)
