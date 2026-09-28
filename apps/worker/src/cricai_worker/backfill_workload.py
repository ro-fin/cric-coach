"""US-H1: derive ``auto_backfill`` bowling-ledger entries from tagged sessions.

Rules (pinned):

- Only ``session_type == BOWLING`` sessions produce auto entries. MIXED
  sessions contribute ONLY via explicit manual entries — the tag/event schema
  cannot attribute per-ball bowler identity inside a mixed session
  (documented V2 limitation, plan scope decision).
- ``balls`` is the conservative maximum of the session's valid ball-event
  count and its manual tag count: the safe failure mode is overcounting a
  young bowler's workload, never undercounting it.
- ``intensity`` comes from session context: ``machine_settings`` carrying
  ``bowling_style == "spin"`` classifies the session as SPIN; everything else
  defaults to PACE_INTENT (the lumbar-risk class — conservative). Throwdown
  volumes are manual-entry territory. Free-text ``notes`` are UNTRUSTED
  (US-G4) and never drive safety classification.
- Idempotent, delete-and-recreate: each processed session first loses ONLY its
  own ``source == "auto_backfill"`` rows, then gets at most one fresh entry.
  Manual rows — even session-linked ones — are never touched. A session that
  is no longer BOWLING (or now counts zero balls) has its stale auto rows
  removed and nothing recreated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from cricai_coaching.workload import SOURCE_AUTO_BACKFILL
from cricai_data.db import session_scope
from cricai_data.enums import DeliveryIntensity, SessionType
from cricai_data.models import BallEvent, BallTag, BowlingLedgerEntry
from cricai_data.models import Session as SessionRow
from sqlalchemy import CursorResult, delete, func, or_, select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext

#: ``created_by`` stamped on every auto entry (audit trail, US-H1).
JOB_NAME = "backfill_workload"

#: ``machine_settings`` key inspected for the session's bowling style.
BOWLING_STYLE_KEY = "bowling_style"


@dataclass(frozen=True)
class BackfillResult:
    """What one run did — job logs surface these numbers."""

    sessions_processed: int
    entries_created: int
    entries_deleted: int


def _session_intensity(session: SessionRow) -> DeliveryIntensity:
    """Classify the session's deliveries from structured session context."""
    machine_settings = session.machine_settings or {}
    style = str(machine_settings.get(BOWLING_STYLE_KEY, "")).lower()
    if style == "spin":
        return DeliveryIntensity.SPIN
    return DeliveryIntensity.PACE_INTENT


def _ball_count(db: OrmSession, session_id: uuid.UUID) -> tuple[int, int]:
    """(valid ball-event count, manual tag count) for the session."""
    events = db.scalar(
        select(func.count())
        .select_from(BallEvent)
        .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
    )
    tags = db.scalar(
        select(func.count()).select_from(BallTag).where(BallTag.session_id == session_id)
    )
    return int(events or 0), int(tags or 0)


def ensure_session_ledger(db: OrmSession, session: SessionRow) -> tuple[int, int]:
    """Derive ONE session's ``auto_backfill`` ledger row, idempotently (US-H1).

    Deletes only this session's ``source == "auto_backfill"`` rows, then (for a
    BOWLING session with counted balls) recreates a single fresh entry; manual
    rows are never touched. Returns ``(created, deleted)`` for the batch job's
    tally. This is the shared per-session unit: the batch :func:`backfill_workload`
    loops it over every session, and the pipeline's safety evaluation calls it for
    the triggering session so its own bowling counts toward the H1 ceiling before
    the allowance is read (finding 2). Idempotent, so both callers agree and a
    re-run never duplicates.
    """
    deleted = cast(
        "CursorResult[Any]",
        db.execute(
            delete(BowlingLedgerEntry).where(
                BowlingLedgerEntry.session_id == session.id,
                BowlingLedgerEntry.source == SOURCE_AUTO_BACKFILL,
            )
        ),
    ).rowcount
    if session.session_type != SessionType.BOWLING:
        return 0, deleted
    event_count, tag_count = _ball_count(db, session.id)
    balls = max(event_count, tag_count)
    if balls == 0:
        return 0, deleted
    db.add(
        BowlingLedgerEntry(
            player_id=session.player_id,
            entry_date=session.session_date,
            balls=balls,
            intensity=_session_intensity(session),
            session_id=session.id,
            source=SOURCE_AUTO_BACKFILL,
            note=f"auto backfill: events={event_count}, tags={tag_count}",
            created_by=JOB_NAME,
        )
    )
    return 1, deleted


def _target_sessions(db: OrmSession, session_id: uuid.UUID | None) -> list[SessionRow]:
    """The sessions one run touches: an explicit target, or every BOWLING
    session plus any session still carrying stale auto rows."""
    if session_id is not None:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise ValueError(f"session {session_id} not found")
        return [session]
    stale = (
        select(BowlingLedgerEntry.session_id)
        .where(
            BowlingLedgerEntry.source == SOURCE_AUTO_BACKFILL,
            BowlingLedgerEntry.session_id.is_not(None),
        )
        .scalar_subquery()
    )
    rows = db.scalars(
        select(SessionRow)
        .where(or_(SessionRow.session_type == SessionType.BOWLING, SessionRow.id.in_(stale)))
        .order_by(SessionRow.session_date, SessionRow.created_at)
    )
    return list(rows)


def backfill_workload(ctx: WorkerContext, session_id: uuid.UUID | None = None) -> BackfillResult:
    """Backfill the bowling ledger from tagged sessions (US-H1).

    ``session_id`` narrows the run to one session (raising ``ValueError`` if
    it does not exist); otherwise every BOWLING session — and any session
    whose stale auto rows need cleaning — is reprocessed. Re-running with the
    same inputs is a no-op state-wise (idempotent: no duplicate rows).
    """
    with session_scope(ctx.session_factory) as db:
        sessions = _target_sessions(db, session_id)
        created_total = deleted_total = 0
        for session in sessions:
            created, deleted = ensure_session_ledger(db, session)
            created_total += created
            deleted_total += deleted
        return BackfillResult(
            sessions_processed=len(sessions),
            entries_created=created_total,
            entries_deleted=deleted_total,
        )
