"""Apply label rules to alerts that are already stored.

Why this exists: at ingest time a rule only reaches an alert that Alertmanager
re-sends. A resolved alert is never re-sent, so an alert that arrived before
its rule existed stays unlabelled for ever. Adding a rule would silently fix
the future and leave the history broken.

The resolution itself is NOT reimplemented here -- it calls
`labeling.resolve()`, the same function `services/ingest.py` uses. That is the
whole point: the preview shown to an operator is the result ingest would
produce, including rule priority, rather than a second implementation that
can drift.

Scope is always `label_source IS NULL`. Manual tags are an operator decision
and `rule` tags already matched something; neither is ours to overwrite.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Service, Team
from app.services import labeling

log = logging.getLogger(__name__)

BATCH_SIZE = 500
# Preview reads every unlabelled alert; a cap keeps one request bounded if the
# backlog is enormous. The number is reported so the UI never implies it saw
# more than it did.
PREVIEW_MAX = 20_000


@dataclass(slots=True)
class PreviewGroup:
    """One row of the preview table."""

    alertname: str
    service: str | None
    team: str | None
    count: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Preview:
    groups: list[PreviewGroup]
    matched: int
    unlabelled_total: int
    scanned: int
    truncated: bool

    @property
    def unmatched(self) -> int:
        return self.scanned - self.matched


async def _unlabelled_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count(Alert.id)).where(Alert.label_source.is_(None))
        )
    ).scalar_one()


async def _name_maps(session: AsyncSession) -> tuple[dict, dict]:
    services = (await session.execute(select(Service))).scalars().all()
    teams = (await session.execute(select(Team))).scalars().all()
    return ({s.id: s.key for s in services}, {t.id: t.key for t in teams})


async def preview(session: AsyncSession) -> Preview:
    """What would happen, grouped by alertname and resulting attribution."""
    total = await _unlabelled_count(session)
    svc_names, team_names = await _name_maps(session)

    rows = (
        (
            await session.execute(
                select(Alert)
                .where(Alert.label_source.is_(None))
                .order_by(Alert.starts_at.desc())
                .limit(PREVIEW_MAX)
            )
        )
        .scalars()
        .all()
    )

    grouped: dict[tuple, PreviewGroup] = {}
    matched = 0
    for alert in rows:
        attribution = await labeling.resolve(session, alert.labels or {})
        if attribution.source is None:
            continue
        matched += 1
        key = (alert.alertname or "(unnamed)",
               attribution.service_id, attribution.team_id)
        group = grouped.get(key)
        if group is None:
            group = PreviewGroup(
                alertname=alert.alertname or "(unnamed)",
                service=svc_names.get(attribution.service_id),
                team=team_names.get(attribution.team_id),
            )
            grouped[key] = group
        group.count += 1
        if len(group.samples) < 3 and alert.summary:
            group.samples.append(alert.summary)

    groups = sorted(grouped.values(), key=lambda g: g.count, reverse=True)
    return Preview(
        groups=groups,
        matched=matched,
        unlabelled_total=total,
        scanned=len(rows),
        truncated=len(rows) >= PREVIEW_MAX,
    )


async def apply(session: AsyncSession) -> int:
    """Label every unlabelled alert a rule now claims. Returns rows changed.

    Walks forward on a primary-key cursor rather than re-querying
    `label_source IS NULL` each round. Alerts that no rule claims STAY null,
    so a plain LIMIT/OFFSET-free re-query would hand back the same unmatched
    rows every iteration -- quadratic work, and a loop that only ends on the
    final batch.

    Committed per batch so a large backlog never holds one long transaction
    open against the table intake is writing to.
    """
    changed = 0
    last_id = 0

    while True:
        rows = (
            (
                await session.execute(
                    select(Alert)
                    .where(Alert.label_source.is_(None), Alert.id > last_id)
                    .order_by(Alert.id)
                    .limit(BATCH_SIZE)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            break

        last_id = rows[-1].id
        for alert in rows:
            attribution = await labeling.resolve(session, alert.labels or {})
            if attribution.source is None:
                continue
            alert.service_id = attribution.service_id
            alert.team_id = attribution.team_id
            # Whatever resolve() actually decided, not a hardcoded `rule`.
            # Usually it IS a rule -- payload attribution would have been
            # applied at ingest -- but not always: a service declared AFTER
            # the alert arrived makes its label resolve for the first time
            # here, and stamping that `rule` under-reports payload coverage
            # on the dashboard for ever.
            alert.label_source = attribution.source
            changed += 1

        await session.commit()

    log.info("backfill: labelled %d alert(s)", changed)
    return changed
