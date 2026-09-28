"""US-I4/US-I5 bowling-flight job: tracks + bounces + targets -> flight metrics.

For every valid ball event of a bowling session, derives the four v1.1
bowling flight metrics and merge-writes them into ``ball_metrics`` under
``phase='flight'`` with EXACTLY the metric names the BallRecord assembler
reads (``cricai_data.ballrecord._BOWLING_PAYLOAD_KEYS``):

- ``turn_cm``   — lateral deviation after pitching (US-I5), from the
  pitch-mapped track points around the per-camera bounce vertex;
- ``apex_m``    — greatest height between release and bounce (US-I5), only
  where the camera has a calibrated vertical scale;
- ``dip_flag``  — late-descent steepening vs a reference arc (US-I5);
- ``target_hit`` — the delivery's bounce scored against its declared
  ``bowling_targets`` zone (US-I4), reusing the US-C5 zone vocabulary.

Honesty rules (pinned):
    Every metric is nullable-with-reason — a pixel-only track yields null
    ``turn_cm``/``apex_m`` with the reason stated, never a fabricated number;
    a delivery with no declared target or no bounce scores null, never a
    guessed miss. All wording is geometric (turn, drift, dip, bounce).

Precedence & camera choice:
    A manual ``bounce_marks`` row (lowest camera_id, the representative rule)
    beats the auto ``bounce_estimates`` row for target scoring — manual beats
    machine, system-wide. Flight measurements walk the ball's tracks in
    ascending camera_id order and keep the first non-null value per metric,
    so a camera that cannot support a measurement degrades to the next one.

Write discipline (the US-F5 fusion pattern):
    MERGE, never replace: only the four job-owned keys are (re)written on the
    flight-phase row, so the US-E4 batting flight keys always survive. One
    commit per ball — a mid-run crash never strands partial work and a re-run
    is a plain re-run. The merge is a row-locked read-modify-write
    (``SELECT ... FOR UPDATE`` on PostgreSQL): a concurrent writer of the same
    ``(session, ball, flight)`` row — the metrics PUT endpoint, an overlapping
    re-run — waits for the lock and then merges onto the committed state
    instead of silently clobbering it (lost-update safety). A lost
    first-insert race on ``uq_metrics_ball_phase`` is retried onto the
    winner's row. On SQLite (single-writer unit DBs) the lock clause is a
    documented no-op.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from cricai_coaching.contact_metrics import MetricValue
from cricai_data.db import session_scope
from cricai_data.enums import Handedness, MetricPhase
from cricai_data.models import (
    BallEvent,
    BallMetrics,
    BallTrack,
    BounceEstimate,
    BounceMark,
    BowlingTarget,
    SessionBlock,
)
from cricai_data.models import Session as SessionRow
from cricai_vision import trajectory
from cricai_vision.bounce_estimate import TrackPayloadError, estimate_bounce, parse_track_payload
from cricai_vision.trajectory import (
    DEFAULT_FLIGHT_CONFIG,
    BouncePoint,
    FlightConfig,
    TargetZone,
    TrajectoryError,
)
from cricai_vision.zones import ZoneConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext

#: The four ball_metrics keys this job owns (BallRecord v1.1 bowling names).
BOWLING_FLIGHT_KEYS: Final = ("turn_cm", "apex_m", "dip_flag", "target_hit")

#: Metric name -> unit written on its payloads.
_UNITS: Final = {"turn_cm": "cm", "apex_m": "m", "dip_flag": "flag"}

#: Resolves a camera's calibrated vertical meters-per-pixel scale (US-I5);
#: ``None`` = no calibrated scale, apex height stays null-with-reason.
ScaleResolver = Callable[[Session, SessionRow, str], "float | None"]


def no_vertical_scale(_db: Session, _session: SessionRow, _camera_id: str) -> float | None:
    """Honest default: no camera carries a calibrated vertical scale yet."""
    return None


@dataclass(frozen=True)
class FlightInputs:
    """Knobs for one flight-analysis run (all defaults are the honest ones).

    ``batter_handedness`` frames the declared targets: US-I4 targets are
    stated in right-hand terms ("off-stump line to a right-hand batter"),
    matching the canonical-frame line channels.
    """

    scale_resolver: ScaleResolver = no_vertical_scale
    zone_config: ZoneConfig = field(default_factory=ZoneConfig)
    flight_config: FlightConfig = DEFAULT_FLIGHT_CONFIG
    batter_handedness: Handedness = Handedness.RIGHT


@dataclass(frozen=True)
class FlightBall:
    """One ball's persisted flight values (None = null-with-reason stored)."""

    ball_no: int
    turn_cm: float | None
    apex_m: float | None
    dip_flag: bool | None
    target_hit: bool | None


@dataclass(frozen=True)
class FlightSummary:
    """What one run wrote: every valid ball gets a row (nulls are data)."""

    session_id: uuid.UUID
    balls: tuple[FlightBall, ...]


def _block_for(blocks: Sequence[SessionBlock], event: BallEvent) -> SessionBlock | None:
    """The ball's practice block by event time (the BallRecord assembly rule)."""
    start_s = event.start_ms / 1000.0
    for block in blocks:
        if block.start_s <= start_s and (block.end_s is None or start_s < block.end_s):
            return block
    return None


def _target_for(
    targets: Sequence[BowlingTarget], block: SessionBlock | None
) -> BowlingTarget | None:
    """The ball's declared target: its block's newest target, else the newest
    session-wide one (``block_id`` NULL), else None (US-I4)."""
    if block is not None:
        scoped = [target for target in targets if target.block_id == block.id]
        if scoped:
            return scoped[0]  # targets arrive newest-first
    session_wide = [target for target in targets if target.block_id is None]
    return session_wide[0] if session_wide else None


def _resolved_bounce(
    db: Session, session_id: uuid.UUID, ball_no: int
) -> tuple[BouncePoint | None, str | None]:
    """(bounce point, provenance): the manual mark always wins (pinned US-F4)."""
    mark = db.scalars(
        select(BounceMark)
        .where(BounceMark.session_id == session_id, BounceMark.ball_no == ball_no)
        .order_by(BounceMark.camera_id)
    ).first()
    if mark is not None:
        return BouncePoint(mark.pitch_x, mark.pitch_y, 1.0), "manual"
    row = db.scalar(
        select(BounceEstimate).where(
            BounceEstimate.session_id == session_id, BounceEstimate.ball_no == ball_no
        )
    )
    if row is not None:
        return BouncePoint(row.pitch_x, row.pitch_y, row.confidence), "auto"
    return None, None


def _target_metric(score: trajectory.DeliveryScore, provenance: str | None) -> MetricValue:
    """``target_hit`` payload from one scored delivery (US-I4)."""
    if score.hit is None:
        return MetricValue(None, "flag", 0.0, reason=score.reason)
    source = f"{cast(str, provenance)}+{trajectory.TRAJECTORY_VERSION}"
    return MetricValue(score.hit, "flag", score.confidence, source=source)


def _metric(value: trajectory.FlightValue, unit: str, camera_id: str) -> MetricValue:
    """A flight measurement as a MetricValue, camera-stamped either way."""
    if value.value is None:
        return MetricValue(None, unit, 0.0, reason=f"{camera_id}: {value.reason}")
    source = f"{camera_id}+{trajectory.TRAJECTORY_VERSION}"
    return MetricValue(value.value, unit, value.confidence, source=source)


def _all_null(reason: str) -> dict[str, MetricValue]:
    """All three flight measurements null for one camera/ball, one reason."""
    return {key: MetricValue(None, unit, 0.0, reason=reason) for key, unit in _UNITS.items()}


def _camera_measurements(
    db: Session,
    ctx: WorkerContext,
    session: SessionRow,
    track: BallTrack,
    opts: FlightInputs,
) -> dict[str, MetricValue]:
    """turn/apex/dip candidates from one camera's track (US-I5).

    The bounce vertex is located on THIS camera's own track (the US-F4
    estimator), so the pre/post split never mixes cameras. Every failure mode
    degrades to null-with-reason for this camera — the caller may still get a
    value from the next one.
    """
    camera_id = track.camera_id
    if not ctx.store.exists(track.points_key):
        return _all_null(f"{camera_id}: track payload missing from the object store")
    raw_bytes = ctx.store.get(track.points_key)
    try:
        raw = json.loads(raw_bytes)
        payload = parse_track_payload(raw)
        points = trajectory.parse_flight_points(raw)
    except (json.JSONDecodeError, TrackPayloadError, TrajectoryError) as exc:
        return _all_null(f"{camera_id}: malformed track payload ({exc})")
    located = estimate_bounce(payload)
    if located.estimate is None:
        return _all_null(f"{camera_id}: {located.reason}")
    measured = trajectory.flight_measurements(
        points,
        located.estimate.ts_ms,
        vertical_scale_m_per_px=opts.scale_resolver(db, session, camera_id),
        config=opts.flight_config,
    )
    return {
        "turn_cm": _metric(measured.turn_cm, _UNITS["turn_cm"], camera_id),
        "apex_m": _metric(measured.apex_m, _UNITS["apex_m"], camera_id),
        "dip_flag": _metric(measured.dip_flag, _UNITS["dip_flag"], camera_id),
    }


def _flight_metrics(
    db: Session,
    ctx: WorkerContext,
    session: SessionRow,
    ball_no: int,
    opts: FlightInputs,
) -> dict[str, MetricValue]:
    """turn/apex/dip for one ball: first non-null per metric across cameras."""
    tracks = db.scalars(
        select(BallTrack)
        .where(BallTrack.session_id == session.id, BallTrack.ball_no == ball_no)
        .order_by(BallTrack.camera_id)
    ).all()
    if not tracks:
        return _all_null("no ball track for this delivery")
    best: dict[str, MetricValue] = {}
    for track in tracks:
        for key, candidate in _camera_measurements(db, ctx, session, track, opts).items():
            current = best.get(key)
            if current is None or (current.value is None and candidate.value is not None):
                best[key] = candidate
    return best


def _merge_flight_metrics(
    db: Session, session_id: uuid.UUID, ball_no: int, entries: dict[str, dict[str, Any]]
) -> None:
    """Upsert-by-key into the flight-phase row, preserving every other key
    (the US-E4 batting flight metrics survive a bowling re-run, and back).

    Row-locked read-modify-write: the SELECT takes ``FOR UPDATE`` (PostgreSQL;
    a no-op on the single-writer SQLite unit DBs) with ``populate_existing``
    so the merge always starts from the row's committed state, never a stale
    identity-map copy — a concurrent flight-phase writer therefore cannot be
    silently clobbered. Losing the first-insert race on
    ``uq_metrics_ball_phase`` is handled by rolling back and merging onto the
    winner's committed row.
    """
    locked = (
        select(BallMetrics)
        .where(
            BallMetrics.session_id == session_id,
            BallMetrics.ball_no == ball_no,
            BallMetrics.phase == MetricPhase.FLIGHT,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = db.execute(locked).scalar_one_or_none()
    if row is None:
        row = BallMetrics(
            session_id=session_id, ball_no=ball_no, phase=MetricPhase.FLIGHT, metrics={}
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # A concurrent first writer committed the row between the SELECT
            # and this INSERT; take their committed row (now visible) under
            # the lock and merge onto it instead of aborting the run.
            db.rollback()
            row = db.execute(locked).scalar_one()
    merged = dict(row.metrics)
    merged.update(entries)
    row.metrics = merged  # fresh dict: plain JSON columns don't track in-place edits


def analyze_session_flight(
    ctx: WorkerContext, session_id: uuid.UUID, *, inputs: FlightInputs | None = None
) -> FlightSummary:
    """Derive and persist the bowling flight metrics for one session (US-I4/I5).

    Every valid ball event gets all four keys written (null-with-reason is
    data, not failure) under ``phase='flight'``; rejected events are not
    balls (US-D4) and are never touched. One commit per ball; idempotent —
    identical inputs rewrite identical values.
    """
    opts = inputs if inputs is not None else FlightInputs()
    balls: list[FlightBall] = []
    with session_scope(ctx.session_factory) as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        events = db.scalars(
            select(BallEvent)
            .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
            .order_by(BallEvent.ball_no)
        ).all()
        blocks = db.scalars(
            select(SessionBlock)
            .where(SessionBlock.session_id == session_id)
            .order_by(SessionBlock.block_no)
        ).all()
        targets = db.scalars(
            select(BowlingTarget)
            .where(BowlingTarget.session_id == session_id)
            .order_by(BowlingTarget.created_at.desc(), BowlingTarget.id)
        ).all()
        for event in events:
            target_row = _target_for(targets, _block_for(blocks, event))
            target = (
                TargetZone(key=str(target_row.id), line=target_row.line, length=target_row.length)
                if target_row is not None
                else None
            )
            bounce, provenance = _resolved_bounce(db, session_id, event.ball_no)
            score = trajectory.score_delivery(
                opts.zone_config,
                ball_no=event.ball_no,
                target=target,
                bounce=bounce,
                handedness=opts.batter_handedness,
            )
            entries = _flight_metrics(db, ctx, session, event.ball_no, opts)
            entries["target_hit"] = _target_metric(score, provenance)
            _merge_flight_metrics(
                db,
                session_id,
                event.ball_no,
                {key: value.to_payload() for key, value in entries.items()},
            )
            db.commit()  # per-ball durability: crash-resume is a plain re-run
            balls.append(
                FlightBall(
                    ball_no=event.ball_no,
                    turn_cm=cast("float | None", entries["turn_cm"].value),
                    apex_m=cast("float | None", entries["apex_m"].value),
                    dip_flag=cast("bool | None", entries["dip_flag"].value),
                    target_hit=cast("bool | None", entries["target_hit"].value),
                )
            )
    return FlightSummary(session_id=session_id, balls=tuple(balls))
