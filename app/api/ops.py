"""Bounded readiness and authenticated Prometheus operational metrics."""

import asyncio
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select, text

from app import telemetry
from app.config import get_settings
from app.db import SessionLocal
from app.models import Notification, NotificationStatus, WorkerState

router = APIRouter(tags=["ops"])


@router.get("/readyz")
async def readyz():
    try:
        async with asyncio.timeout(3), SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(authorization: str | None = Header(default=None)):
    token = get_settings().ops_metrics_token
    if not token or not secrets.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "invalid metrics token")
    async with asyncio.timeout(5), SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Notification.channel, Notification.status, func.count(Notification.id)
                ).group_by(Notification.channel, Notification.status)
            )
        ).all()
        oldest = (
            await session.execute(
                select(func.min(Notification.created_at)).where(
                    Notification.status == NotificationStatus.pending,
                    Notification.digest_parent_id.is_(None),
                )
            )
        ).scalar_one()
        workers = (await session.execute(select(WorkerState))).scalars().all()
    now = datetime.now(UTC)
    lines = telemetry.exposition() + ["# TYPE opstrail_deliveries gauge"]
    for channel, status, count in rows:
        lines.append(
            f'opstrail_deliveries{{channel="{channel.value}",status="{status.value}"}} {count}'
        )
    oldest_age = max((now - oldest).total_seconds(), 0) if oldest else 0
    lines += [
        "# TYPE opstrail_pending_oldest_age_seconds gauge",
        f"opstrail_pending_oldest_age_seconds {oldest_age}",
    ]
    lines.append("# TYPE opstrail_worker_last_success_timestamp_seconds gauge")
    for worker in workers:
        # Names come only from our worker entry points, never user labels.
        if worker.name in {"dispatch", "maintenance", "retention", "backup"}:
            lines.append(
                f'opstrail_worker_last_success_timestamp_seconds{{component="{worker.name}"}} '
                f"{worker.updated_at.timestamp()}"
            )
    return "\n".join(lines) + "\n"
