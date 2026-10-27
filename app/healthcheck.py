"""Container worker probe; failures exit nonzero without leaking DB credentials."""

import asyncio
import socket
from datetime import UTC, datetime

from app.db import SessionLocal, engine
from app.models import WorkerState


async def check():
    try:
        async with asyncio.timeout(5), SessionLocal() as session:
            row = await session.get(WorkerState, f"dispatch:{socket.gethostname()[:40]}")
            if row is None or (datetime.now(UTC) - row.updated_at).total_seconds() > 60:
                raise SystemExit(1)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(check())
