"""US-B3: session blocks — per-block bowler source, machine settings and intent.

Blocks partition the session timeline. Invariants:

- No two blocks may overlap; touching boundaries are allowed
  (``new.start_s == prev.end_s``). An open block (``end_s is None``) occupies
  ``[start_s, inf)`` until it is closed, so at most one block is open at a time
  and it is always the latest block on the timeline.
- Starting a new block while one is open requires ``auto_close_open: true`` in
  the payload (the open block is closed at the new block's ``start_s``);
  without the flag the request is rejected with 409 for explicit control.
- A machine block requires the session's safety checklist to be acknowledged
  (US-A5) — even inside coach/human sessions, machine balls never bypass it.
- Gaps between blocks are allowed and reported by the list endpoint,
  including the leading gap ``[0, first.start_s)`` when the first block
  starts after 0.
- Ball->block assignment is by timestamp over half-open ``[start_s, end_s)``
  spans: a ball exactly on a boundary belongs to the LATER block (see
  :mod:`cricai_api.services.block_assign`).
"""

import math
import uuid
from collections.abc import Sequence
from itertools import pairwise
from typing import Annotated, Any, NoReturn, Self

from cricai_data.enums import BlockIntent, BowlerSource, Role
from cricai_data.models import Session, SessionBlock
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.routers.sessions import MachineSettings
from cricai_api.services.block_assign import assign_block

router = APIRouter(prefix="/sessions/{session_id}/blocks", tags=["blocks"])


class BlockIn(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float | None = None  # None = open block
    bowler_source: BowlerSource
    machine_settings: MachineSettings | None = None
    intent: BlockIntent
    auto_close_open: bool = False

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end_s is not None and self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self


class BlockPatch(BaseModel):
    """Close a block (set ``end_s``) or edit ``machine_settings`` / ``intent``."""

    end_s: float | None = None
    machine_settings: MachineSettings | None = None
    intent: BlockIntent | None = None


class BlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    block_no: int
    start_s: float
    end_s: float | None
    bowler_source: BowlerSource
    machine_settings: dict[str, Any] | None
    intent: BlockIntent


class Gap(BaseModel):
    """Uncovered interval ``[from_s, to_s)``.

    ``after_block_no`` is the block the gap follows, or ``None`` for the
    leading gap before the first block on the timeline.
    """

    after_block_no: int | None
    from_s: float
    to_s: float


class BlockList(BaseModel):
    blocks: list[BlockOut]
    gaps: list[Gap]


class AssignOut(BaseModel):
    block_no: int | None


def _get_session_or_404(db: OrmSession, session_id: uuid.UUID) -> Session:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _blocks_for(db: OrmSession, session_id: uuid.UUID) -> Sequence[SessionBlock]:
    return db.scalars(
        select(SessionBlock)
        .where(SessionBlock.session_id == session_id)
        .order_by(SessionBlock.block_no)
    ).all()


def _raise_overlap(block_no: int) -> NoReturn:
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {"message": "block overlaps an existing block", "overlapping_block_no": block_no},
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_block(
    session_id: uuid.UUID,
    payload: BlockIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> BlockOut:
    session = _get_session_or_404(db, session_id)
    # US-A5: machine balls never bypass the safety checklist, whatever the
    # session-level bowler_source says (mixed/coach sessions included).
    if payload.bowler_source is BowlerSource.MACHINE and session.checklist_ack_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "safety checklist not acknowledged")
    blocks = _blocks_for(db, session_id)

    open_block: SessionBlock | None = None
    for existing in blocks:
        if existing.end_s is None:
            open_block = existing

    # Auto-close only helps when the new block starts after the open block does;
    # otherwise the overlap check below rejects the request.
    to_close: SessionBlock | None = None
    if open_block is not None and payload.auto_close_open and payload.start_s > open_block.start_s:
        to_close = open_block

    new_end = math.inf if payload.end_s is None else payload.end_s
    for existing in blocks:
        if existing is to_close:
            continue  # would be truncated to [start_s, new.start_s): cannot overlap
        existing_end = math.inf if existing.end_s is None else existing.end_s
        if payload.start_s < existing_end and existing.start_s < new_end:
            _raise_overlap(existing.block_no)

    if to_close is not None:
        to_close.end_s = payload.start_s

    block = SessionBlock(
        session_id=session_id,
        block_no=blocks[-1].block_no + 1 if blocks else 1,
        start_s=payload.start_s,
        end_s=payload.end_s,
        bowler_source=payload.bowler_source,
        machine_settings=(
            payload.machine_settings.model_dump() if payload.machine_settings else None
        ),
        intent=payload.intent,
    )
    db.add(block)
    db.flush()
    return BlockOut.model_validate(block)


@router.get("")
def list_blocks(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> BlockList:
    """List blocks in timeline order plus the uncovered gaps between them."""
    _get_session_or_404(db, session_id)
    blocks = sorted(_blocks_for(db, session_id), key=lambda b: b.start_s)
    gaps: list[Gap] = []
    if blocks and blocks[0].start_s > 0:  # leading gap: timeline starts uncovered
        gaps.append(Gap(after_block_no=None, from_s=0.0, to_s=blocks[0].start_s))
    gaps.extend(
        Gap(after_block_no=prev.block_no, from_s=prev.end_s, to_s=nxt.start_s)
        for prev, nxt in pairwise(blocks)
        if prev.end_s is not None and nxt.start_s > prev.end_s
    )
    return BlockList(blocks=[BlockOut.model_validate(b) for b in blocks], gaps=gaps)


@router.get("/assign")
def assign_ball(
    session_id: uuid.UUID,
    t_s: Annotated[float, Query(ge=0)],
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> AssignOut:
    """Resolve which block a ball at ``t_s`` belongs to (Epic D events pipeline)."""
    _get_session_or_404(db, session_id)
    spans = [(b.block_no, b.start_s, b.end_s) for b in _blocks_for(db, session_id)]
    return AssignOut(block_no=assign_block(spans, t_s))


@router.patch("/{block_no}")
def update_block(
    session_id: uuid.UUID,
    block_no: int,
    payload: BlockPatch,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> BlockOut:
    _get_session_or_404(db, session_id)
    block = db.scalar(
        select(SessionBlock).where(
            SessionBlock.session_id == session_id, SessionBlock.block_no == block_no
        )
    )
    if block is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "block not found")

    provided = payload.model_fields_set
    if "end_s" in provided:
        _apply_end_s(db, block, payload.end_s)
    if "machine_settings" in provided:
        block.machine_settings = (
            payload.machine_settings.model_dump() if payload.machine_settings else None
        )
    if "intent" in provided and payload.intent is not None:
        block.intent = payload.intent
    db.flush()
    return BlockOut.model_validate(block)


def _apply_end_s(db: OrmSession, block: SessionBlock, end_s: float | None) -> None:
    if end_s is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "end_s cannot be null: a block cannot be reopened",
        )
    if end_s <= block.start_s:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "end_s must be greater than start_s",
        )
    others = db.scalars(
        select(SessionBlock).where(
            SessionBlock.session_id == block.session_id, SessionBlock.id != block.id
        )
    ).all()
    for other in others:
        other_end = math.inf if other.end_s is None else other.end_s
        if block.start_s < other_end and other.start_s < end_s:
            _raise_overlap(other.block_no)
    block.end_s = end_s
