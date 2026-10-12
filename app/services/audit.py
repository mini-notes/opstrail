"""Append-only record of who changed what.

The `audit_log` table existed from the first migration but nothing wrote to
it. Every state change an operator makes through the UI goes through here, so
"who demoted this account?" has an answer that does not depend on container
logs surviving a restart.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User

log = logging.getLogger(__name__)


async def record(
    session: AsyncSession,
    actor: User | None,
    action: str,
    *,
    object_type: str | None = None,
    object_id: str | int | None = None,
    detail: dict | None = None,
) -> None:
    """Stage an audit row. The caller commits it with its own change, so an
    action and its audit entry land in the same transaction or neither does.

    `actor` is None for things OpsTrail does by itself -- intake handing a
    manually tagged alert back to the label its own payload now carries, for
    instance. Those changes are the ones most worth recording precisely
    BECAUSE nobody did them: an attribution that moves with no person behind
    it is otherwise indistinguishable from a bug.
    """
    session.add(
        AuditLog(
            user_id=actor.id if actor else None,
            action=action,
            object_type=object_type,
            object_id=str(object_id) if object_id is not None else None,
            detail=detail,
        )
    )
    log.info(
        "audit: %s %s %s=%s %s",
        actor.username if actor else "system", action, object_type, object_id,
        detail or "",
    )
