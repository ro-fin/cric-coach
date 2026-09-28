"""US-H4: wellness check-ins, pain flags and adult clearances.

- ``POST /wellness/{player_id}/checkins``: any role (the player completes the
  30-second check-in unaided); fields validated by
  :mod:`cricai_coaching.wellness`; ``created_by`` records the creating role.
- ``GET /wellness/{player_id}/checkins``: list, optional date range.
- ``POST /wellness/{player_id}/checkins/{checkin_id}/clearance``: parent or
  coach ONLY (the US-H4 adult-clearance rule), note required, writes AuditLog.
- ``GET /wellness/{player_id}/state``: the pain state machine's verdict —
  bowling suppression, escalation, and explicit no-check-in visibility.
"""

import hashlib
import uuid
from datetime import date, datetime
from typing import Annotated

from cricai_coaching.wellness import evaluate_wellness, validate_checkin
from cricai_data.enums import Role
from cricai_data.models import AuditLog, PainClearance, Player, Session, WellnessCheckin
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.services.safety_state import active_safety_config

router = APIRouter(prefix="/wellness", tags=["wellness"])


class CheckinIn(BaseModel):
    checkin_date: date
    session_id: uuid.UUID | None = None
    soreness: dict[str, int] = Field(default_factory=dict)
    energy: int | None = None
    sleep_hours: float | None = None
    pain: bool = False
    pain_note: str | None = None


class CheckinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    session_id: uuid.UUID | None
    checkin_date: date
    soreness: dict[str, int]
    energy: int | None
    sleep_hours: float | None
    pain: bool
    pain_note: str | None
    created_by: str
    created_at: datetime


class ClearanceIn(BaseModel):
    note: str = Field(min_length=1)


class ClearanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    checkin_id: uuid.UUID
    cleared_by: str
    role: str
    note: str
    created_at: datetime


class WellnessStateOut(BaseModel):
    """The US-H4 state machine's output; absence of a check-in is explicit."""

    as_of: date
    checked_in: bool
    no_checkin: bool
    last_checkin_date: date | None
    days_since_checkin: int | None
    pain_active: bool
    bowling_suppressed: bool
    open_pain_checkin_ids: list[uuid.UUID]
    escalation: bool
    pain_reports_in_window: int


def _get_player_or_404(db: OrmSession, player_id: uuid.UUID) -> Player:
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    return player


def _player_rows(
    db: OrmSession, player_id: uuid.UUID
) -> tuple[list[WellnessCheckin], list[PainClearance]]:
    checkins = list(
        db.scalars(
            select(WellnessCheckin)
            .where(WellnessCheckin.player_id == player_id)
            .order_by(WellnessCheckin.checkin_date, WellnessCheckin.created_at)
        )
    )
    clearances = list(db.scalars(select(PainClearance).where(PainClearance.player_id == player_id)))
    return checkins, clearances


@router.post("/{player_id}/checkins", status_code=status.HTTP_201_CREATED)
def create_checkin(
    player_id: uuid.UUID,
    payload: CheckinIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> CheckinOut:
    """Create a check-in (US-H4); the creating role is recorded as created_by."""
    _get_player_or_404(db, player_id)
    if payload.session_id is not None:
        session = db.get(Session, payload.session_id)
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
        if session.player_id != player_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "session belongs to a different player")
    problems = validate_checkin(payload.soreness, payload.energy, payload.sleep_hours)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problems)
    checkin = WellnessCheckin(
        player_id=player_id,
        session_id=payload.session_id,
        checkin_date=payload.checkin_date,
        soreness=dict(payload.soreness),
        energy=payload.energy,
        sleep_hours=payload.sleep_hours,
        pain=payload.pain,
        pain_note=payload.pain_note,
        created_by=role.value,
    )
    db.add(checkin)
    db.flush()
    return CheckinOut.model_validate(checkin)


@router.get("/{player_id}/checkins")
def list_checkins(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    start: date | None = None,
    end: date | None = None,
) -> list[CheckinOut]:
    """List a player's check-ins, optionally within [start, end]."""
    _get_player_or_404(db, player_id)
    if start is not None and end is not None and start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start is after end")
    query = (
        select(WellnessCheckin)
        .where(WellnessCheckin.player_id == player_id)
        .order_by(WellnessCheckin.checkin_date, WellnessCheckin.created_at)
    )
    if start is not None:
        query = query.where(WellnessCheckin.checkin_date >= start)
    if end is not None:
        query = query.where(WellnessCheckin.checkin_date <= end)
    return [CheckinOut.model_validate(row) for row in db.scalars(query)]


@router.post("/{player_id}/checkins/{checkin_id}/clearance", status_code=status.HTTP_201_CREATED)
def clear_pain(
    player_id: uuid.UUID,
    checkin_id: uuid.UUID,
    payload: ClearanceIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> ClearanceOut:
    """Adult-only pain clearance (US-H4): note required, decision audit-logged."""
    _get_player_or_404(db, player_id)
    checkin = db.get(WellnessCheckin, checkin_id)
    if checkin is None or checkin.player_id != player_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "check-in not found for player")
    if not checkin.pain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "check-in has no pain flag to clear")
    existing = db.scalar(select(PainClearance).where(PainClearance.checkin_id == checkin_id))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pain flag already cleared")
    clearance = PainClearance(
        player_id=player_id,
        checkin_id=checkin_id,
        cleared_by=role.value,
        role=role.value,
        note=payload.note,
    )
    db.add(clearance)
    db.add(
        AuditLog(
            actor=role.value,
            action="pain_clearance",
            entity="wellness_checkin",
            entity_id=str(checkin_id),
            # The clearance note is health PII (the privacy-delete round-trip
            # classifies it as such); the audit trail keeps only its presence
            # and a sha256, never the verbatim text (US-L3, forward-fix).
            detail={
                "player_id": str(player_id),
                "checkin_date": checkin.checkin_date.isoformat(),
                "note_present": bool(payload.note),
                "note_sha256": hashlib.sha256(payload.note.encode("utf-8")).hexdigest(),
            },
        )
    )
    db.flush()
    return ClearanceOut.model_validate(clearance)


@router.get("/{player_id}/state")
def wellness_state(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    as_of: date | None = None,
) -> WellnessStateOut:
    """Pain suppression + escalation + no-check-in visibility (US-H4/H5 seam).

    Escalation thresholds come from the versioned ``safety_configs`` wellness
    section (the coach-approved config the safety agent uses), not code
    defaults — so a governance-approved change takes effect here too (US-H1)."""
    _get_player_or_404(db, player_id)
    checkins, clearances = _player_rows(db, player_id)
    config = active_safety_config(db)
    state = evaluate_wellness(
        checkins, clearances, as_of=as_of or date.today(), config=config.get("wellness")
    )
    return WellnessStateOut(
        as_of=state.as_of,
        checked_in=state.checked_in,
        no_checkin=state.no_checkin,
        last_checkin_date=state.last_checkin_date,
        days_since_checkin=state.days_since_checkin,
        pain_active=state.pain_active,
        bowling_suppressed=state.bowling_suppressed,
        open_pain_checkin_ids=list(state.open_pain_checkin_ids),
        escalation=state.escalation,
        pain_reports_in_window=state.pain_reports_in_window,
    )
