"""US-I4: declared bowling targets — line/length intent per block for scoring.

Built with the US-I1 capture story: guardian roles (parent/coach) declare and
delete targets; every mutation writes an :class:`~cricai_data.models.AuditLog`
row. A target is the coach's stated intent for a session or one of its blocks
("good length, off-stump line"); story i3 scores deliveries against it — this
router never computes accuracy itself.

Session ids arrive in the body/query (the ``/targets`` prefix is pinned by the
Phase-6 surface), so an unknown session is a 422 like the health-checks
router; an unknown ``block_no`` inside a valid session is a 404, mirroring the
tags router.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from cricai_data.enums import Length, Line, Role
from cricai_data.models import AuditLog, BowlingTarget, Session, SessionBlock
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/targets", tags=["targets"])

#: Registry mutations are guardian-only (US-L3): players read, never declare.
WRITE_ROLES = (Role.PARENT, Role.COACH)
READ_ROLES = (Role.PARENT, Role.COACH, Role.PLAYER)


class TargetIn(BaseModel):
    session_id: uuid.UUID
    block_no: int | None = None  # None = session-wide target
    line: Line
    length: Length
    description: str = Field(min_length=1, max_length=500)


class TargetOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    block_no: int | None
    line: Line
    length: Length
    description: str
    created_by: str
    created_at: datetime


def _get_session_or_422(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only (finding 56). Players get this
        # router's own pinned unknown-session 422, so a guest session is
        # indistinguishable from one that does not exist — the same
        # hide-existence discipline the sibling readers express as 404.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown session_id")
    return session


def _resolve_block(db: OrmSession, session_id: uuid.UUID, block_no: int) -> uuid.UUID:
    block = db.scalar(
        select(SessionBlock).where(
            SessionBlock.session_id == session_id, SessionBlock.block_no == block_no
        )
    )
    if block is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"block {block_no} not found in session")
    return block.id


def _block_no(db: OrmSession, target: BowlingTarget) -> int | None:
    if target.block_id is None:
        return None
    return db.scalar(select(SessionBlock.block_no).where(SessionBlock.id == target.block_id))


def _to_out(db: OrmSession, target: BowlingTarget) -> TargetOut:
    return TargetOut(
        id=target.id,
        session_id=target.session_id,
        block_no=_block_no(db, target),
        line=target.line,
        length=target.length,
        description=target.description,
        created_by=target.created_by,
        created_at=target.created_at,
    )


def _audit(db: OrmSession, role: Role, action: str, target_id: uuid.UUID, **detail: Any) -> None:
    db.add(
        AuditLog(
            actor=role.value,
            action=action,
            entity="bowling_target",
            entity_id=str(target_id),
            detail=detail,
        )
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def declare_target(
    payload: TargetIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> TargetOut:
    """Declare a line/length target for a session or one of its blocks."""
    _get_session_or_422(db, payload.session_id, role)
    block_id = (
        None
        if payload.block_no is None
        else _resolve_block(db, payload.session_id, payload.block_no)
    )
    target = BowlingTarget(
        session_id=payload.session_id,
        block_id=block_id,
        line=payload.line,
        length=payload.length,
        description=payload.description,
        created_by=role.value,
    )
    db.add(target)
    db.flush()
    _audit(
        db,
        role,
        "target_declare",
        target.id,
        session_id=str(payload.session_id),
        block_no=payload.block_no,
        line=payload.line.value,
        length=payload.length.value,
    )
    return _to_out(db, target)


@router.get("")
def list_targets(
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    session_id: uuid.UUID,
    block_no: int | None = None,
) -> list[TargetOut]:
    """Targets declared for a session, oldest first; ``block_no`` narrows to
    one block's targets (session-wide targets are excluded by the filter).
    Player reads are scoped away from guest sessions (US-L3, finding 56)."""
    _get_session_or_422(db, session_id, role)
    query = (
        select(BowlingTarget)
        .where(BowlingTarget.session_id == session_id)
        .order_by(BowlingTarget.created_at, BowlingTarget.id)
    )
    if block_no is not None:
        query = query.where(BowlingTarget.block_id == _resolve_block(db, session_id, block_no))
    return [_to_out(db, t) for t in db.scalars(query).all()]


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(
    target_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> None:
    """Withdraw a declared target; the deletion is audited like the declare."""
    target = db.get(BowlingTarget, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "target not found")
    _audit(
        db,
        role,
        "target_delete",
        target.id,
        session_id=str(target.session_id),
        block_no=_block_no(db, target),
        line=target.line.value,
        length=target.length.value,
    )
    db.delete(target)
    db.flush()
