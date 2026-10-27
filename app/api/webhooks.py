"""Alert intake endpoints.

Authentication here is a shared bearer token, NOT the admin session:
Alertmanager is a machine client. Configure it on the receiver:

    receivers:
      - name: opstrail
        webhook_configs:
          - url: http://opstrail:8000/api/v1/webhooks/alertmanager
            send_resolved: true
            http_config:
              authorization:
                type: Bearer
                credentials: <INTAKE_TOKEN>
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.schemas.alertmanager import AlertmanagerPayload
from app.services import ingest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhooks", tags=["intake"])


def require_intake_token(authorization: str | None = Header(default=None)) -> None:
    """Constant-time-ish bearer check. An empty configured token disables the
    check entirely -- only acceptable on a trusted network."""
    settings = get_settings()
    if not settings.intake_token:
        return
    expected = f"Bearer {settings.intake_token}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid intake token")


@router.post(
    "/alertmanager",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_intake_token)],
)
async def receive_alertmanager(
    payload: AlertmanagerPayload,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Persist first, notify after.

    Returning 2xx means the alerts are committed. If persisting raises, the
    500 propagates and Alertmanager retries -- that is the delivery
    guarantee, so this handler deliberately does NOT swallow exceptions.
    """
    if not payload.alerts:
        return {"received": 0, "queued": 0}

    result = await ingest.ingest_payload(session, payload, commit=False)
    # Only new / state-changed alerts ring the bell; see IngestResult.
    note_ids = result.notification_ids

    await session.commit()

    return {
        "received": len(result.alert_ids),
        "notified": len(result.notify_ids),
        "queued": len(note_ids),
    }
