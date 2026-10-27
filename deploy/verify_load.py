"""Read-only reconciliation of a load_test.py run against its disposable DB."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import aliased

from app.models import Alert, Notification


async def verify(report):
    dsn = os.environ["DATABASE_URL"]
    if not (make_url(dsn).database or "").endswith("_test"):
        raise SystemExit("verification requires a disposable *_test database")
    result = json.loads(report.read_text())
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            alert_ids = select(Alert.id).where(Alert.labels["run_id"].astext == result["run_id"])
            alerts = (
                await conn.execute(
                    select(func.count()).select_from(Alert).where(Alert.id.in_(alert_ids))
                )
            ).scalar_one()
            deliveries = (
                await conn.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.alert_id.in_(alert_ids))
                )
            ).scalar_one()
            missing = (
                await conn.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(
                        Notification.alert_id.in_(alert_ids),
                        (Notification.payload.is_(None) | Notification.event_snapshot.is_(None)),
                    )
                )
            ).scalar_one()
            parent = aliased(Notification)
            orphans = (
                await conn.execute(
                    select(func.count())
                    .select_from(Notification)
                    .outerjoin(parent, parent.id == Notification.digest_parent_id)
                    .where(
                        Notification.alert_id.in_(alert_ids),
                        Notification.digest_parent_id.is_not(None),
                        parent.id.is_(None),
                    )
                )
            ).scalar_one()
        summary = {
            "run_id": result["run_id"],
            "alerts": alerts,
            "deliveries": deliveries,
            "missing_snapshots": missing,
            "orphan_digest_members": orphans,
        }
        print(json.dumps(summary, indent=2))
        if (
            alerts != result["expected_alerts"]
            or deliveries != result["expected_deliveries"]
            or missing
            or orphans
        ):
            raise SystemExit(1)
        return summary
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    asyncio.run(verify(parser.parse_args().report))
