"""THE manual-vs-auto bounce precedence resolver (US-F4 pinned contract).

Exactly one place decides which bounce a ball effectively has, so the heatmap
and later exports can never drift:

* A manual :class:`~cricai_data.models.BounceMark` ALWAYS wins over the
  automatic :class:`~cricai_data.models.BounceEstimate` for the same ball.
  Auto reprocessing only writes ``bounce_estimates`` (the worker never touches
  ``bounce_marks``), so a manual override survives any number of re-runs.
* Manual multi-camera dedupe mirrors the US-C6 aggregation rule: the
  representative mark prefers marks with derived zone classes, then the lowest
  camera_id; ANY flagged mark flags the ball. Manual bounces carry
  ``confidence=None`` (a click is ground truth, not a model output).
* Auto bounces carry the estimator's confidence and are never flagged (the
  cross-camera review flag is a manual-marking concept).
* Rejected events are not balls (US-D4): an auto estimate is served only while
  its ball's event is currently valid, so rejecting an event hides its phantom
  ``bounce_estimates`` row immediately — no re-run of estimate_bounces needed
  (the next run deletes the row; this gate mirrors the worker's
  ``_valid_event_balls`` rule at read time). Manual marks are ground truth and
  are never gated.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from cricai_data.enums import EventSource, Length, Line
from cricai_data.models import BallEvent, BounceEstimate, BounceMark
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession


@dataclass(frozen=True)
class ResolvedBounce:
    """The effective bounce for one ball, with its source and confidence."""

    ball_no: int
    pitch_x: float
    pitch_y: float
    line: Line | None
    length: Length | None
    source: EventSource  # MANUAL (bounce_marks) or AUTO (bounce_estimates)
    confidence: float | None  # estimator confidence; None for manual clicks
    flagged: bool  # manual cross-camera review flag; auto rows never flag


def _from_marks(ball_no: int, marks: list[BounceMark]) -> ResolvedBounce:
    representative = min(
        marks, key=lambda mark: (mark.line is None or mark.length is None, mark.camera_id)
    )
    return ResolvedBounce(
        ball_no=ball_no,
        pitch_x=representative.pitch_x,
        pitch_y=representative.pitch_y,
        line=representative.line,
        length=representative.length,
        source=EventSource.MANUAL,
        confidence=None,
        flagged=any(mark.flagged_for_review for mark in marks),
    )


def _from_estimate(row: BounceEstimate) -> ResolvedBounce:
    return ResolvedBounce(
        ball_no=row.ball_no,
        pitch_x=row.pitch_x,
        pitch_y=row.pitch_y,
        line=row.line,
        length=row.length,
        source=EventSource.AUTO,
        confidence=row.confidence,
        flagged=False,
    )


def _valid_event_balls(db: OrmSession, session_id: uuid.UUID) -> set[int]:
    """Balls that exist per US-D4: rejected events are not balls.

    Mirrors the worker's ``estimate_bounces._valid_event_balls`` rule at read
    time so a phantom auto row (its event rejected after the job ran) is never
    served while it waits for the next job run to delete it.
    """
    return set(
        db.scalars(
            select(BallEvent.ball_no).where(
                BallEvent.session_id == session_id, BallEvent.valid.is_(True)
            )
        )
    )


def resolve_session_bounces(db: OrmSession, session_id: uuid.UUID) -> list[ResolvedBounce]:
    """Effective bounce per ball for one session, ordered by ball_no."""
    marks_by_ball: dict[int, list[BounceMark]] = defaultdict(list)
    for mark in db.scalars(
        select(BounceMark)
        .where(BounceMark.session_id == session_id)
        .order_by(BounceMark.ball_no, BounceMark.camera_id)
    ):
        marks_by_ball[mark.ball_no].append(mark)
    resolved = {ball_no: _from_marks(ball_no, marks) for ball_no, marks in marks_by_ball.items()}
    valid_balls = _valid_event_balls(db, session_id)
    for row in db.scalars(select(BounceEstimate).where(BounceEstimate.session_id == session_id)):
        # The manual mark always wins; an auto estimate is served only while
        # its ball's event is currently valid (US-D4).
        if row.ball_no not in resolved and row.ball_no in valid_balls:
            resolved[row.ball_no] = _from_estimate(row)
    return [resolved[ball_no] for ball_no in sorted(resolved)]


def resolve_bounce(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> ResolvedBounce | None:
    """Effective bounce for one ball: manual mark(s) win, else the auto estimate."""
    marks = list(
        db.scalars(
            select(BounceMark).where(
                BounceMark.session_id == session_id, BounceMark.ball_no == ball_no
            )
        )
    )
    if marks:
        return _from_marks(ball_no, marks)
    estimate = db.scalar(
        select(BounceEstimate).where(
            BounceEstimate.session_id == session_id, BounceEstimate.ball_no == ball_no
        )
    )
    if estimate is None or ball_no not in _valid_event_balls(db, session_id):
        return None  # no estimate, or a phantom for a rejected ball (US-D4)
    return _from_estimate(estimate)
