"""Pick the message template for an alert.

One card for every alert stopped being enough as soon as severity and
criticality started to matter: a tier-1 payment outage and a disk filling up
on a staging box do not deserve the same layout.

Selection order, cheapest and most specific first:

1. the first enabled `template_rules` row whose conditions all match;
2. the channel's catch-all template (`message_templates.is_active`);
3. nothing -- the caller falls back to the built-in file template.

**Nothing here may raise.** A routing decision must never be the reason a card
does not go out: this module exists to choose a nicer card, and a bad choice
has to degrade to the plain one rather than to silence. Everything is wrapped,
and the exception path ends at the catch-all, then at None.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Alert,
    MessageTemplate,
    NotificationChannel,
    Service,
    Team,
    TemplateRule,
)
from app.services import globmatch

log = logging.getLogger(__name__)

# Keys OpsTrail derives and adds to the match context. They are listed here
# so the editor can show operators what is available beyond the alert's own
# labels -- a condition on a key that does not exist silently never matches,
# which is the least debuggable kind of rule.
#
# `severity` and `alertname` are deliberately NOT here: app/services/ingest.py
# copies both straight out of the alert's labels, so they are already in the
# context under their own names. Adding them would be a second way to say the
# same thing, differing only when they disagree.
DERIVED_KEYS: tuple[tuple[str, str], ...] = (
    ("status", "firing / resolved. Không bao giờ là label — Alertmanager gửi riêng"),
    ("tier", "Tier của service, khai trong Labels → Services. NULL = chưa phân loại"),
    (
        "resolved_service",
        "Service key sau khi gán nhãn — khác `service` khi nhãn đến từ rule",
    ),
    ("resolved_team", "Team key sau khi gán nhãn"),
    ("service", "Chỉ điền khi alert KHÔNG tự mang label `service`"),
    ("team", "Chỉ điền khi alert KHÔNG tự mang label `team`"),
)


def match_context(
    alert: Alert,
    *,
    service: Service | None = None,
    team: Team | None = None,
) -> dict[str, str]:
    """The keys a template rule can match on.

    The alert's own labels, plus what OpsTrail worked out. Where the two
    disagree the **label wins**: Alertmanager is the source of truth, and a
    tool that quietly overwrites the payload it was sent is a tool whose
    output nobody can reason about.

    That leaves a real gap, so it is filled rather than argued away. If an
    alert carries `service=payments-api-v2` and a label rule attributes it to
    the Service keyed `payments-api`, then `service` holds the label and
    `resolved_service` holds the attribution. Both are matchable, neither is
    invented, and the editor shows the operator exactly what this returned.
    """
    ctx: dict[str, str] = {}
    for key, value in (alert.labels or {}).items():
        if value in (None, ""):
            continue
        ctx[str(key).lower()] = str(value)

    # Never a label: Alertmanager sends status outside `labels`.
    ctx["status"] = alert.status.value

    if service is not None:
        ctx["resolved_service"] = service.key
        ctx.setdefault("service", service.key)
        if service.tier is not None:
            # Absent rather than empty when unclassified, so a rule with a
            # tier condition does not match instead of matching "".
            ctx["tier"] = str(service.tier)
    if team is not None:
        ctx["resolved_team"] = team.key
        ctx.setdefault("team", team.key)

    return ctx


def rule_matches(match: dict, context: dict[str, str]) -> bool:
    """Every entry in `match` must match the context key of that name.

    Same semantics as label rules (app/services/labeling.py): case-insensitive
    globs, all conditions must hold, and an empty dict matches nothing because
    a half-filled form must not become a rule that claims every alert.

    Deliberately not shared with `labeling._rule_matches`: that one takes raw
    labels and folds them itself, this one takes an already-built context. If
    the two were one function, a change to how alerts are attributed would
    silently change which template they get.
    """
    if not match:
        return False
    return all(
        globmatch.matches(str(pattern), context.get(str(key).lower()))
        for key, pattern in match.items()
    )


async def select_source(
    session: AsyncSession,
    alert: Alert,
    channel: NotificationChannel,
    *,
    service: Service | None = None,
    team: Team | None = None,
) -> tuple[str | None, str | None]:
    """(template body, note). A body of None means "use the built-in file".

    `note` is non-None only when something went wrong and the choice was
    downgraded; the caller records it alongside the delivery so a degraded
    send is visible rather than merely survivable.
    """
    try:
        context = match_context(alert, service=service, team=team)
        rows = (
            await session.execute(
                select(TemplateRule, MessageTemplate)
                .join(MessageTemplate, MessageTemplate.id == TemplateRule.template_id)
                .where(
                    TemplateRule.enabled.is_(True),
                    MessageTemplate.channel == channel,
                )
                # `id` as the tiebreaker: without it the winner among rules of
                # equal priority is whatever the planner returns first, which
                # can change between runs.
                .order_by(TemplateRule.priority, TemplateRule.id)
            )
        ).all()

        for rule, template in rows:
            if rule_matches(rule.match or {}, context):
                return template.body, None
    except Exception as exc:  # noqa: BLE001
        log.error("template selection failed, falling back: %s", exc)
        note = f"template rules unavailable, used the default: {type(exc).__name__}"
        return await _default_source(session, channel), note

    return await _default_source(session, channel), None


async def _default_source(
    session: AsyncSession, channel: NotificationChannel
) -> str | None:
    """The channel's catch-all, or None for the built-in file."""
    try:
        row = (
            await session.execute(
                select(MessageTemplate).where(
                    MessageTemplate.channel == channel,
                    MessageTemplate.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        return row.body if row else None
    except Exception as exc:  # noqa: BLE001
        log.error("default template unavailable, using the built-in: %s", exc)
        return None


async def explain(
    session: AsyncSession,
    alert: Alert,
    channel: NotificationChannel,
    *,
    service: Service | None = None,
    team: Team | None = None,
) -> dict:
    """Why this alert gets the template it gets.

    The editor renders this. "My rule does not fire" is otherwise answered by
    reading code and guessing at the context, and the gap between what an
    operator thinks the context holds and what it holds is where every one of
    these systems goes wrong.
    """
    context = match_context(alert, service=service, team=team)
    rows = (
        await session.execute(
            select(TemplateRule, MessageTemplate)
            .join(MessageTemplate, MessageTemplate.id == TemplateRule.template_id)
            .where(MessageTemplate.channel == channel)
            .order_by(TemplateRule.priority, TemplateRule.id)
        )
    ).all()

    evaluated, winner = [], None
    for rule, template in rows:
        hit = bool(rule.enabled) and rule_matches(rule.match or {}, context)
        if hit and winner is None:
            winner = template
        evaluated.append(
            {
                "rule": rule,
                "template": template,
                "matched": hit,
                "skipped": winner is not None and winner is not template,
            }
        )

    default = (
        await session.execute(
            select(MessageTemplate).where(
                MessageTemplate.channel == channel,
                MessageTemplate.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()

    return {
        "context": dict(sorted(context.items())),
        "rules": evaluated,
        "winner": winner,
        "default": default,
        # What actually renders, named the way an operator would say it.
        "effective": winner or default,
    }
