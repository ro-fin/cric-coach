"""Reference-ball library (US-E5): coach/parent-marked model examples.

Any ball a guardian role marks becomes a "model example" available for future
before/after comparisons. Marks are validated against the session's known
balls (a ``BallEvent`` or ``BallTag`` must exist for the ball_no - 422
otherwise), duplicates are a 409, and every create/delete writes an
``AuditLog`` row. The library view (``GET /reference-balls``) spans sessions
so a reference marked last month can anchor today's comparison; player-role
access never sees guest players' balls (US-L3).
"""

import uuid
from datetime import datetime
from typing import Annotated

from cricai_data.enums import Role
from cricai_data.models import AuditLog, BallEvent, BallTag, Player, ReferenceBall, Session
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(tags=["reference"])

#: Guardian roles that may curate the library (US-E5: coach OR parent write).
WRITE_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH)

#: All roles may browse it; player access is scoped away from guest data.
READ_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH, Role.PLAYER)


class ReferenceBallIn(BaseModel):
    ball_no: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)


class ReferenceBallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    ball_no: int
    label: str
    marked_by: str
    created_at: datetime


class ReferenceBallLibraryOut(ReferenceBallOut):
    """Library row: the owning player makes cross-session rows attributable."""

    player_id: uuid.UUID


def _session_or_404(db: OrmSession, session_id: uuid.UUID) -> Session:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _ball_exists(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> bool:
    """A ball is known when a non-rejected event or a manual tag references it."""
    event_id = db.scalar(
        select(BallEvent.id).where(
            BallEvent.session_id == session_id,
            BallEvent.ball_no == ball_no,
            BallEvent.valid.is_(True),  # rejected events are not balls (US-D4)
        )
    )
    if event_id is not None:
        return True
    tag_id = db.scalar(
        select(BallTag.id).where(BallTag.session_id == session_id, BallTag.ball_no == ball_no)
    )
    return tag_id is not None


def _get_reference(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> ReferenceBall | None:
    return db.scalar(
        select(ReferenceBall).where(
            ReferenceBall.session_id == session_id, ReferenceBall.ball_no == ball_no
        )
    )


def _audit(db: OrmSession, role: Role, action: str, reference: ReferenceBall) -> None:
    db.add(
        AuditLog(
            actor=role.value,
            action=action,
            entity="reference_ball",
            entity_id=str(reference.id),
            detail={
                "session_id": str(reference.session_id),
                "ball_no": reference.ball_no,
                "label": reference.label,
            },
        )
    )


@router.post("/sessions/{session_id}/reference-balls", status_code=status.HTTP_201_CREATED)
def mark_reference_ball(
    session_id: uuid.UUID,
    payload: ReferenceBallIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> ReferenceBallOut:
    """Mark a ball as a model example (US-E5 AC: coach can mark any ball)."""
    _session_or_404(db, session_id)
    if not _ball_exists(db, session_id, payload.ball_no):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"ball {payload.ball_no} has no ball event or tag in this session",
        )
    if _get_reference(db, session_id, payload.ball_no) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ball {payload.ball_no} is already a reference ball in this session",
        )
    reference = ReferenceBall(
        session_id=session_id,
        ball_no=payload.ball_no,
        label=payload.label,
        marked_by=role.value,
    )
    db.add(reference)
    db.flush()
    _audit(db, role, "reference_ball_marked", reference)
    return ReferenceBallOut.model_validate(reference)


@router.get("/reference-balls")
def list_reference_balls(
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    player_id: uuid.UUID | None = None,
) -> list[ReferenceBallLibraryOut]:
    """Cross-session reference library, optionally filtered to one player.

    Players never see guest players' reference balls (US-L3): a guest
    ``player_id`` filter 404s for them (hiding existence) and unfiltered
    listings exclude guest sessions' rows.
    """
    if player_id is not None:
        player = db.get(Player, player_id)
        if player is None or (role is Role.PLAYER and player.is_guest):
            # US-L3: guest data is parent/coach-only; hide existence from players.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    query = select(ReferenceBall, Session.player_id).join(
        Session, ReferenceBall.session_id == Session.id
    )
    if player_id is not None:
        query = query.where(Session.player_id == player_id)
    if role is Role.PLAYER:
        query = query.join(Player, Session.player_id == Player.id).where(Player.is_guest.is_(False))
    rows = db.execute(
        query.order_by(ReferenceBall.created_at, ReferenceBall.session_id, ReferenceBall.ball_no)
    ).all()
    return [
        ReferenceBallLibraryOut(
            id=reference.id,
            session_id=reference.session_id,
            ball_no=reference.ball_no,
            label=reference.label,
            marked_by=reference.marked_by,
            created_at=reference.created_at,
            player_id=owner_player_id,
        )
        for reference, owner_player_id in rows
    ]


@router.delete(
    "/sessions/{session_id}/reference-balls/{ball_no}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def unmark_reference_ball(
    session_id: uuid.UUID,
    ball_no: int,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> None:
    """Remove a model example; the removal is audited like the marking."""
    _session_or_404(db, session_id)
    reference = _get_reference(db, session_id, ball_no)
    if reference is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reference ball not found")
    _audit(db, role, "reference_ball_unmarked", reference)
    db.delete(reference)
    db.flush()
