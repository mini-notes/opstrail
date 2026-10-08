"""Turn an Alertmanager webhook payload into persisted Alert rows.

Contract with Alertmanager
--------------------------
* Persist happens BEFORE any outbound notification, so a dead Teams flow can
  never lose the alert record.
* The endpoint answers 2xx only once the rows are committed. Anything else
  returns 5xx so Alertmanager retries -- that is the delivery guarantee.
* Rows are UPSERTed on (fingerprint, starts_at). Alertmanager re-sends the
  same alert every `repeat_interval`; INSERT would multiply one alert into
  dozens of rows and wreck any statistic built on top.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, AlertStatus, LabelSource
from app.schemas.alertmanager import AlertmanagerPayload
from app.services import labeling

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestResult:
    """What intake did, split by whether it is worth notifying about.

    `notify_ids` holds only alerts that are NEW or that CHANGED STATE.
    Alertmanager re-sends every `repeat_interval` (often 1-4h, and 1m in the
    dev stack); re-posting an unchanged alert to Teams each time would turn
    one incident into a wall of identical cards. Repeats still update the
    stored row -- they just do not ring the bell again.
    """

    alert_ids: list[int]
    notify_ids: list[int]


async def ingest_payload(
    session: AsyncSession, payload: AlertmanagerPayload
) -> IngestResult:
    """Persist every alert in the payload."""
    alert_ids: list[int] = []
    notify_ids: list[int] = []

    for am_alert in payload.alerts:
        labels = payload.effective_labels(am_alert)
        annotations = payload.effective_annotations(am_alert)
        attribution = await labeling.resolve(session, labels)

        # Read the prior state before upserting so we can tell a genuine
        # state change from a plain repeat notification.
        previous_status = (
            await session.execute(
                select(Alert.status).where(
                    Alert.fingerprint == am_alert.fingerprint,
                    Alert.starts_at == am_alert.startsAt,
                )
            )
        ).scalar_one_or_none()

        stmt = pg_insert(Alert).values(
            fingerprint=am_alert.fingerprint,
            starts_at=am_alert.startsAt,
            ends_at=am_alert.endsAt,
            status=AlertStatus(am_alert.status),
            alertname=labels.get("alertname"),
            severity=labels.get("severity"),
            summary=annotations.get("summary"),
            description=annotations.get("description"),
            labels=labels,
            annotations=annotations,
            generator_url=am_alert.generatorURL,
            external_url=payload.externalURL,
            group_key=payload.groupKey,
            receiver=payload.receiver,
            service_id=attribution.service_id,
            team_id=attribution.team_id,
            label_source=attribution.source,
        )

        # A manual tag is an operator decision and must survive the next
        # repeat notification; anything auto-resolved is refreshed, so adding
        # a label_rule retro-labels alerts still being re-sent.
        keep_manual = Alert.label_source == LabelSource.manual

        stmt = stmt.on_conflict_do_update(
            constraint="uq_alert_fingerprint_start",
            set_={
                "status": stmt.excluded.status,
                "ends_at": stmt.excluded.ends_at,
                "labels": stmt.excluded.labels,
                "annotations": stmt.excluded.annotations,
                "summary": stmt.excluded.summary,
                "description": stmt.excluded.description,
                "service_id": case(
                    (keep_manual, Alert.service_id), else_=stmt.excluded.service_id
                ),
                "team_id": case(
                    (keep_manual, Alert.team_id), else_=stmt.excluded.team_id
                ),
                "label_source": case(
                    (keep_manual, Alert.label_source), else_=stmt.excluded.label_source
                ),
                "updated_at": func.now(),
            },
        ).returning(Alert.id)

        alert_id = (await session.execute(stmt)).scalar_one()
        alert_ids.append(alert_id)

        new_status = AlertStatus(am_alert.status)
        if previous_status is None or previous_status != new_status:
            notify_ids.append(alert_id)

    await session.commit()
    log.info(
        "ingested %d alert(s) from receiver=%s (%d worth notifying)",
        len(alert_ids), payload.receiver, len(notify_ids),
    )
    return IngestResult(alert_ids=alert_ids, notify_ids=notify_ids)
