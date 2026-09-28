"""US-L4: alert routing (parent vs developer audiences) and acknowledgement.

Routing is authorization, not preference: the Parent role sees parent-audience
alerts only (device/health honesty banners); the Coach role acts as the
household's developer seat and sees developer-audience alerts (pipeline/model
drift, canary). The Player role sees none. Acknowledgement flips the per-row
flag and writes an AuditLog entry (who, which alert, when).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from cricai_data.enums import AlertAudience, Role
from cricai_data.models import Alert, AuditLog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/alerts", tags=["alerts"])

#: Which alert audiences each role may see (US-L4 routing AC). The Coach role
#: holds the developer seat; the Parent role gets honesty/device alerts only.
VISIBLE_AUDIENCES: dict[Role, frozenset[AlertAudience]] = {
    Role.PARENT: frozenset({AlertAudience.PARENT}),
    Role.COACH: frozenset({AlertAudience.DEVELOPER}),
}


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    audience: AlertAudience
    code: str
    severity: str
    detail: dict[str, Any]
    session_id: uuid.UUID | None
    acknowledged: bool
    created_at: datetime


def _visible(role: Role) -> frozenset[AlertAudience]:
    return VISIBLE_AUDIENCES[role]


@router.get("")
def list_alerts(
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
    audience: AlertAudience | None = None,
    acknowledged: bool | None = None,
) -> list[AlertOut]:
    """List alerts routed to the caller, newest first (US-L4).

    ``audience`` narrows within the caller's visibility and is rejected (403)
    outside it — routing is enforced, not advisory. ``acknowledged`` filters
    the per-row flag (``false`` = the open inbox).
    """
    visible = _visible(role)
    if audience is not None and audience not in visible:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{role.value} cannot view {audience.value}")
    audiences = {audience} if audience is not None else visible
    query = select(Alert).where(Alert.audience.in_(audiences))
    if acknowledged is not None:
        query = query.where(Alert.acknowledged.is_(acknowledged))
    rows = db.scalars(query.order_by(Alert.created_at.desc(), Alert.id)).all()
    return [AlertOut.model_validate(row) for row in rows]


@router.post("/{alert_id}/ack")
def acknowledge_alert(
    alert_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> AlertOut:
    """Acknowledge one alert (US-L4): idempotent flag flip plus an audit row.

    Only a role that can see the alert's audience may acknowledge it; the
    AuditLog row is written once, on the actual state change.
    """
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found")
    if alert.audience not in _visible(role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"{role.value} cannot acknowledge {alert.audience.value}"
        )
    if not alert.acknowledged:
        alert.acknowledged = True
        db.add(
            AuditLog(
                actor=role.value,
                action="alert_ack",
                entity="alert",
                entity_id=str(alert.id),
                detail={"code": alert.code, "audience": alert.audience.value},
            )
        )
    db.flush()
    return AlertOut.model_validate(alert)
