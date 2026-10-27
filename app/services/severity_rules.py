"""Adjust an alert's severity by context -- in practice, by the clock.

A payments outage at 10am on a Tuesday and the same outage at 2am on a Sunday
do not deserve the same treatment, but Prometheus knows one severity per
rule. These rules close that gap: "during the trading session, raise this a
band."

The result goes in `alerts.effective_severity`. `alerts.severity` keeps what
Alertmanager sent, untouched -- OpsTrail does not edit the system of record,
and the ingest UPSERT would refresh an overwrite away on the next repeat.

Four guard rails that no rule can get past, because the cost of each is paid
at 3am by somebody who did not write it:

1. Nothing goes below INFO or above CRITICAL. Enforced in severity.shift().
2. Quieting an alert changes who gets mentioned. It NEVER stops the alert
   being stored or delivered. "Fewer pages out of hours" must not quietly
   become "no alerts out of hours", which is when the fewest people are
   watching.
3. A resolved alert is never escalated. Nobody needs waking for something
   that already stopped.
4. Nothing here raises. A failure computing severity falls back to the
   source value; it must not be able to block an alert.
"""

import asyncio
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Alert, AlertStatus, Service, SeverityRule, Team
from app.services import audit, severity, template_select

log = logging.getLogger(__name__)

ACTIONS = ("bump", "set")
WEEKDAY_NAMES = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")


def business_tz() -> ZoneInfo:
    """The exchange's timezone, not the viewer's.

    Falls back to UTC rather than raising: a mistyped zone name should make
    the windows wrong in an obvious way, not take alerting down.
    """
    name = get_settings().business_timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.error("BUSINESS_TIMEZONE=%r is not a known zone; using UTC", name)
        return ZoneInfo("UTC")


def _parse_clock(raw: str | None) -> time | None:
    if not raw:
        return None
    try:
        hour, _, minute = raw.strip().partition(":")
        return time(int(hour), int(minute or 0))
    except (ValueError, TypeError):
        return None


def in_window(window: dict | None, moment: datetime) -> bool:
    """Is `moment` inside this rule's window, read in the business timezone?

    An empty window means "whenever" -- a rule can care only about labels.
    """
    if not window:
        return True

    local = moment.astimezone(business_tz())
    days = window.get("days") or []
    start = _parse_clock(window.get("from"))
    end = _parse_clock(window.get("to"))

    # ISO weekday: 1 is Monday.
    if days and local.isoweekday() not in days:
        return False
    if start is None or end is None:
        return True

    now = local.time()
    if start <= end:
        return start <= now <= end
    # A window that wraps midnight (22:00 -> 06:00) is one window, not an
    # empty one. Reading it as start<=now<=end would match nothing, and the
    # out-of-hours rules people actually want all look like this.
    return now >= start or now <= end


def describe_window(window: dict | None) -> str:
    """How the window reads on screen. Also used in audit detail."""
    if not window:
        return "mọi lúc"
    days = window.get("days") or []
    when = (
        " ".join(WEEKDAY_NAMES[d - 1] for d in sorted(days) if 1 <= d <= 7) if days else "mọi ngày"
    )
    start, end = window.get("from"), window.get("to")
    return f"{when} {start}–{end}" if start and end else when


def apply_action(source: str | None, action: str, value: str) -> str | None:
    """The severity a rule produces, or None when it produces nothing."""
    if action == "set":
        banded = severity.band(value)
        return banded if banded in severity.BANDS else None
    try:
        steps = int(value)
    except (TypeError, ValueError):
        return None
    return severity.shift(source, steps)


async def active_rules(session: AsyncSession) -> list[SeverityRule]:
    return list(
        (
            await session.execute(
                select(SeverityRule)
                .where(SeverityRule.enabled.is_(True))
                # `id` as the tiebreaker, as everywhere else: without it the
                # winner among equal priorities is whatever the planner
                # returns first, which can change between runs.
                .order_by(SeverityRule.priority, SeverityRule.id)
            )
        )
        .scalars()
        .all()
    )


def evaluate_against(
    alert: Alert,
    rules: list[SeverityRule],
    *,
    service=None,
    team=None,
    now: datetime | None = None,
) -> tuple[str | None, SeverityRule | None]:
    """(effective severity, the rule that decided it).

    Pure, so the sweeper can evaluate a page of alerts against one loaded
    rule list, and so tests can hand it a fixed `now` instead of waiting for
    Tuesday.
    """
    moment = now or datetime.now(business_tz())
    context = template_select.match_context(alert, service=service, team=team)

    for rule in rules:
        if not template_select.rule_matches(rule.match or {}, context):
            continue
        if not in_window(rule.time_window, moment):
            continue
        result = apply_action(alert.severity, rule.action, rule.value)
        if result is None:
            # A rule that cannot produce a severity -- `set` to a band that
            # does not exist, a source severity nobody mapped -- is skipped
            # rather than allowed to blank the field.
            log.warning(
                "severity rule %s (%s) produced nothing for severity=%r",
                rule.id,
                rule.name,
                alert.severity,
            )
            continue
        # First match wins, like every other rule table here.
        return (None if result == severity.band(alert.severity) else result), rule
    return None, None


async def evaluate(
    session: AsyncSession,
    alert: Alert,
    *,
    service=None,
    team=None,
    now: datetime | None = None,
) -> tuple[str | None, SeverityRule | None]:
    """As above, loading the rules. Never raises."""
    try:
        rules = await active_rules(session)
        if not rules:
            return None, None
        return evaluate_against(alert, rules, service=service, team=team, now=now)
    except Exception:  # noqa: BLE001
        log.exception("severity evaluation failed for alert %s", alert.id)
        return None, None


# ---------------------------------------------------------------------------
# periodic re-evaluation
# ---------------------------------------------------------------------------

REEVALUATE_BATCH = 500


async def reevaluate_once() -> int:
    """Recompute effective severity for every firing alert. Rows changed.

    Needed because a window opens while an alert is already firing. An alert
    that started at 08:45 is a trading-hours problem at 09:00, but intake
    only reconsiders it when Alertmanager re-sends -- and `repeat_interval`
    is typically one to four hours, so without this you find out at 11:00.

    Only escalations notify, and only when they cross the mention threshold.
    A downgrade updates the row in silence: nobody wants to be paged to be
    told something got less urgent.
    """
    from app.db import SessionLocal
    from app.services import dispatch, mention

    changed = 0
    escalated: list[int] = []

    async with SessionLocal() as session:
        rules = await active_rules(session)
        if not rules:
            # Nothing configured: no scan, no queries, no cost.
            return 0

        alerts = (
            (
                await session.execute(
                    select(Alert)
                    .where(Alert.status == AlertStatus.firing)
                    .order_by(Alert.id.desc())
                    .limit(REEVALUATE_BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for alert in alerts:
            service = await session.get(Service, alert.service_id) if alert.service_id else None
            team = await session.get(Team, alert.team_id) if alert.team_id else None
            result, rule = evaluate_against(alert, rules, service=service, team=team)
            if result == alert.effective_severity:
                continue

            before = alert.acting_severity
            alert.effective_severity = result
            changed += 1

            await audit.record(
                session,
                None,
                "alert.severity.overridden",
                object_type="alert",
                object_id=alert.id,
                detail={
                    "from": before,
                    "to": alert.acting_severity,
                    "rule": rule.name if rule else None,
                    "window": describe_window(rule.time_window) if rule else None,
                },
            )

            # Worse than it was, and now past the threshold that decides who
            # gets named. Anything less is not worth interrupting a shift.
            if severity.rank(alert.acting_severity) < severity.rank(before):
                try:
                    plan = await mention.plan_for(session, alert)
                    if plan.should_mention:
                        escalated.append(alert.id)
                except Exception:  # noqa: BLE001
                    log.exception("mention check failed for alert %s", alert.id)

        if changed:
            await session.flush()
            log.info("severity: adjusted %d firing alert(s)", changed)

        if escalated:
            await dispatch.queue_for_alerts(session, escalated)
            log.info("severity: escalated %d alert(s) past the mention threshold", len(escalated))

        await session.commit()

    return changed


async def reevaluate_loop(interval_seconds: float = 60.0) -> None:
    """Background sweeper started on app startup."""
    while True:
        try:
            await reevaluate_once()
        except Exception:  # noqa: BLE001 - a sweeper must never die
            log.exception("severity sweep failed")
        await asyncio.sleep(interval_seconds)
