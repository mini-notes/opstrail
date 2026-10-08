"""JSON feeds for the dashboard charts.

One endpoint, one round trip: every chart on the page shares the same Scope,
so fetching them separately would risk charts disagreeing with each other
while a filter is being applied.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.params import OptInt
from app.db import get_session
from app.models import User
from app.security import current_user
from app.services import stats

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


@router.get("/summary")
async def summary(
    days: int = Query(30, ge=1, le=365),
    service_id: OptInt = None,
    team_id: OptInt = None,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(current_user),
) -> dict:
    scope = stats.Scope(days=days, service_id=service_id, team_id=team_id)
    return {
        "scope": {"days": days, "service_id": service_id, "team_id": team_id},
        "headline": await stats.headline(session, scope),
        "alerts_per_day": await stats.alerts_per_day(session, scope),
        "top_services": await stats.top_services(session, scope),
        "severity": await stats.severity_breakdown(session, scope),
        "noisiest": await stats.noisiest_alerts(session, scope),
        "durations": await stats.duration_percentiles(session, scope),
        "heatmap": await stats.hour_weekday_heatmap(session, scope),
        "label_coverage": await stats.label_coverage(session, scope),
        "notifications": await stats.notification_health(session, scope),
    }
