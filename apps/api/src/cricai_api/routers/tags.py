"""US-B4: manual ball tagging — V1 value now, training ground truth forever.

Every tag is written in the canonical per-ball schema (US-G1) with
``source="manual"`` and ``ground_truth_eligible=True``; edits keep a full
audit history (who, when, old→new); export/import round-trips losslessly.
"""

import csv
import hashlib
import io
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from cricai_data.enums import Contact, Footwork, Length, Line, Outcome, Role, Shot
from cricai_data.models import AuditLog, BallTag, Session, SessionBlock, TagAudit
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/sessions/{session_id}/tags", tags=["tags"])

MANUAL_SOURCE = "manual"

#: Canonical per-ball export schema (US-G1 contract). Names and order are
#: pinned by a schema-compatibility test — changing them is a breaking change.
EXPORT_FIELDS: tuple[str, ...] = (
    "ball_no",
    "block_no",
    "line",
    "length",
    "shot",
    "footwork",
    "contact",
    "outcome",
    "control",
    "source",
    "ground_truth_eligible",
)

#: Fields editable after creation; every actual change writes a TagAudit row.
EDITABLE_FIELDS: tuple[str, ...] = (
    "line",
    "length",
    "shot",
    "footwork",
    "contact",
    "outcome",
    "control",
)


class TagIn(BaseModel):
    ball_no: int = Field(ge=1)
    block_no: int | None = None
    line: Line
    length: Length
    shot: Shot
    footwork: Footwork
    contact: Contact
    outcome: Outcome
    control: bool


class TagImportItem(TagIn):
    """One row of the canonical JSON export; provenance is forced to manual."""

    source: str = MANUAL_SOURCE
    ground_truth_eligible: bool = True


class TagPatch(BaseModel):
    line: Line | None = None
    length: Length | None = None
    shot: Shot | None = None
    footwork: Footwork | None = None
    contact: Contact | None = None
    outcome: Outcome | None = None
    control: bool | None = None


class TagAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    actor: str
    field: str
    old_value: str | None
    new_value: str | None
    at: datetime


class TagOut(BaseModel):
    ball_no: int
    block_no: int | None
    line: Line
    length: Length
    shot: Shot
    footwork: Footwork
    contact: Contact
    outcome: Outcome
    control: bool
    source: str
    ground_truth_eligible: bool
    created_by: str
    audits: list[TagAuditOut]


class ImportResult(BaseModel):
    created: int
    updated: int
    audits_written: int


def _get_session(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players,
        # the same 404-hiding rule the sibling readers (events/clips/metrics) apply.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _resolve_block(db: OrmSession, session_id: uuid.UUID, block_no: int | None) -> uuid.UUID | None:
    if block_no is None:
        return None
    block = db.scalar(
        select(SessionBlock).where(
            SessionBlock.session_id == session_id, SessionBlock.block_no == block_no
        )
    )
    if block is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"block {block_no} not found in session")
    return block.id


def _get_tag(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> BallTag | None:
    return db.scalar(
        select(BallTag).where(BallTag.session_id == session_id, BallTag.ball_no == ball_no)
    )


def _tag_block_no(db: OrmSession, tag: BallTag) -> int | None:
    if tag.block_id is None:
        return None
    return db.scalar(select(SessionBlock.block_no).where(SessionBlock.id == tag.block_id))


def _tag_out(db: OrmSession, tag: BallTag) -> TagOut:
    return TagOut(
        ball_no=tag.ball_no,
        block_no=_tag_block_no(db, tag),
        line=tag.line,
        length=tag.length,
        shot=tag.shot,
        footwork=tag.footwork,
        contact=tag.contact,
        outcome=tag.outcome,
        control=tag.control,
        source=tag.source,
        ground_truth_eligible=tag.ground_truth_eligible,
        created_by=tag.created_by,
        audits=[TagAuditOut.model_validate(a) for a in tag.audits],
    )


def _audit_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)  # StrEnum → canonical value string


def _opt_str(value: int | None) -> str | None:
    return None if value is None else str(value)


def _apply_changes(tag: BallTag, changes: dict[str, Any], actor: str) -> int:
    """Set changed fields; write one audit row (who, old→new) per actual change."""
    written = 0
    for field, new_value in changes.items():
        old_value = getattr(tag, field)
        if old_value == new_value:
            continue
        tag.audits.append(
            TagAudit(
                actor=actor,
                field=field,
                old_value=_audit_value(old_value),
                new_value=_audit_value(new_value),
            )
        )
        setattr(tag, field, new_value)
        written += 1
    return written


def _patch_changes(payload: TagPatch) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for name in EDITABLE_FIELDS:
        if name not in payload.model_fields_set:
            continue
        value = getattr(payload, name)
        if value is None:  # explicit null: field is not nullable, ignore
            continue
        changes[name] = value
    return changes


def _new_tag(session_id: uuid.UUID, item: TagIn, block_id: uuid.UUID | None, actor: str) -> BallTag:
    return BallTag(
        session_id=session_id,
        ball_no=item.ball_no,
        block_id=block_id,
        line=item.line,
        length=item.length,
        shot=item.shot,
        footwork=item.footwork,
        contact=item.contact,
        outcome=item.outcome,
        control=item.control,
        source=MANUAL_SOURCE,
        ground_truth_eligible=True,
        created_by=actor,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_tag(
    session_id: uuid.UUID,
    payload: TagIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> TagOut:
    _get_session(db, session_id, role)
    block_id = _resolve_block(db, session_id, payload.block_no)
    if _get_tag(db, session_id, payload.ball_no) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ball {payload.ball_no} already tagged; use PATCH to edit",
        )
    tag = _new_tag(session_id, payload, block_id, actor=role.value)
    db.add(tag)
    db.flush()
    return _tag_out(db, tag)


@router.patch("/{ball_no}")
def edit_tag(
    session_id: uuid.UUID,
    ball_no: int,
    payload: TagPatch,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> TagOut:
    _get_session(db, session_id, role)
    tag = _get_tag(db, session_id, ball_no)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tag not found")
    _apply_changes(tag, _patch_changes(payload), actor=role.value)
    db.flush()
    return _tag_out(db, tag)


@router.get("")
def list_tags(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> list[TagOut]:
    _get_session(db, session_id, role)
    tags = db.scalars(
        select(BallTag).where(BallTag.session_id == session_id).order_by(BallTag.ball_no)
    ).all()
    return [_tag_out(db, t) for t in tags]


def _export_rows(db: OrmSession, session_id: uuid.UUID) -> list[dict[str, Any]]:
    blocks = db.scalars(select(SessionBlock).where(SessionBlock.session_id == session_id)).all()
    block_nos = {b.id: b.block_no for b in blocks}
    tags = db.scalars(
        select(BallTag).where(BallTag.session_id == session_id).order_by(BallTag.ball_no)
    ).all()
    return [
        {
            "ball_no": tag.ball_no,
            "block_no": block_nos[tag.block_id] if tag.block_id is not None else None,
            "line": tag.line.value,
            "length": tag.length.value,
            "shot": tag.shot.value,
            "footwork": tag.footwork.value,
            "contact": tag.contact.value,
            "outcome": tag.outcome.value,
            "control": tag.control,
            "source": tag.source,
            "ground_truth_eligible": tag.ground_truth_eligible,
        }
        for tag in tags
    ]


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@router.get("/export")
def export_tags(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
    format: Literal["json", "csv"] = "json",
) -> Response:
    """Export tags for sharing (US-L3): guardian roles only, every call is audit-logged."""
    _get_session(db, session_id, role)
    rows = _export_rows(db, session_id)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_FIELDS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})
        response: Response = PlainTextResponse(buffer.getvalue(), media_type="text/csv")
    else:
        response = JSONResponse(rows)
    db.add(
        AuditLog(
            actor=role.value,
            action="share_export",
            entity="session",
            entity_id=str(session_id),
            detail={
                "format": format,
                "sha256": hashlib.sha256(response.body).hexdigest(),
                "surface": "tags_export",
            },
        )
    )
    return response


@router.post("/import")
def import_tags(
    session_id: uuid.UUID,
    payload: list[TagImportItem],
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> ImportResult:
    """Upsert the canonical JSON export; provenance is forced back to manual."""
    _get_session(db, session_id, role)
    created = updated = audits_written = 0
    for item in payload:
        block_id = _resolve_block(db, session_id, item.block_no)
        tag = _get_tag(db, session_id, item.ball_no)
        if tag is None:
            db.add(_new_tag(session_id, item, block_id, actor=role.value))
            created += 1
            continue
        changes = {name: getattr(item, name) for name in EDITABLE_FIELDS}
        n_changes = _apply_changes(tag, changes, actor=role.value)
        old_block_no = _tag_block_no(db, tag)
        if old_block_no != item.block_no:
            tag.audits.append(
                TagAudit(
                    actor=role.value,
                    field="block_no",
                    old_value=_opt_str(old_block_no),
                    new_value=_opt_str(item.block_no),
                )
            )
            tag.block_id = block_id
            n_changes += 1
        tag.source = MANUAL_SOURCE
        tag.ground_truth_eligible = True
        if n_changes:
            updated += 1
        audits_written += n_changes
    db.flush()
    return ImportResult(created=created, updated=updated, audits_written=audits_written)
