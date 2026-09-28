"""US-D4: ball-event review & correction API — versioned ground truth for the detector.

Human corrections (accept / adjust / reject / add) are the detector's training
and evaluation feed: every operation writes an :class:`EventCorrection` row
(action, before/after, actor, at) and updates the event's ``source``
provenance, so re-detection (which replaces only ``source == "auto"`` rows
that are still valid) can never resurrect or overwrite a human decision.

Ball-numbering stability (core US-D4 AC)
----------------------------------------
Downstream rows (the canonical ``cricai_data.models.EVENT_DEPENDENT_TABLES``
list, shared with the worker's re-detection guard) join events on
``(session_id, ball_no)``. Accept/adjust/reject therefore NEVER mutate
``ball_no``; a rejected event keeps its number (it only turns
``valid=False``), so no downstream row can ever orphan.

``add`` insertion algorithm (fractional-free):

1. ``target = position + 1`` where ``position`` is the ``after_ball_no``
   (0 inserts before the first ball). ``position`` need not reference an
   existing ball: adding after a number beyond the last ball simply appends.
2. If no event of the session (valid or rejected alike — rejected events keep
   their numbers) occupies ``target``, the new manual event takes it: the gap
   absorbs the insert.
3. Otherwise the contiguous occupied run ``target .. m`` is shifted up by one
   into the first gap above it, highest number first. Renumbering is allowed
   only for events that are ``source == "auto"``, still valid, AND have no
   dependent rows in any of the six downstream tables. A manual/corrected or
   rejected event in the run, or any dependent row, aborts the whole insert
   with 409 explaining the blocker (never silently orphan). Shifted rows are
   untouched detector output, so no correction rows are written for them.

Exports (``GET .../export``) are guardian-only and audit-logged with a sha256
of the payload (US-L3 ``share_export`` pattern); each event carries its
``source`` and ``detector_version`` so training/eval sets trace provenance.
Rejected events are exported too, flagged ``valid: false`` with their reject
correction history: a human-labeled false positive is exactly the negative
training/eval signal US-D4 promises the detector.
"""

import hashlib
import itertools
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from cricai_data.enums import EventSource, Role
from cricai_data.models import (
    EVENT_DEPENDENT_TABLES,
    AuditLog,
    BallEvent,
    EventCorrection,
    Session,
)
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/sessions/{session_id}/events", tags=["events"])

#: Timing fields a human can adjust; ``contact_ms`` is the only nullable one
#: (a left ball has no contact — explicit ``null`` clears it, US-D4).
TIMING_FIELDS: tuple[str, ...] = ("start_ms", "release_ms", "contact_ms", "end_ms")
NON_NULLABLE_TIMING_FIELDS: tuple[str, ...] = ("start_ms", "release_ms", "end_ms")

#: ``BallEvent`` timing/number columns are 32-bit ``Integer`` on PostgreSQL;
#: bounding the wire schema turns an out-of-range int into a 422 instead of a
#: backend ``DataError`` 500 (same class of guard as the ball-metrics finiteness
#: check). SQLite (tests) accepts arbitrary ints, so only this bound gates it.
INT32_MAX = 2**31 - 1


class AdjustIn(BaseModel):
    """Partial timing update; omitted fields keep their stored values."""

    start_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)
    release_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)
    contact_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)
    end_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)


class EventAddIn(BaseModel):
    start_ms: int = Field(ge=0, le=INT32_MAX)
    release_ms: int = Field(ge=0, le=INT32_MAX)
    contact_ms: int | None = Field(default=None, ge=0, le=INT32_MAX)
    end_ms: int = Field(ge=0, le=INT32_MAX)
    # after_ball_no; 0 inserts before the first ball. The new event takes
    # ``position + 1``, which must itself fit the int32 ``ball_no`` column.
    position: int = Field(ge=0, le=INT32_MAX - 1)


class BulkAcceptIn(BaseModel):
    min_confidence: float = Field(ge=0.0, le=1.0)


class BulkAcceptOut(BaseModel):
    accepted: int


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    ball_no: int
    start_ms: int
    release_ms: int
    contact_ms: int | None
    end_ms: int
    confidence: float
    source: EventSource
    detector_version: str
    valid: bool
    created_at: datetime


def _get_session(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _event_or_404(db: OrmSession, session_id: uuid.UUID, event_id: uuid.UUID) -> BallEvent:
    event = db.get(BallEvent, event_id)
    if event is None or event.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found in session")
    return event


def _require_correctable(event: BallEvent) -> None:
    if not event.valid:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ball {event.ball_no} is rejected; rejected events cannot be corrected",
        )


def _snapshot(event: BallEvent) -> dict[str, Any]:
    return {
        "ball_no": event.ball_no,
        "start_ms": event.start_ms,
        "release_ms": event.release_ms,
        "contact_ms": event.contact_ms,
        "end_ms": event.end_ms,
        "confidence": event.confidence,
        "source": event.source.value,
        "detector_version": event.detector_version,
        "valid": event.valid,
    }


def _validate_ordering(start_ms: int, release_ms: int, contact_ms: int | None, end_ms: int) -> None:
    """Enforce start <= release <= (contact when present) <= end, else 422."""
    checkpoints: list[tuple[str, int]] = [("start_ms", start_ms), ("release_ms", release_ms)]
    if contact_ms is not None:
        checkpoints.append(("contact_ms", contact_ms))
    checkpoints.append(("end_ms", end_ms))
    for (earlier_name, earlier), (later_name, later) in itertools.pairwise(checkpoints):
        if earlier > later:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"invalid ordering: {earlier_name} ({earlier}) > {later_name} ({later})",
            )


def _correct(
    db: OrmSession,
    event: BallEvent,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: str,
) -> None:
    db.add(
        EventCorrection(event_id=event.id, action=action, before=before, after=after, actor=actor)
    )


@router.get("")
def list_events(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    source: EventSource | None = None,
    valid: bool | None = None,
    min_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
) -> list[EventOut]:
    """List the session timeline; rejected events appear only via explicit ``valid=false``."""
    _get_session(db, session_id, role)
    query = select(BallEvent).where(
        BallEvent.session_id == session_id,
        # Default view is the live timeline: rejected events only on request.
        BallEvent.valid.is_(True if valid is None else valid),
    )
    if source is not None:
        query = query.where(BallEvent.source == source)
    if min_confidence is not None:
        query = query.where(BallEvent.confidence >= min_confidence)
    events = db.scalars(query.order_by(BallEvent.ball_no)).all()
    return [EventOut.model_validate(event) for event in events]


def _accept_event(db: OrmSession, event: BallEvent, actor: str) -> None:
    before = _snapshot(event)
    event.source = EventSource.CORRECTED  # accepted auto output is now ground truth
    _correct(db, event, "accept", before, _snapshot(event), actor)


@router.post("/{event_id}/accept")
def accept_event(
    session_id: uuid.UUID,
    event_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> EventOut:
    _get_session(db, session_id, role)
    event = _event_or_404(db, session_id, event_id)
    _require_correctable(event)
    if event.source is not EventSource.AUTO:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"only auto events can be accepted; ball {event.ball_no} is already"
            f" '{event.source.value}' ground truth",
        )
    _accept_event(db, event, actor=role.value)
    db.flush()
    return EventOut.model_validate(event)


def _adjust_candidates(event: BallEvent, payload: AdjustIn) -> dict[str, int | None]:
    """Merge stored timings with the fields the payload explicitly set."""
    for name in NON_NULLABLE_TIMING_FIELDS:
        if name in payload.model_fields_set and getattr(payload, name) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name} cannot be null")
    return {
        name: getattr(payload, name) if name in payload.model_fields_set else getattr(event, name)
        for name in TIMING_FIELDS
    }


@router.post("/{event_id}/adjust")
def adjust_event(
    session_id: uuid.UUID,
    event_id: uuid.UUID,
    payload: AdjustIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> EventOut:
    """Adjust timing boundaries; the correction row records changed fields only."""
    _get_session(db, session_id, role)
    event = _event_or_404(db, session_id, event_id)
    _require_correctable(event)
    candidates = _adjust_candidates(event, payload)
    _validate_ordering(
        cast(int, candidates["start_ms"]),  # non-nullable: _adjust_candidates rejected nulls
        cast(int, candidates["release_ms"]),
        candidates["contact_ms"],
        cast(int, candidates["end_ms"]),
    )
    changed = {name: value for name, value in candidates.items() if value != getattr(event, name)}
    if changed:  # a no-op adjust is not a correction: nothing to version
        before = {name: getattr(event, name) for name in changed}
        for name, value in changed.items():
            setattr(event, name, value)
        event.source = EventSource.CORRECTED
        _correct(db, event, "adjust", before, changed, actor=role.value)
        db.flush()
    return EventOut.model_validate(event)


@router.post("/{event_id}/reject")
def reject_event(
    session_id: uuid.UUID,
    event_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> EventOut:
    """Mark a false detection invalid; its ball_no stays (numbering stability)."""
    _get_session(db, session_id, role)
    event = _event_or_404(db, session_id, event_id)
    _require_correctable(event)  # rejecting twice is a conflict, not a no-op
    before = _snapshot(event)
    event.valid = False
    _correct(db, event, "reject", before, _snapshot(event), actor=role.value)
    db.flush()
    return EventOut.model_validate(event)


def _dependent_tables_for(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> list[str]:
    return [
        label
        for model, label in EVENT_DEPENDENT_TABLES
        if db.scalar(
            select(model.id).where(model.session_id == session_id, model.ball_no == ball_no)
        )
        is not None
    ]


def _open_slot(db: OrmSession, session_id: uuid.UUID, target: int) -> None:
    """Free ``ball_no == target``, shifting the contiguous run above it by one.

    Every event in the run must be dependency-free auto output that is still
    valid; anything else is (or feeds) ground truth keyed by its number, so
    the insert aborts with 409 instead of silently orphaning rows.
    """
    events_by_no = {
        event.ball_no: event
        for event in db.scalars(select(BallEvent).where(BallEvent.session_id == session_id))
    }
    run: list[BallEvent] = []
    number = target
    while number in events_by_no:
        run.append(events_by_no[number])
        number += 1
    for event in run:
        if event.source is not EventSource.AUTO or not event.valid:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"cannot renumber ball {event.ball_no}: it is"
                f" {'rejected' if not event.valid else event.source.value} and its number is"
                " pinned; adding here would corrupt ground truth",
            )
        blockers = _dependent_tables_for(db, session_id, event.ball_no)
        if blockers:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"cannot renumber ball {event.ball_no}: dependent rows exist in"
                f" {', '.join(blockers)}; renumbering would orphan them",
            )
    for event in reversed(run):  # highest first so the unique constraint always holds
        event.ball_no += 1
        db.flush()


@router.post("/add", status_code=status.HTTP_201_CREATED)
def add_event(
    session_id: uuid.UUID,
    payload: EventAddIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> EventOut:
    """Add a missed ball after ``position`` (see module docstring for numbering)."""
    _get_session(db, session_id, role)
    _validate_ordering(payload.start_ms, payload.release_ms, payload.contact_ms, payload.end_ms)
    target = payload.position + 1
    occupied = db.scalar(
        select(BallEvent.id).where(BallEvent.session_id == session_id, BallEvent.ball_no == target)
    )
    if occupied is not None:
        _open_slot(db, session_id, target)
    event = BallEvent(
        session_id=session_id,
        ball_no=target,
        start_ms=payload.start_ms,
        release_ms=payload.release_ms,
        contact_ms=payload.contact_ms,
        end_ms=payload.end_ms,
        confidence=1.0,  # human-entered: full confidence by definition
        source=EventSource.MANUAL,
        valid=True,
    )
    db.add(event)
    db.flush()
    _correct(db, event, "add", None, _snapshot(event), actor=role.value)
    db.flush()
    return EventOut.model_validate(event)


@router.post("/bulk-accept")
def bulk_accept(
    session_id: uuid.UUID,
    payload: BulkAcceptIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> BulkAcceptOut:
    """Accept every valid auto event at/above the confidence threshold (US-D4 AC)."""
    _get_session(db, session_id, role)
    events = db.scalars(
        select(BallEvent)
        .where(
            BallEvent.session_id == session_id,
            BallEvent.valid.is_(True),
            BallEvent.source == EventSource.AUTO,
            BallEvent.confidence >= payload.min_confidence,
        )
        .order_by(BallEvent.ball_no)
    ).all()
    for event in events:
        _accept_event(db, event, actor=role.value)
    db.flush()
    return BulkAcceptOut(accepted=len(events))


def _export_rows(db: OrmSession, session_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every event of the session — rejected ones included, flagged ``valid: false``.

    A rejected event is a human-labeled detector false positive; dropping it
    would leave the training/eval feed without negative labels (US-D4), so it
    is exported alongside valid events with its reject correction history.
    """
    events = db.scalars(
        select(BallEvent).where(BallEvent.session_id == session_id).order_by(BallEvent.ball_no)
    ).all()
    corrections_by_event: dict[uuid.UUID, list[dict[str, Any]]] = {e.id: [] for e in events}
    corrections = db.scalars(
        select(EventCorrection)
        .where(EventCorrection.event_id.in_(corrections_by_event))
        .order_by(EventCorrection.at)
    ).all()
    for correction in corrections:
        corrections_by_event[correction.event_id].append(
            {
                "action": correction.action,
                "before": correction.before,
                "after": correction.after,
                "actor": correction.actor,
                "at": correction.at.isoformat(),
            }
        )
    return [
        {
            "ball_no": event.ball_no,
            "start_ms": event.start_ms,
            "release_ms": event.release_ms,
            "contact_ms": event.contact_ms,
            "end_ms": event.end_ms,
            "confidence": event.confidence,
            "source": event.source.value,
            "detector_version": event.detector_version,
            "valid": event.valid,
            "corrections": corrections_by_event[event.id],
        }
        for event in events
    ]


@router.get("/export")
def export_events(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
    format: Literal["json"] = "json",
) -> JSONResponse:
    """Ground-truth export: all events (rejected flagged ``valid: false``) + full
    correction history, audit-logged."""
    _get_session(db, session_id, role)
    response = JSONResponse(_export_rows(db, session_id))
    db.add(
        AuditLog(
            actor=role.value,
            action="share_export",
            entity="session",
            entity_id=str(session_id),
            detail={
                "format": format,
                "sha256": hashlib.sha256(response.body).hexdigest(),
                "surface": "events_export",
            },
        )
    )
    return response
