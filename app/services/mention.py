"""Decide who to @mention on an alert, and build the Teams mention block.

Both halves of a Teams mention are produced here, from one source:

* the `<at>Name</at>` token that goes inside a TextBlock, and
* the matching entry in `msteams.entities`.

Teams links the two by comparing the `text` field **character for character**.
Building them in two places is how they drift, and a drifted mention renders
as literal `<at>Name</at>` in the channel instead of highlighting the person.

Only TextBlock and FactSet elements can display a mention, so the template
puts the token in a TextBlock.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Alert, Owner, Service, ServiceOwner, Team, TeamOwner

log = logging.getLogger(__name__)

# Lower number = more severe. Mirrors app/api/sorting.py:severity_rank so the
# table ordering and the mention threshold agree on what "worse" means.
SEVERITY_RANK = {
    "critical": 1, "fatal": 1, "alarm": 1, "p1": 1,
    "error": 2, "high": 2, "p2": 2,
    "warning": 3, "warn": 3, "p3": 3,
    "info": 4, "information": 4, "low": 4, "p4": 4,
}
UNKNOWN_RANK = 5


def rank(severity: str | None) -> int:
    if not severity:
        return UNKNOWN_RANK
    return SEVERITY_RANK.get(severity.strip().lower(), UNKNOWN_RANK)


def meets_threshold(severity: str | None, threshold: str | None) -> bool:
    """True when `severity` is at least as bad as `threshold`.

    Compared by rank, never as strings: `"critical" >= "warning"` is False
    alphabetically, which is the opposite of what an operator means.
    """
    if not threshold:
        return False
    return rank(severity) <= rank(threshold)


@dataclass(slots=True)
class MentionPlan:
    owners: list[Owner]
    threshold: str | None
    should_mention: bool
    source: str | None = None  # "service" | "team" | None

    @property
    def names(self) -> list[str]:
        return [o.mention_name for o in self.owners]


def _mention_id(owner: Owner) -> str | None:
    """Which identifier goes into `mentioned.id`.

    Tenants differ: some accept the UPN/email, others need the AAD object id
    (`8:orgid:<guid>` form). `MSTEAMS_MENTION_ID_FIELD` chooses, and the other
    value is used as a fallback so a half-filled directory still mentions
    someone rather than nobody.
    """
    settings = get_settings()
    if settings.msteams_mention_id_field == "aad":
        return owner.aad_object_id or owner.upn or owner.email
    return owner.upn or owner.email or owner.aad_object_id


async def _service_owners(session: AsyncSession, service_id: int) -> list[Owner]:
    rows = (
        await session.execute(
            select(ServiceOwner)
            .where(ServiceOwner.service_id == service_id)
            .order_by(ServiceOwner.role, ServiceOwner.owner_id)
        )
    ).unique().scalars().all()
    return [r.owner for r in rows if r.owner and r.owner.is_active]


async def _team_owners(session: AsyncSession, team_id: int) -> list[Owner]:
    rows = (
        await session.execute(
            select(TeamOwner)
            .where(TeamOwner.team_id == team_id)
            .order_by(TeamOwner.role, TeamOwner.owner_id)
        )
    ).unique().scalars().all()
    return [r.owner for r in rows if r.owner and r.owner.is_active]


async def plan_for(session: AsyncSession, alert: Alert) -> MentionPlan:
    """Work out who owns this alert and whether it is bad enough to mention.

    Service owners take precedence over team owners rather than being merged:
    once a service names its own people, pulling in the whole team as well is
    the fastest way to get the channel muted.
    """
    settings = get_settings()

    owners: list[Owner] = []
    source = None
    if alert.service_id:
        owners = await _service_owners(session, alert.service_id)
        if owners:
            source = "service"
    if not owners and alert.team_id:
        owners = await _team_owners(session, alert.team_id)
        if owners:
            source = "team"

    # Threshold: the service's own setting, else the team's, else global.
    threshold = None
    if alert.service_id:
        service = await session.get(Service, alert.service_id)
        threshold = service.mention_severity if service else None
    if threshold is None and alert.team_id:
        team = await session.get(Team, alert.team_id)
        threshold = team.mention_severity if team else None
    if threshold is None:
        threshold = settings.mention_severity_default

    should = bool(owners) and meets_threshold(alert.severity, threshold)
    return MentionPlan(
        owners=owners, threshold=threshold, should_mention=should, source=source
    )


def build_entities(owners: list[Owner]) -> list[dict]:
    """`msteams.entities` for the card. Owners with no usable identifier are
    skipped -- a mention pointing at nothing renders as raw markup."""
    out = []
    for owner in owners:
        ident = _mention_id(owner)
        if not ident:
            log.warning(
                "owner %s has no upn/email/aad id; skipping mention", owner.id
            )
            continue
        out.append(
            {
                "type": "mention",
                "text": f"<at>{owner.mention_name}</at>",
                "mentioned": {"id": ident, "name": owner.mention_name},
            }
        )
    return out


def build_text(entities: list[dict], suffix: str = "— cần xử lý") -> str:
    """The TextBlock that carries the tokens.

    Tokens are taken from `entities`, not rebuilt from the owner list, so the
    two can never disagree.
    """
    return " ".join(e["text"] for e in entities) + (f" {suffix}" if suffix else "")
