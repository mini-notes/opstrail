"""Edit the MS Teams card template from the UI.

Templates live in the database so changing a card does not need a redeploy.
Two safety layers keep a typo from silencing every alert:

* saving renders the template against a sample alert and parses the result,
  so broken JSON never reaches the table;
* sending falls back to the built-in template if a saved one fails on a
  particular alert (see notifiers/render.render_card_safe).
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    Alert,
    AlertStatus,
    MessageTemplate,
    NotificationChannel,
    Service,
    Team,
    User,
    UserRole,
)
from app.notifiers import render
from app.security import current_user, require_role
from app.services import audit, mention
from app.templating import templates as ui_templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/settings/template", tags=["template"], include_in_schema=False)


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


async def _active(session: AsyncSession) -> MessageTemplate | None:
    return (
        await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.channel == NotificationChannel.msteams,
                MessageTemplate.is_active.is_(True),
            ).limit(1)
        )
    ).scalar_one_or_none()


async def _preview_context(session: AsyncSession, *, use_sample: bool = False) -> dict:
    """Context for the preview.

    Defaults to the most recent REAL alert: a preview against invented data
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
    alert = alert or _sample_alert()

    service_name = team_name = None
    if alert.service_id:
        svc = await session.get(Service, alert.service_id)
        service_name = svc.name if svc else None
    if alert.team_id:
        team = await session.get(Team, alert.team_id)
        team_name = team.name if team else None

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
        service_name=service_name,
        team_name=team_name,
        detail_url=f"http://localhost:8000/alerts/{alert.id or 0}",
        mention_entities=entities,
        mention_text=text,
        owner_names=names,
    )


async def _render_page(
    request: Request, session: AsyncSession, user: User,
    *, body=None, error=None, ok=None, use_sample: bool = False,
):
    row = await _active(session)
    source = body if body is not None else (row.body if row else render.builtin_source())

    context = await _preview_context(session, use_sample=use_sample)
    preview = preview_error = None
    try:
        preview = json.dumps(
            render.render_source(source, context), indent=2, ensure_ascii=False
        )
    except Exception as exc:  # noqa: BLE001
        preview_error = str(exc)

    # Tell the operator when their own alerts carry no measurement, so an
    # empty value line reads as "fix your rule" rather than "this is broken".
    has_value = context.get("value_line") is not None

    return ui_templates.TemplateResponse(
        request,
        "ui/template_editor.html",
        {
            "user": user,
            "source": source,
            "using_custom": row is not None,
            "builtin": render.builtin_source(),
            "preview": preview,
            "preview_error": preview_error,
            "use_sample": use_sample,
            "has_value": has_value,
            "error": error,
            "ok": ok,
            "can_edit": user.role in (UserRole.editor, UserRole.admin),
        },
        status_code=status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK,
    )


@router.get("")
async def editor(
    request: Request,
    sample: bool = False,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    return await _render_page(request, session, user, use_sample=sample)


@router.post("/preview")
async def preview(
    request: Request,
    body: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Render without saving, so a bad edit can be seen before it is stored."""
    return await _render_page(request, session, user, body=body)


@router.post("/save")
async def save(
    request: Request,
    body: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    # Layer 1: never store something that cannot render. Checked against BOTH
    # the latest real alert and the fully-populated sample, so a template that
    # only works when every field is present is caught here rather than at
    # 3am on a sparse alert.
    for use_sample in (False, True):
        try:
            render.render_source(
                body, await _preview_context(session, use_sample=use_sample)
            )
        except Exception as exc:  # noqa: BLE001
            which = "alert mẫu đầy đủ" if use_sample else "alert thật gần nhất"
            return await _render_page(
                request, session, user, body=body,
                error=f"Template không hợp lệ với {which}, chưa lưu: {exc}",
            )

    row = await _active(session)
    if row:
        row.body = body
        row.updated_by_id = user.id
    else:
        session.add(
            MessageTemplate(
                channel=NotificationChannel.msteams,
                name="msteams-custom",
                body=body,
                is_active=True,
                updated_by_id=user.id,
            )
        )
    await audit.record(
        session, user, "template.save",
        object_type="message_template", object_id="msteams",
        detail={"bytes": len(body)},
    )
    await session.commit()
    return RedirectResponse("/settings/template", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/reset")
async def reset(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.editor)),
):
    """Drop the custom template and go back to the one shipped in the image."""
    row = await _active(session)
    if row:
        await session.delete(row)
        await audit.record(
            session, user, "template.reset",
            object_type="message_template", object_id="msteams",
        )
        await session.commit()
    return RedirectResponse("/settings/template", status_code=status.HTTP_303_SEE_OTHER)
