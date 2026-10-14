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

from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, AlertStatus, LabelSource, Service, Team
from app.schemas.alertmanager import AlertmanagerPayload
from app.services import audit, labeling, severity_rules

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


async def _effective_severity(session, am_alert, labels, attribution, rules):
    """What severity_rules make of this alert, or None.

    Returns None without touching the database when there are no rules --
    which is the common case and the one the invariant depends on.
    """
    if not rules:
        return None
    try:
        service = (
            await session.get(Service, attribution.service_id)
            if attribution.service_id else None
        )
        team = (
            await session.get(Team, attribution.team_id)
            if attribution.team_id else None
        )
        # A detached stand-in: the row does not exist yet, and the rules only
        # read labels, severity and status.
        probe = Alert(
            fingerprint=am_alert.fingerprint,
            starts_at=am_alert.startsAt,
            status=AlertStatus(am_alert.status),
            severity=labels.get("severity"),
            labels=labels,
        )
        result, _rule = severity_rules.evaluate_against(
            probe, rules, service=service, team=team
        )
        return result
    except Exception:  # noqa: BLE001 - severity is an enhancement; intake is not
        log.exception("severity evaluation failed during intake")
        return None


async def _record_attribution_change(
    session: AsyncSession, previous, alert_id: int, attribution
) -> None:
    """Write an audit row when intake moves an alert's label by itself.

    Two cases, both of which look like a bug to whoever notices them later:

    * a manual tag handed back to the payload, now that the payload resolves;
    * an alert losing its label because the rule that claimed it was deleted
      or disabled, which drops the attributed count with nothing to explain it.

    Never raises: an audit row is a nice-to-have and intake is the path that
    must not fail.
    """
    if previous is None:
        return
    before, after = previous.label_source, attribution.source
    if before == after and previous.service_id == attribution.service_id:
        return

    action = None
    if before is LabelSource.manual and after is LabelSource.payload:
        action = "alert.label.superseded"
    elif before is LabelSource.rule and after is None:
        action = "alert.label.lost"
    if action is None:
        return

    try:
        await audit.record(
            session, None, action,
            object_type="alert", object_id=alert_id,
            detail={
                "from": before.value if before else None,
                "to": after.value if after else None,
                "service_id_from": previous.service_id,
                "service_id_to": attribution.service_id,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("could not record attribution change for alert %s", alert_id)


async def ingest_payload(
    session: AsyncSession, payload: AlertmanagerPayload
) -> IngestResult:
    """Persist every alert in the payload."""
    alert_ids: list[int] = []
    notify_ids: list[int] = []

    # Loaded once for the whole payload, and not at all when nobody has
    # written any. An install with no severity rules must not pay a query
    # per alert for a feature it does not use.
    sev_rules = await severity_rules.active_rules(session)

    for am_alert in payload.alerts:
        labels = payload.effective_labels(am_alert)
        annotations = payload.effective_annotations(am_alert)
        attribution = await labeling.resolve(session, labels)

        # Read the prior state before upserting. The status tells a genuine
        # state change from a plain repeat; the attribution is needed to
        # notice when intake is about to hand a manual tag back to the
        # payload. One query serves both -- the second value would otherwise
        # cost a round trip per alert on the intake hot path.
        previous = (
            await session.execute(
                select(
                    Alert.id,
                    Alert.status,
                    Alert.label_source,
                    Alert.service_id,
                    Alert.team_id,
                ).where(
                    Alert.fingerprint == am_alert.fingerprint,
                    Alert.starts_at == am_alert.startsAt,
                )
            )
        ).one_or_none()
        previous_status = previous.status if previous else None

        effective = await _effective_severity(session, am_alert, labels,
                                              attribution, sev_rules)

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
            effective_severity=effective,
        )

        # A manual tag is a patch applied while the source could not say who
        # owns this alert. Once the alert's OWN labels resolve to a service,
        # the source takes over: Alertmanager is the system of record, and a
        # hand-tag that outlives the thing it was standing in for turns into
        # a quiet lie about attribution.
        #
        # A `rule` match does NOT take over. A rule is OpsTrail guessing too,
        # and a guess must never overwrite a person's decision.
        #
        # `is_distinct_from` rather than `!=`: when nothing resolved,
        # excluded.label_source is NULL and `NULL != 'payload'` is NULL, not
        # TRUE -- using `!=` would silently drop the manual tag every time an
        # alert stopped matching anything.
        keep_manual = and_(
            Alert.label_source == LabelSource.manual,
            stmt.excluded.label_source.is_distinct_from(LabelSource.payload),
        )

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
                # Recomputed on every repeat, like the other derived fields:
                # a rule added since the alert arrived should take hold, and
                # one deleted should let go.
                "effective_severity": stmt.excluded.effective_severity,
                "updated_at": func.now(),
            },
        ).returning(Alert.id)

        alert_id = (await session.execute(stmt)).scalar_one()
        alert_ids.append(alert_id)

        await _record_attribution_change(session, previous, alert_id, attribution)

        new_status = AlertStatus(am_alert.status)
        if previous_status is None or previous_status != new_status:
            notify_ids.append(alert_id)

    await session.commit()
    log.info(
        "ingested %d alert(s) from receiver=%s (%d worth notifying)",
        len(alert_ids), payload.receiver, len(notify_ids),
    )
    return IngestResult(alert_ids=alert_ids, notify_ids=notify_ids)
