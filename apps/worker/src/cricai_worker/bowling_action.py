"""Per-ball bowling-action job (US-I2/I3): bowler pose -> release + checkpoints.

For every valid ball event of a BOWLING session with bowling-camera footage,
composes the Phase-6 leg-spin action pipeline:

1. the injected :class:`~cricai_vision.pose.PoseProvider` extracts the
   bowler's pose track from the resolved frames (production providers select
   the subject under the :mod:`cricai_vision.bowler_pose` crease-zone prior;
   tests inject deterministic fakes),
2. :func:`cricai_vision.release.detect_release` finds the release frame from
   wrist/arm kinematics (US-I3: ±2 frames @ 120 fps AC),
3. :func:`cricai_coaching.checkpoints.evaluate_checkpoints` scores the action
   checkpoints at that frame (US-I2), and
4. :func:`cricai_vision.release.release_height_cm` measures release height —
   the triangulated stereo seam first, the pose+scale path as fallback.

Results land in ``ball_metrics`` (phase ``pre_release``) under EXACTLY the
bowling metric names the BallRecord v1.1 assembler reads —
``release_height_cm``, ``release_frame_offset``, ``brace_state``,
``falling_away_deg`` — plus the contract #6 release primitives
``release_frame``, ``release_ms``, ``hand_xy`` (the per-ball release-point
scatter datum consumed by the US-I3/I7 report) and the head-position
checkpoint pair. Geometric language only: no revolution or axis claims, ever
(US-I5 honest limits).

MERGE, never replace (the US-F5 pattern): only :data:`BOWLING_ACTION_KEYS`
are (re)written in the ball's pre-release row, per ball, with a per-ball
commit — crash-resume is a plain re-run and identical inputs write identical
values. A ball whose release cannot be detected still writes every key,
null-with-reason (US-I2 AC: no silent zeros); only balls without footage are
skipped, loudly, in the summary.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cricai_coaching.checkpoints import (
    CHECKPOINT_KEYS,
    DEFAULT_CHECKPOINT_CONFIG,
    CheckpointConfig,
    evaluate_checkpoints,
)
from cricai_coaching.contact_metrics import MetricValue
from cricai_data.db import session_scope
from cricai_data.enums import MetricPhase, SessionType
from cricai_data.models import BallEvent, BallMetrics
from cricai_data.models import Session as SessionRow
from cricai_vision.pose import PoseProvider
from cricai_vision.release import (
    DEFAULT_RELEASE_CONFIG,
    ReleaseConfig,
    detect_release,
    release_height_cm,
)
from cricai_vision.triangulate import Track3D
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext

#: Side-on bowling camera (square of the bowling crease, US-I1): the release
#: view the action pipeline consumes by default.
DEFAULT_CAMERA = "C5"

#: Skip reason reported in :class:`BowlingActionSummary.skipped`.
SKIP_NO_FOOTAGE = "no footage for the bowling camera"

#: The ball_metrics keys this job owns (phase ``pre_release``): the four
#: BallRecord v1.1 assembler names + the contract #6 release primitives +
#: the head-position checkpoint pair.
BOWLING_ACTION_KEYS: tuple[str, ...] = (
    "release_frame",
    "release_ms",
    "hand_xy",
    "release_height_cm",
    "release_frame_offset",
    *CHECKPOINT_KEYS,
)

#: Returns (frames, fps) for one (session, ball, camera), or None when no footage exists.
FramesResolver = Callable[[uuid.UUID, int, str], "tuple[Sequence[Any], float] | None"]

#: Returns the ball's triangulated 3D track (US-F6 stereo seam), or None.
Track3DResolver = Callable[[uuid.UUID, int], "Track3D | None"]


@dataclass(frozen=True)
class BowlingActionInputs:
    """Optional seams and knobs for one bowling-action run."""

    camera_id: str = DEFAULT_CAMERA
    px_per_cm: float | None = None
    track3d_resolver: Track3DResolver | None = None
    release_config: ReleaseConfig = DEFAULT_RELEASE_CONFIG
    checkpoint_config: CheckpointConfig = DEFAULT_CHECKPOINT_CONFIG

    def __post_init__(self) -> None:
        if self.px_per_cm is not None and self.px_per_cm <= 0:
            raise ValueError(f"px_per_cm must be positive, got {self.px_per_cm}")


@dataclass(frozen=True)
class AnalyzedBall:
    """One ball's outcome as written to ball_metrics (nulls carried honestly)."""

    ball_no: int
    release_frame: int | None
    release_frame_offset: int | None
    release_height_cm: float | None
    brace_state: str | None
    detection_reason: str | None


@dataclass(frozen=True)
class BowlingActionSummary:
    """What one run did: balls analyzed vs skipped (with reasons)."""

    session_id: uuid.UUID
    analyzed: tuple[AnalyzedBall, ...]
    skipped: tuple[tuple[int, str], ...]  # (ball_no, reason)

    @property
    def analyzed_count(self) -> int:
        return len(self.analyzed)


def _null_entries(reason: str) -> dict[str, MetricValue]:
    """Every owned key null with the detection failure's reason (no silent zeros)."""
    units = {
        "release_frame": "frame",
        "release_ms": "ms",
        "hand_xy": "px",
        "release_height_cm": "cm",
        "release_frame_offset": "frames",
        "brace_state": "class",
        "falling_away_deg": "deg",
        "head_offset_at_release_px": "px",
        "head_offset_at_release_cm": "cm",
    }
    return {
        key: MetricValue(value=None, unit=units[key], confidence=0.0, reason=reason)
        for key in BOWLING_ACTION_KEYS
    }


@dataclass(frozen=True)
class _DetectedRelease:
    """A successful detection, narrowed to non-null values for the entry builders."""

    frame: int
    hand_xy: tuple[float, float]
    ms_session: float  # session-timeline ms: event window start + track offset
    confidence: float


def _release_entries(
    detected: _DetectedRelease, event: BallEvent, fps: float
) -> dict[str, MetricValue]:
    """The contract #6 release primitives for one detected release.

    ``release_ms`` is session-timeline milliseconds; ``release_frame_offset``
    is the detected frame minus the event's nominal release frame — the drift
    the US-I3 ±2-frame benchmark scores.
    """
    expected_frame = round((event.release_ms - event.start_ms) * fps / 1000.0)
    return {
        "release_frame": MetricValue(
            value=detected.frame, unit="frame", confidence=detected.confidence
        ),
        "release_ms": MetricValue(
            value=detected.ms_session, unit="ms", confidence=detected.confidence
        ),
        "hand_xy": MetricValue(
            value=[detected.hand_xy[0], detected.hand_xy[1]],
            unit="px",
            confidence=detected.confidence,
        ),
        "release_frame_offset": MetricValue(
            value=detected.frame - expected_frame, unit="frames", confidence=detected.confidence
        ),
    }


def _merge_pre_release_metrics(
    db: Session, session_id: uuid.UUID, ball_no: int, entries: dict[str, dict[str, Any]]
) -> None:
    """Upsert-by-key into the ball's pre-release metrics row, preserving the rest.

    Row-locked read-modify-write (the :mod:`~cricai_worker.bowling_flight`
    pattern): ``FOR UPDATE`` + ``populate_existing`` so a concurrent
    pre-release writer is merged with, never silently clobbered; a lost
    first-insert race on ``uq_metrics_ball_phase`` retries onto the winner's
    committed row. On SQLite (single-writer unit DBs) the lock is a no-op.
    """
    locked = (
        select(BallMetrics)
        .where(
            BallMetrics.session_id == session_id,
            BallMetrics.ball_no == ball_no,
            BallMetrics.phase == MetricPhase.PRE_RELEASE,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = db.execute(locked).scalar_one_or_none()
    if row is None:
        row = BallMetrics(
            session_id=session_id, ball_no=ball_no, phase=MetricPhase.PRE_RELEASE, metrics={}
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = db.execute(locked).scalar_one()
    merged = dict(row.metrics)  # foreign keys survive (merge, never replace)
    merged.update(entries)
    row.metrics = merged  # fresh dict: plain JSON columns don't track in-place edits


def _analyze_ball(
    event: BallEvent,
    resolved: tuple[Sequence[Any], float],
    provider: PoseProvider,
    inputs: BowlingActionInputs,
    session_id: uuid.UUID,
) -> dict[str, MetricValue]:
    """One ball's full metric set: release primitives + height + checkpoints."""
    frames, fps = resolved
    track = provider.extract(frames, fps=fps)
    detection = detect_release(track, config=inputs.release_config)
    if detection.release_frame is None or detection.release_ms is None or detection.hand_xy is None:
        return _null_entries(str(detection.reason))
    detected = _DetectedRelease(
        frame=detection.release_frame,
        hand_xy=detection.hand_xy,
        ms_session=float(event.start_ms) + detection.release_ms,
        confidence=detection.confidence,
    )
    track3d = (
        inputs.track3d_resolver(session_id, event.ball_no)
        if inputs.track3d_resolver is not None
        else None
    )
    height = release_height_cm(
        track,
        detection,
        stereo=(track3d, detected.ms_session) if track3d is not None else None,
        px_per_cm=inputs.px_per_cm,
        config=inputs.release_config,
    )
    entries = _release_entries(detected, event, track.fps)
    entries["release_height_cm"] = MetricValue(
        value=height.value_cm,
        unit="cm",
        confidence=height.confidence,
        reason=height.reason,
        source=height.source,
    )
    entries.update(
        evaluate_checkpoints(
            track,
            release_frame=detected.frame,
            px_per_cm=inputs.px_per_cm,
            config=inputs.checkpoint_config,
        )
    )
    return entries


def _scalar(entries: dict[str, MetricValue], key: str) -> Any:
    return entries[key].value


def analyze_session_bowling_action(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    provider: PoseProvider,
    frames_resolver: FramesResolver,
    inputs: BowlingActionInputs | None = None,
) -> BowlingActionSummary:
    """Analyze the bowling action for every valid ball event with footage.

    Raises :class:`ValueError` when the session does not exist or is not a
    bowling session (bowling fields belong only to ``mode == "bowling"``
    records, BallRecord v1.1 contract #1).
    """
    opts = inputs if inputs is not None else BowlingActionInputs()
    analyzed: list[AnalyzedBall] = []
    skipped: list[tuple[int, str]] = []
    with session_scope(ctx.session_factory) as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        if session.session_type is not SessionType.BOWLING:
            raise ValueError(
                f"session {session_id} is {session.session_type.value}, not bowling "
                "(US-I2 action metrics apply to bowling sessions only)"
            )
        events = (
            db.execute(
                select(BallEvent)
                .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
                .order_by(BallEvent.ball_no)
            )
            .scalars()
            .all()
        )
        for event in events:
            resolved = frames_resolver(session_id, event.ball_no, opts.camera_id)
            if resolved is None:
                skipped.append((event.ball_no, SKIP_NO_FOOTAGE))
                continue
            entries = _analyze_ball(event, resolved, provider, opts, session_id)
            _merge_pre_release_metrics(
                db,
                session_id,
                event.ball_no,
                {key: metric.to_payload() for key, metric in entries.items()},
            )
            db.commit()  # per-ball durability: crash-resume is a plain re-run
            release_frame = _scalar(entries, "release_frame")
            analyzed.append(
                AnalyzedBall(
                    ball_no=event.ball_no,
                    release_frame=release_frame,
                    release_frame_offset=_scalar(entries, "release_frame_offset"),
                    release_height_cm=_scalar(entries, "release_height_cm"),
                    brace_state=_scalar(entries, "brace_state"),
                    detection_reason=(
                        entries["release_frame"].reason if release_frame is None else None
                    ),
                )
            )
    return BowlingActionSummary(
        session_id=session_id, analyzed=tuple(analyzed), skipped=tuple(skipped)
    )
