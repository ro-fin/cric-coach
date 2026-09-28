"""Auto ball-event detection job (US-D1): persist detector candidates as ``ball_events``.

EVENT REPLACE RULE (pinned; the US-D4 corrections flow relies on it):
    Re-running auto detection REPLACES exactly the rows where
    ``source == EventSource.AUTO`` and ``valid is True``. MANUAL and CORRECTED
    rows, and ``valid == False`` (rejected) rows - whatever their source - are
    never deleted, updated, or resurrected by a re-run. When any such preserved
    rows exist, newly detected balls are numbered starting above the maximum
    preserved ``ball_no``; otherwise numbering starts at 1. Re-running with the
    same inputs therefore replaces rather than duplicates (idempotent, US-D1 AC).

DEPENDENT-ROW GUARD (same protection philosophy as the corrections API):
    Downstream tables (the canonical ``cricai_data.models.EVENT_DEPENDENT_TABLES``
    list, shared with the US-D4 renumbering guard) join events on
    ``(session_id, ball_no)`` with no FK to ``BallEvent.id``, so deleting an
    auto event whose ball number has dependent rows would silently re-associate
    those rows with whatever event next claims the number (clips of one
    delivery attached to another delivery's event). Before replacing anything,
    every to-be-deleted event is checked for rows in the dependent tables and
    for :class:`EventCorrection` history; any hit aborts the whole run with
    :class:`DetectionBlockedError` naming the blocking ball numbers - nothing
    is modified, never silently mis-associate. Dependents keyed to PRESERVED
    balls never block: preserved events keep their numbers and new detections
    are numbered above them. A virgin auto-only session (no downstream rows
    yet) re-runs idempotently as before.

    KNOWN LIMITATION (deliberate, recorded in docs/specs/phase-3-plan.md):
    machine-derived dependents (clips, pose tracks, ball metrics) have no
    removal API, so once the pipeline has processed a session the guard blocks
    every re-detection until an operator removes the dependent rows out of
    band. Block beats corrupt; the delete-dependents-and-re-derive cascade is
    Phase 5 job orchestration (Epic J).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from cricai_data.db import session_scope
from cricai_data.enums import EventSource
from cricai_data.models import EVENT_DEPENDENT_TABLES, BallEvent, EventCorrection
from cricai_data.models import Session as SessionRow
from cricai_vision.events import DETECTOR_VERSION, detect_events
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext


class DetectionBlockedError(RuntimeError):
    """Re-detection would delete auto events that ground-truth rows still point at.

    Structured for job results/logs: ``session_id`` plus ``blockers`` mapping
    each blocking ``ball_no`` to the labels of the tables that pin it. The run
    aborts before any write, so the session is exactly as it was. The message
    is honest about remediation: machine-derived dependents (clips, pose
    tracks, ball metrics) currently have no removal API, so re-detection on a
    processed session requires operator intervention (see the KNOWN LIMITATION
    in the module docstring).
    """

    def __init__(self, session_id: uuid.UUID, blockers: dict[int, tuple[str, ...]]) -> None:
        self.session_id = session_id
        self.blockers = blockers
        detail = "; ".join(
            f"ball {ball_no} has {', '.join(labels)}"
            for ball_no, labels in sorted(blockers.items())
        )
        super().__init__(
            f"re-detection blocked for session {session_id}: replacing auto events would"
            f" mis-associate dependent rows ({detail}); nothing was modified."
            " Machine-derived dependents currently have no removal API, so re-detection on"
            " a processed session requires operator intervention (remove the dependent rows"
            " out of band, or delete the whole session via the US-L3 privacy API); the"
            " re-derive cascade is deferred to Phase 5 job orchestration (Epic J)"
        )


def _replace_blockers(
    db: OrmSession, session_id: uuid.UUID, replaceable: Sequence[BallEvent]
) -> dict[int, tuple[str, ...]]:
    """Map each to-be-deleted ball_no to the dependent tables that pin it."""
    blockers: dict[int, tuple[str, ...]] = {}
    for row in replaceable:
        labels = [
            label
            for model, label in EVENT_DEPENDENT_TABLES
            if db.scalar(
                select(model.id).where(model.session_id == session_id, model.ball_no == row.ball_no)
            )
            is not None
        ]
        if (
            db.scalar(select(EventCorrection.id).where(EventCorrection.event_id == row.id))
            is not None
        ):
            labels.append("event corrections")
        if labels:
            blockers[row.ball_no] = tuple(labels)
    return blockers


@dataclass(frozen=True)
class DetectionSummary:
    """What one detection run did to a session's auto events."""

    session_id: uuid.UUID
    detector_version: str
    detected: int  # candidates found and written this run
    replaced: int  # prior source=auto valid=True rows deleted
    preserved: int  # manual/corrected/rejected rows left untouched
    first_ball_no: int | None  # numbering start; None when nothing was written


def run_detection(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    motion_energy: Sequence[float],
    fps: float,
    audio_onsets_ms: Sequence[int] | None = None,
) -> DetectionSummary:
    """Detect ball events and write them per the EVENT REPLACE RULE above.

    Pure segmentation runs (and validates its inputs) before any database work,
    so an :class:`cricai_vision.events.EventDetectionError` never leaves partial
    state. Raises :class:`LookupError` when the session does not exist and
    :class:`DetectionBlockedError` (before any write) when a to-be-replaced
    event has dependent rows - see the DEPENDENT-ROW GUARD above.
    """
    candidates = detect_events(motion_energy, fps=fps, audio_onsets_ms=audio_onsets_ms)
    with session_scope(ctx.session_factory) as db:
        if db.get(SessionRow, session_id) is None:
            raise LookupError(f"session {session_id} not found")
        existing = db.scalars(select(BallEvent).where(BallEvent.session_id == session_id)).all()
        replaceable = [row for row in existing if row.source == EventSource.AUTO and row.valid]
        preserved = [row for row in existing if not (row.source == EventSource.AUTO and row.valid)]
        blockers = _replace_blockers(db, session_id, replaceable)
        if blockers:
            raise DetectionBlockedError(session_id, blockers)
        for row in replaceable:
            db.delete(row)
        db.flush()  # free the (session_id, ball_no) slots before re-inserting them
        next_ball_no = max((row.ball_no for row in preserved), default=0) + 1
        for offset, candidate in enumerate(candidates):
            db.add(
                BallEvent(
                    session_id=session_id,
                    ball_no=next_ball_no + offset,
                    start_ms=candidate.start_ms,
                    release_ms=candidate.release_ms,
                    contact_ms=candidate.contact_ms,
                    end_ms=candidate.end_ms,
                    confidence=candidate.confidence,
                    source=EventSource.AUTO,
                    detector_version=DETECTOR_VERSION,
                    valid=True,
                )
            )
        return DetectionSummary(
            session_id=session_id,
            detector_version=DETECTOR_VERSION,
            detected=len(candidates),
            replaced=len(replaceable),
            preserved=len(preserved),
            first_ball_no=next_ball_no if candidates else None,
        )
