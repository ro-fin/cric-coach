"""US-B1: session CRUD — every downstream artifact hangs off one session ID."""

import uuid
from datetime import date
from typing import Annotated, Any

from cricai_data.enums import BowlerSource, Length, Role, SessionType
from cricai_data.lifecycle import SessionState
from cricai_data.models import Player, Session
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/sessions", tags=["sessions"])

MACHINE_SPEED_MIN_KPH = 30.0
MACHINE_SPEED_MAX_KPH = 160.0


class MachineSettings(BaseModel):
    speed_kph: float = Field(ge=MACHINE_SPEED_MIN_KPH, le=MACHINE_SPEED_MAX_KPH)
    length: Length
    variation: str | None = Field(default=None, max_length=64)


class SessionIn(BaseModel):
    player_id: uuid.UUID
    session_date: date = Field(alias="date")
    session_type: SessionType
    bowler_source: BowlerSource
    machine_settings: MachineSettings | None = None
    notes: str | None = Field(default=None, max_length=4000)

    model_config = ConfigDict(populate_by_name=True)


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    session_date: date
    session_type: SessionType
    bowler_source: BowlerSource
    machine_settings: dict[str, Any] | None
    notes: str | None
    state: SessionState
    degraded: bool
    missing_views: list[str]


class SessionPage(BaseModel):
    items: list[SessionOut]
    total: int
    limit: int
    offset: int


@router.post("", status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> SessionOut:
    if db.get(Player, payload.player_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown player_id")
    if payload.bowler_source is BowlerSource.MACHINE and payload.machine_settings is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "machine sessions must declare machine_settings",
        )
    session = Session(
        player_id=payload.player_id,
        session_date=payload.session_date,
        session_type=payload.session_type,
        bowler_source=payload.bowler_source,
        machine_settings=(
            payload.machine_settings.model_dump() if payload.machine_settings else None
        ),
        notes=payload.notes,
    )
    db.add(session)
    db.flush()
    return SessionOut.model_validate(session)


@router.get("")
def list_sessions(
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    player_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SessionPage:
    query = select(Session)
    if role is Role.PLAYER:  # US-L3: guest data is parent/coach-only
        query = query.join(Player).where(Player.is_guest.is_(False))
    if player_id is not None:
        query = query.where(Session.player_id == player_id)
    if date_from is not None:
        query = query.where(Session.session_date >= date_from)
    if date_to is not None:
        query = query.where(Session.session_date <= date_to)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(Session.session_date.desc(), Session.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return SessionPage(
        items=[SessionOut.model_validate(s) for s in items],
        total=total if total is not None else 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{session_id}")
def get_session(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> SessionOut:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return SessionOut.model_validate(session)
