"""Per-ball ball-tracking job (US-F3): valid events x evidence cameras -> ball_tracks.

For every valid :class:`~cricai_data.models.BallEvent` and each camera with
evidence footage, the injected :class:`~cricai_vision.detect.DetectionProvider`
(default :class:`~cricai_vision.detect.FakeDetectionProvider` until the US-F2
adapter has trained weights) detects over the event window, the US-F3 tracker
builds the trajectory, the pinned payload lands at
``sessions/{session_id}/balls/{ball_no}/track-{camera_id}.json`` and a
``ball_tracks`` row is upserted on the (session_id, ball_no, camera_id) unique
triple — the row mirrors the payload's coverage/segments/flags/confidence.

Dependent-row semantics (same philosophy as ``detect_events``):
    Tracks are machine-derived. A re-run REPLACES exactly the rows this job
    owns — the same-triple upsert with the current ``tracker_version`` — and
    never touches any other table (bounce marks, tags, clips, ... are other
    stories' ground truth).

Crash-resume + idempotency:
    Each track lands in two steps: the payload is written to a per-run
    staging key and committed on the row, then promoted to the pinned key
    (atomic replace) and the row re-pointed — the committed row always
    describes the bytes at its ``points_key``, so a mid-run crash never
    strands an overwritten payload behind a stale committed row. Re-running
    converges on the pinned key and sweeps staging leftovers. The sweep is
    namespace-wide, so a PARALLEL run's sweep may delete this run's staging
    object mid-promotion: the promote tolerates the missing key by landing
    the in-hand bytes at the pinned key directly — concurrent runs converge
    last-writer-wins instead of aborting (audit track_balls.py:326).

Pitch mapping:
    The session's era-consistent extrinsic calibration (US-C2/C3 resolution
    rule, private local copy — importing API services from the worker is
    forbidden) enriches points with pitch coordinates when present; otherwise
    the camera is reported pixel-only (``pitch_mapped=false`` in flags).
    Track points keep raw pixels either way; intrinsic undistortion of track
    points is deferred (the manual bounce-click path stays the precise one).

Loud, never silent:
    Cameras with unusable fps/resolution metadata, pixel-only cameras,
    below-target coverage (US-F3 AC: >= 90%) and unusable event windows are
    all reported in the summary; rows and points are never fabricated.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from cricai_data.db import session_scope
from cricai_data.enums import EVIDENCE_STATUSES, CalibrationKind, LabelClass
from cricai_data.models import BallEvent, BallTrack, Calibration, CameraConfig, Video
from cricai_data.models import Session as SessionRow
from cricai_data.storage import ObjectStore, StorageError
from cricai_vision import extrinsics
from cricai_vision.detect import DetectionError, DetectionProvider, FakeDetectionProvider
from cricai_vision.track import (
    TRACKER_VERSION,
    TrackerConfig,
    TrackError,
    TrackingWindow,
    track_ball,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext

#: US-F3 AC: the track should cover >= 90% of the ball window; below is flagged.
MIN_COVERAGE = 0.9

_RESOLUTION_RE = re.compile(r"^(\d+)x(\d+)$")


def track_key(session_id: uuid.UUID, ball_no: int, camera_id: str) -> str:
    """Pinned object-store key of a per-ball track payload (US-F3 contract)."""
    if ball_no < 1:
        raise ValueError(f"ball_no must be >= 1, got {ball_no}")
    return f"sessions/{session_id}/balls/{ball_no}/track-{camera_id}.json"


@dataclass(frozen=True)
class TrackRunSummary:
    """What one run did: tracks written plus every loudly-reported gap."""

    session_id: str
    tracked: int
    skipped_cameras: tuple[tuple[str, str], ...]  # (camera_id, reason): unusable metadata
    pixel_only_cameras: tuple[tuple[str, str], ...]  # (camera_id, reason): no pitch mapping
    low_coverage: tuple[tuple[int, str], ...]  # (ball_no, camera_id) below MIN_COVERAGE
    failed: tuple[tuple[int, str, str], ...]  # (ball_no, camera_id, reason): unusable window


def _evidence_videos(db: Session, session_id: uuid.UUID) -> dict[str, Video]:
    """Camera id -> its newest evidence-status Video (usable footage exists)."""
    rows = db.scalars(
        select(Video)
        .where(Video.session_id == session_id, Video.status.in_(sorted(EVIDENCE_STATUSES)))
        .order_by(Video.created_at, Video.id)
    )
    return {video.camera_id: video for video in rows}  # newest row wins


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _frame_size(probe: dict[str, Any], claimed_resolution: str | None) -> tuple[int, int] | None:
    """Pixel (width, height): the measured probe wins over the client claim."""
    width = _numeric(probe.get("width"))
    height = _numeric(probe.get("height"))
    if width is not None and height is not None and width >= 1 and height >= 1:
        return (int(width), int(height))
    if claimed_resolution is not None:
        match = _RESOLUTION_RE.fullmatch(claimed_resolution)
        if match is not None:
            claimed_w, claimed_h = int(match.group(1)), int(match.group(2))
            if claimed_w >= 1 and claimed_h >= 1:
                return (claimed_w, claimed_h)
    return None


def _camera_metadata(video: Video) -> tuple[float, int, int] | str:
    """Best-known (fps, width, height), or the reason the camera is unusable."""
    probe = video.probe or {}
    fps = _numeric(probe.get("fps"))
    if fps is None:
        fps = _numeric(video.claimed_fps)
    if fps is None or fps <= 0:
        return f"no usable fps for camera {video.camera_id} (neither probed nor claimed)"
    size = _frame_size(probe, video.claimed_resolution)
    if size is None:
        return f"no usable resolution for camera {video.camera_id} (neither probed nor claimed)"
    return (fps, size[0], size[1])


def _linked_calibration(db: Session, session: SessionRow, camera_id: str) -> Calibration | None:
    """The session-attached calibration when usable for this camera (US-C3)."""
    if session.calibration_id is None:
        return None
    record = db.get(Calibration, session.calibration_id)
    if record is not None and record.valid and record.camera_id == camera_id:
        return record
    return None


def _era_calibration(db: Session, camera_id: str) -> Calibration | None:
    """Newest valid extrinsic calibration of the camera's active placement era."""
    era_no = db.scalar(
        select(CameraConfig.era_no)
        .where(CameraConfig.camera_id == camera_id, CameraConfig.active.is_(True))
        .order_by(CameraConfig.era_no.desc())
    )
    if era_no is None:
        return None
    return db.scalars(
        select(Calibration)
        .where(
            Calibration.camera_id == camera_id,
            Calibration.era_no == int(era_no),
            Calibration.kind == CalibrationKind.EXTRINSIC,
            Calibration.valid.is_(True),
        )
        .order_by(Calibration.created_at.desc())
    ).first()


def _camera_calibration(
    db: Session, session: SessionRow, camera_id: str
) -> extrinsics.PlaneCalibration | str:
    """The pixel->pitch homography for enrichment, or the pixel-only reason.

    Mirrors the API bounce-mapping resolution rule (session-linked record wins
    when valid and same-camera, else the newest valid extrinsic of the current
    active era) as a private local copy — the worker must not import API
    services. Unusable stored params degrade to pixel-only, loudly.
    """
    record = _linked_calibration(db, session, camera_id)
    if record is None:
        record = _era_calibration(db, camera_id)
    if record is None:
        return f"no valid extrinsic calibration for camera {camera_id}"
    try:
        return extrinsics.from_params(record.params)
    except extrinsics.ExtrinsicsError as exc:
        return f"stored calibration for camera {camera_id} is unusable: {exc}"


def _track_row(db: Session, session_id: uuid.UUID, ball_no: int, camera_id: str) -> BallTrack:
    """Existing (session_id, ball_no, camera_id) ball_tracks row, or a fresh one."""
    row = db.execute(
        select(BallTrack).where(
            BallTrack.session_id == session_id,
            BallTrack.ball_no == ball_no,
            BallTrack.camera_id == camera_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = BallTrack(session_id=session_id, ball_no=ball_no, camera_id=camera_id)
        db.add(row)
    return row


@dataclass
class _Run:
    """Shared per-run state threaded through the camera/ball loops."""

    ctx: WorkerContext
    db: Session
    session_id: uuid.UUID
    detector: DetectionProvider
    config: TrackerConfig
    tracked: int = 0

    def __post_init__(self) -> None:
        self.skipped: list[tuple[str, str]] = []
        self.pixel_only: list[tuple[str, str]] = []
        self.low_coverage: list[tuple[int, str]] = []
        self.failed: list[tuple[int, str, str]] = []


def track_session_balls(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    provider: DetectionProvider | None = None,
    config: TrackerConfig | None = None,
) -> TrackRunSummary:
    """Track every (valid ball event x evidence camera) for one session."""
    detector = provider if provider is not None else FakeDetectionProvider()
    with session_scope(ctx.session_factory) as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        run = _Run(
            ctx=ctx,
            db=db,
            session_id=session_id,
            detector=detector,
            config=config if config is not None else TrackerConfig(),
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
        videos = _evidence_videos(db, session_id)
        for camera_id in sorted(videos):
            resolved = _camera_metadata(videos[camera_id])
            if isinstance(resolved, str):
                run.skipped.append((camera_id, resolved))
                continue
            resolved_calibration = _camera_calibration(db, session, camera_id)
            if isinstance(resolved_calibration, str):
                run.pixel_only.append((camera_id, resolved_calibration))
                calibration = None
            else:
                calibration = resolved_calibration
            for event in events:
                _track_one(run, event, camera_id, resolved, calibration)
    return TrackRunSummary(
        session_id=str(session_id),
        tracked=run.tracked,
        skipped_cameras=tuple(run.skipped),
        pixel_only_cameras=tuple(run.pixel_only),
        low_coverage=tuple(run.low_coverage),
        failed=tuple(run.failed),
    )


def _track_one(
    run: _Run,
    event: BallEvent,
    camera_id: str,
    metadata: tuple[float, int, int],
    calibration: extrinsics.PlaneCalibration | None,
) -> None:
    """Detect + track one ball on one camera; the row always mirrors its bytes."""
    fps, width, height = metadata
    try:
        window = TrackingWindow(
            start_ms=float(event.start_ms),
            end_ms=float(event.end_ms),
            fps=fps,
            width=width,
            height=height,
        )
        detections = run.detector.detect(start_ms=window.start_ms, end_ms=window.end_ms, fps=fps)
        balls = [d for d in detections if d.label is LabelClass.BALL]
        result = track_ball(balls, window, calibration=calibration, config=run.config)
    except (TrackError, DetectionError) as exc:
        # An unusable event window is recorded loudly and never dead-ends the run.
        run.failed.append((event.ball_no, camera_id, str(exc)))
        return
    key = track_key(run.session_id, event.ball_no, camera_id)
    # Two-phase write: overwriting the pinned key before the row commits would
    # leave a crash window serving new bytes under the stale committed row, so
    # the row is first committed against a per-run staging copy (review
    # track_balls.py:296). A crash at any step leaves a consistent pair.
    staging_key = f"{key}.staging-{uuid.uuid4().hex}"
    payload = json.dumps(result.to_payload()).encode("utf-8")
    run.ctx.store.put(staging_key, payload)
    row = _track_row(run.db, run.session_id, event.ball_no, camera_id)
    row.tracker_version = TRACKER_VERSION
    row.points_key = staging_key
    row.coverage = result.coverage
    row.segments = result.segments_payload()
    row.flags = dict(result.flags)
    row.confidence = result.confidence
    run.db.commit()  # the row now describes the staging bytes; pinned bytes intact
    try:
        run.ctx.store.copy(staging_key, key)  # atomic replace of the pinned object
    except StorageError:
        # A parallel run's namespace-wide sweep deleted this run's staging
        # object after promoting its own payload (audit track_balls.py:326).
        # The bytes are still in hand: land them at the pinned key directly so
        # the committed row never stays pointing at the swept staging key and
        # the sweep never aborts the rest of the run. Runs converge last-writer
        # -wins on a consistent (pinned bytes, row) pair either way.
        run.ctx.store.put(key, payload)
    row.points_key = key
    run.db.commit()  # same bytes now live at the pinned key; the row follows
    _sweep_staging(run.ctx.store, key)
    run.tracked += 1
    if result.coverage < MIN_COVERAGE:
        run.low_coverage.append((event.ball_no, camera_id))


def _sweep_staging(store: ObjectStore, key: str) -> None:
    """Drop this track's staging objects (the current run's and crashed runs')."""
    for stale in store.list_keys(key.rsplit("/", 1)[0]):
        if stale.startswith(f"{key}.staging-"):
            store.delete(stale)
