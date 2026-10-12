"""Resolve a service/team for an incoming alert.

Precedence, cheapest and most trustworthy first:

1. ``payload``  -- the Prometheus alerting rule carried `service` / `team`
                   labels itself. This is the source of truth; prefer fixing
                   rules over adding mappings here.
2. ``rule``     -- a `label_rules` row matched. For rules we cannot edit.
3. ``manual``   -- nothing matched; an operator tags it later. Rows left at
                   `label_source IS NULL` are the backlog of missing rules.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LabelRule, LabelSource, Service, Team
from app.services import globmatch

# Label keys we accept as naming a service, in order of preference.
# Case-insensitive; `job` is last because it often names a scrape target
# rather than a service.
SERVICE_LABEL_KEYS = (
    "service",
    "service_name",
    "servicename",
    "app",
    "application",
    "component",
    "job",
)
TEAM_LABEL_KEYS = ("team", "squad", "owner", "owner_team")


@dataclass(slots=True)
class Attribution:
    service_id: int | None = None
    team_id: int | None = None
    source: LabelSource | None = None


def _lookup(labels: dict[str, str], keys: tuple[str, ...]) -> str | None:
    """Case-insensitive lookup over an ordered key preference list."""
    folded = {k.lower(): v for k, v in labels.items() if v}
    for key in keys:
        if value := folded.get(key):
            return value.strip()
    return None


def _rule_matches(match: dict, labels: dict[str, str]) -> bool:
    """Every entry in `match` must match the alert's label of that name.

    Values are glob patterns (`pg-prod-*`), matched case-insensitively by
    app/services/globmatch.py -- the same module the backfill preview
    translates to SQL, so a rule behaves identically at ingest time and when
    applied to stored alerts.

    An empty match dict would match everything, which is almost never what an
    operator means, so it is treated as non-matching.
    """
    if not match:
        return False
    folded = {k.lower(): v for k, v in labels.items()}
    return all(
        globmatch.matches(str(pattern), folded.get(str(key).lower()))
        for key, pattern in match.items()
    )


async def _by_key(session: AsyncSession, model, key: str):
    """Look up a Service or Team by key, ignoring case.

    Everything else in this module folds case -- the label keys in `_lookup`,
    and both keys and values in `_rule_matches`. An exact, case-sensitive
    comparison here was the one asymmetry, and it failed silently: an alert
    labelled `service=Payments-API` simply fell through to the rules and then
    to the unlabelled backlog, with nothing to say why.

    Ordered so the answer is stable if two keys ever differ only by case.
    """
    return (
        await session.execute(
            select(model)
            .where(func.lower(model.key) == key.lower())
            .order_by(model.key)
            .limit(1)
        )
    ).scalar_one_or_none()


async def resolve(session: AsyncSession, labels: dict[str, str]) -> Attribution:
    """Attribute an alert to a service/team using the precedence above."""
    # --- 1. labels carried by the payload itself ---
    service_key = _lookup(labels, SERVICE_LABEL_KEYS)
    team_key = _lookup(labels, TEAM_LABEL_KEYS)

    service_id = team_id = None
    if service_key:
        service = await _by_key(session, Service, service_key)
        if service:
            service_id = service.id
            team_id = service.team_id
    if team_key:
        team = await _by_key(session, Team, team_key)
        if team:
            team_id = team.id

    if service_id or team_id:
        return Attribution(service_id, team_id, LabelSource.payload)

    # --- 2. label_rules, lowest priority number wins ---
    rules = (
        (
            await session.execute(
                select(LabelRule)
                .where(LabelRule.enabled.is_(True))
                .order_by(LabelRule.priority, LabelRule.id)
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        if _rule_matches(rule.match, labels):
            resolved_team = rule.team_id
            if rule.service_id and resolved_team is None:
                service = await session.get(Service, rule.service_id)
                if service:
                    resolved_team = service.team_id
            return Attribution(rule.service_id, resolved_team, LabelSource.rule)

    # --- 3. unattributed; waiting for a manual tag or a new rule ---
    return Attribution()
