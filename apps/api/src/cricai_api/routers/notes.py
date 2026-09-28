"""US-K3: coach notes — timestamped free-text pinned to player/session/ball.

``body`` is UNTRUSTED human input (US-G4 prompt-injection hardening): it is
stored verbatim, served only to human UIs (React escapes it at render), and
must NEVER appear in an LLM-reachable payload — the egress strict-PII set
blocks the ``body`` key at any depth, and the audit trail carries only its
SHA-256, never the text. Guardian roles (parent/coach) create and delete
notes with a full audit trail (US-L3); the player role (kid mode) sees
``shared`` notes only — ``coach_only`` stays the adults' working channel.
"""

import hashlib
import uuid
from datetime import datetime
from typing import Annotated, Literal

from cricai_data.enums import Role
from cricai_data.models import AuditLog, CoachNote, Player, Session
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/notes", tags=["notes"])

#: Note visibility vocabulary (US-K3 SAF): ``coach_only`` notes never reach
#: the player role; ``shared`` notes are written for the player to read.
Visibility = Literal["coach_only", "shared"]

#: LIKE/ILIKE escape character for the search filter, so a query containing
#: ``%`` or ``_`` matches those characters literally instead of as wildcards.
_LIKE_ESCAPE = "\\"


class NoteIn(BaseModel):
    player_id: uuid.UUID
    session_id: uuid.UUID | None = None
    ball_no: int | None = Field(default=None, ge=1)
    body: str = Field(min_length=1, max_length=4000)
    visibility: Visibility = "coach_only"

    @model_validator(mode="after")
    def _ball_needs_session(self) -> "NoteIn":
        """A ball-pinned note must name its session (ball_no is per-session)."""
        if self.ball_no is not None and self.session_id is None:
            raise ValueError("ball_no requires session_id")
        return self


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    session_id: uuid.UUID | None
    ball_no: int | None
    body: str
    author: str
    visibility: str
    created_at: datetime


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _audit(role: Role, action: str, note: CoachNote) -> AuditLog:
    """Audit row for a note mutation (US-L3). The detail carries the body's
    SHA-256 only: audit logs are queryable surfaces, so echoing the untrusted
    free text there would leak it beyond the human notes UI."""
    return AuditLog(
        actor=role.value,
        action=action,
        entity="coach_note",
        entity_id=str(note.id),
        detail={
            "player_id": str(note.player_id),
            "session_id": str(note.session_id) if note.session_id is not None else None,
            "ball_no": note.ball_no,
            "visibility": note.visibility,
            "body_sha256": _body_sha256(note.body),
        },
    )


def _validate_refs(db: OrmSession, payload: NoteIn) -> None:
    if db.get(Player, payload.player_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    if payload.session_id is None:
        return
    session = db.get(Session, payload.session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    if session.player_id != payload.player_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "session belongs to a different player")


@router.post("", status_code=status.HTTP_201_CREATED)
def create_note(
    payload: NoteIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> NoteOut:
    """Create a note pinned to a player, session, or ball (US-K3).

    Guardian roles only; the author is the acting role. Every create writes
    an audit row (US-L3) that references the body by hash, never by text.
    """
    _validate_refs(db, payload)
    note = CoachNote(
        player_id=payload.player_id,
        session_id=payload.session_id,
        ball_no=payload.ball_no,
        body=payload.body,
        author=role.value,
        visibility=payload.visibility,
    )
    db.add(note)
    db.flush()
    db.add(_audit(role, "note_created", note))
    return NoteOut.model_validate(note)


def _escaped_pattern(q: str) -> str:
    escaped = (
        q.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


@router.get("")
def list_notes(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    session_id: uuid.UUID | None = None,
    ball_no: int | None = None,
    q: str | None = None,
) -> list[NoteOut]:
    """List a player's notes, oldest first, filterable by session, ball and a
    case-insensitive body search (US-K3 "searchable"). Kid mode (SAF): the
    player role gets ``shared`` notes only — ``coach_only`` never ships."""
    query = select(CoachNote).where(CoachNote.player_id == player_id)
    if session_id is not None:
        query = query.where(CoachNote.session_id == session_id)
    if ball_no is not None:
        query = query.where(CoachNote.ball_no == ball_no)
    if q is not None:
        query = query.where(CoachNote.body.ilike(_escaped_pattern(q), escape=_LIKE_ESCAPE))
    if role is Role.PLAYER:
        query = query.where(CoachNote.visibility == "shared")
    query = query.order_by(CoachNote.created_at, CoachNote.id)
    return [NoteOut.model_validate(row) for row in db.scalars(query)]


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    note_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> None:
    """Delete a note (guardian roles); the deletion is audit-logged (US-L3)."""
    note = db.get(CoachNote, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    db.add(_audit(role, "note_deleted", note))
    db.delete(note)
    db.flush()
