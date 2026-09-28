"""Automatic bounce estimation job (US-F4): ball tracks -> ``bounce_estimates``.

For every tracked ball of a session (``ball_tracks`` rows, US-F3), the pinned
track payload is read from the object store, the bounce vertex is located by
:func:`cricai_vision.bounce_estimate.estimate_bounce`, mapped to pitch
coordinates through the camera's homography, classified into line/length zones
via :mod:`cricai_vision.zones` — the same config vocabulary the manual bounce
router uses (default :class:`~cricai_vision.zones.ZoneConfig`, left-handed
BATTERS mirrored by the classifier; a BOWLING session's zones use the fixed
canonical right-hand frame that US-I4 target scoring pins, never the bowler's
own batting handedness; an off-pitch vertex keeps NULL zone classes rather
than failing the ball) — and upserted into ``bounce_estimates`` on the
``(session_id, ball_no)`` unique pair.

Precedence & safety (pinned Phase-4 contract):
    This job NEVER reads, writes, updates or deletes ``bounce_marks``. The
    manual mark always wins at read time via the API's shared precedence
    resolver; re-running this job therefore preserves manual overrides
    untouched (US-F4 AC).

Camera choice & calibration:
    A ball tracked from several cameras uses the lowest camera_id that yields
    an estimate (mirroring the heatmap's representative-camera rule), falling
    back to the next camera when a payload is missing/malformed or the camera
    has no usable calibration. The default resolver mirrors the manual click
    path WITHOUT importing the API: the session-attached extrinsic calibration
    wins (US-C3 era consistency), else the newest valid extrinsic of the
    camera's active era; track pixels are undistorted through the era's newest
    valid intrinsic calibration when one exists (US-C1).

Idempotency & crash-safety:
    One commit per ball (the extract_pose pattern): a mid-run crash never
    strands partial work, and a re-run is a plain re-run. A stale auto row is
    deleted only on an authoritative no-bounce: EVERY camera's inputs were
    readable and none yielded an estimate (full toss / tracking failure).
    When any camera's inputs are unavailable (missing payload, no calibration,
    an unmappable vertex) existing rows are left alone; transient
    infrastructure gaps never destroy previously computed estimates. Rejected
    events are not balls (US-D4): their tracks are ignored and their stale
    estimate rows are removed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from cricai_data.db import session_scope
from cricai_data.enums import CalibrationKind, Handedness, Length, Line, SessionType
from cricai_data.models import BallEvent, BallTrack, BounceEstimate, Calibration, CameraConfig
from cricai_data.models import Session as SessionRow
from cricai_data.storage import ObjectStore, StorageError
from cricai_vision import extrinsics, intrinsics
from cricai_vision.bounce_estimate import (
    ESTIMATOR_VERSION,
    EstimatedBounce,
    TrackPayload,
    TrackPayloadError,
    estimate_bounce,
    parse_track_payload,
)
from cricai_vision.zones import ZoneConfig, ZoneConfigError, classify
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext


@dataclass(frozen=True)
class CameraMapping:
    """Pixel->pitch mapping inputs for one camera (US-C1/C2 seam)."""

    homography: extrinsics.PlaneCalibration
    intrinsic_params: dict[str, Any] | None = None


#: Resolves the mapping for (db, session, camera_id); ``None`` = not calibrated.
MappingResolver = Callable[[Session, SessionRow, str], "CameraMapping | None"]


@dataclass(frozen=True)
class BounceEstimationSummary:
    """What one run did: rows upserted, stale rows removed, balls skipped loudly."""

    session_id: str
    estimated: int
    removed: int
    skipped: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class _Estimated:
    """A usable vertex from the representative camera, with its provenance."""

    estimate: EstimatedBounce
    tracker_version: str


@dataclass(frozen=True)
class _NoBounce:
    """Every camera's track was read fine and none yields a bounce (authoritative)."""

    reason: str


@dataclass(frozen=True)
class _Unavailable:
    """Inputs could not be read/resolved; existing rows must be preserved."""

    reason: str


def _newest_valid(
    db: Session, camera_id: str, era_no: int, kind: CalibrationKind
) -> Calibration | None:
    return db.scalars(
        select(Calibration)
        .where(
            Calibration.camera_id == camera_id,
            Calibration.era_no == era_no,
            Calibration.kind == kind,
            Calibration.valid.is_(True),
        )
        .order_by(Calibration.created_at.desc())
    ).first()


def _session_calibration(db: Session, session: SessionRow, camera_id: str) -> Calibration | None:
    """The session-attached extrinsic calibration, when usable for this camera
    (US-C3 era consistency — mirrors the manual click path)."""
    if session.calibration_id is None:
        return None
    record = db.get(Calibration, session.calibration_id)
    if (
        record is not None
        and record.valid
        and record.camera_id == camera_id
        and record.kind is CalibrationKind.EXTRINSIC
    ):
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
    return _newest_valid(db, camera_id, int(era_no), CalibrationKind.EXTRINSIC)


def camera_mapping(db: Session, session: SessionRow, camera_id: str) -> CameraMapping | None:
    """Default :data:`MappingResolver` over the stored calibration records.

    ``None`` (skip the camera loudly, never mis-map) when no usable extrinsic
    exists; the same-era intrinsic rides along for undistortion when present.
    """
    record = _session_calibration(db, session, camera_id)
    if record is None:
        record = _era_calibration(db, camera_id)
    if record is None:
        return None
    try:
        homography = extrinsics.from_params(record.params)
    except extrinsics.ExtrinsicsError:
        return None  # stored params unusable: treated as not calibrated
    intrinsic = _newest_valid(db, camera_id, record.era_no, CalibrationKind.INTRINSIC)
    return CameraMapping(
        homography=homography,
        intrinsic_params=intrinsic.params if intrinsic is not None else None,
    )


def _undistorted(payload: TrackPayload, params: dict[str, Any]) -> TrackPayload:
    """Undistort every track point pixel (mirrors the manual click path, US-C1)."""
    pxs = intrinsics.undistort_pixels(
        params, [(point.px_x, point.px_y) for point in payload.points]
    )
    points = tuple(
        replace(point, px_x=px[0], px_y=px[1])
        for point, px in zip(payload.points, pxs, strict=True)
    )
    return replace(payload, points=points)


def _valid_event_balls(db: Session, session_id: uuid.UUID) -> set[int]:
    """Balls that exist per US-D4: rejected events are not balls."""
    return set(
        db.scalars(
            select(BallEvent.ball_no).where(
                BallEvent.session_id == session_id, BallEvent.valid.is_(True)
            )
        )
    )


def _tracks_by_ball(
    db: Session, session_id: uuid.UUID, valid_balls: set[int]
) -> dict[int, list[BallTrack]]:
    grouped: dict[int, list[BallTrack]] = {}
    for row in db.scalars(
        select(BallTrack)
        .where(BallTrack.session_id == session_id)
        .order_by(BallTrack.ball_no, BallTrack.camera_id)
    ):
        if row.ball_no in valid_balls:
            grouped.setdefault(row.ball_no, []).append(row)
    return grouped


def _load_payload(store: ObjectStore, track: BallTrack) -> TrackPayload | str:
    """The parsed payload for one track row, or the per-camera skip reason."""
    try:
        return parse_track_payload(json.loads(store.get(track.points_key)))
    except StorageError:
        return f"{track.camera_id}: track payload missing from the object store"
    except (json.JSONDecodeError, TrackPayloadError) as exc:
        return f"{track.camera_id}: malformed track payload ({exc})"


def _estimate_ball(
    db: Session,
    store: ObjectStore,
    session: SessionRow,
    tracks: list[BallTrack],
    resolver: MappingResolver,
) -> _Estimated | _NoBounce | _Unavailable:
    """First camera (ascending id) that yields a bounce estimate wins.

    A per-camera no-bounce verdict is evidence about that camera's track, not
    about the ball: the outcome is :class:`_NoBounce` (stale rows may be
    deleted) only when EVERY camera's inputs were readable and none yielded an
    estimate. Any unreadable/unresolvable input — missing or malformed
    payload, no calibration, an unmappable vertex — makes the ball
    :class:`_Unavailable` so previously computed rows are preserved.
    """
    reasons: list[str] = []
    unavailable = False
    no_bounce: _NoBounce | None = None
    for track in tracks:
        mapping = resolver(db, session, track.camera_id)
        if mapping is None:
            reasons.append(f"{track.camera_id}: no usable extrinsic calibration")
            unavailable = True
            continue
        payload = _load_payload(store, track)
        if isinstance(payload, str):
            reasons.append(payload)
            unavailable = True
            continue
        if mapping.intrinsic_params is not None:
            try:
                payload = _undistorted(payload, mapping.intrinsic_params)
            except ValueError as exc:
                reasons.append(f"{track.camera_id}: unusable intrinsic calibration ({exc})")
                unavailable = True
                continue
        result = estimate_bounce(payload)  # pixel-space: the track's own verdict
        if result.estimate is None:
            reason = f"{track.camera_id}: {cast(str, result.reason)}"
            reasons.append(reason)
            if no_bounce is None:  # lowest camera is the representative
                no_bounce = _NoBounce(reason)
            continue
        try:
            pitch_x, pitch_y = extrinsics.pixel_to_pitch_xy(
                mapping.homography, (result.estimate.px_x, result.estimate.px_y)
            )
        except extrinsics.ExtrinsicsError as exc:
            # A calibration artifact, not a ball truth: preserve existing rows.
            reasons.append(
                f"{track.camera_id}: bounce pixel is unmappable through the homography ({exc})"
            )
            unavailable = True
            continue
        estimate = replace(result.estimate, pitch_x=pitch_x, pitch_y=pitch_y)
        return _Estimated(estimate, f"{track.tracker_version}+{ESTIMATOR_VERSION}")
    if no_bounce is not None and not unavailable:
        return no_bounce  # every camera was readable and none saw a bounce
    return _Unavailable("; ".join(reasons))


def _apply(
    row: BounceEstimate, estimated: _Estimated, config: ZoneConfig, handedness: Handedness
) -> None:
    """Write one estimate onto its (new or reused) ``bounce_estimates`` row."""
    # The job always supplies a homography, so the vertex is always mapped.
    pitch_x = cast(float, estimated.estimate.pitch_x)
    pitch_y = cast(float, estimated.estimate.pitch_y)
    line: Line | None
    length: Length | None
    try:
        line, length = classify(config, pitch_x, pitch_y, handedness)
    except ZoneConfigError:
        # Off-pitch vertex: NULL zone classes (the US-C5 vocabulary), never a crash.
        line = None
        length = None
    row.pitch_x = pitch_x
    row.pitch_y = pitch_y
    row.line = line
    row.length = length
    row.confidence = estimated.estimate.confidence
    row.tracker_version = estimated.tracker_version


def estimate_session_bounces(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    mapping_resolver: MappingResolver = camera_mapping,
    zone_config: ZoneConfig | None = None,
) -> BounceEstimationSummary:
    """Estimate the bounce of every tracked ball in one session (US-F4).

    ``zone_config`` defaults to the same :class:`ZoneConfig` the manual bounce
    router classifies with, so auto and manual zone classes share one
    vocabulary. Never touches ``bounce_marks``; see the module docstring for
    the camera-choice, idempotency and stale-row rules.
    """
    config = zone_config if zone_config is not None else ZoneConfig()
    estimated = 0
    removed = 0
    skipped: list[tuple[int, str]] = []
    with session_scope(ctx.session_factory) as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        # Line channels are batter-relative. In a batting session the player IS
        # the batter, so their handedness frames the zones (US-C5). In a
        # BOWLING session the player is the bowler and their batting
        # handedness is irrelevant to where a delivery pitched: the cached
        # zones use the fixed canonical right-hand frame — the SAME frame
        # US-I4 target scoring pins (trajectory.score_delivery defaults
        # Handedness.RIGHT) — so the pitch map can never mirror against the
        # accuracy scorecard for a left-handed kid's bowling session.
        handedness = (
            Handedness.RIGHT
            if session.session_type is SessionType.BOWLING
            else session.player.handedness
        )
        existing = {
            row.ball_no: row
            for row in db.scalars(
                select(BounceEstimate).where(BounceEstimate.session_id == session_id)
            )
        }
        valid_balls = _valid_event_balls(db, session_id)
        for ball_no in sorted(set(existing) - valid_balls):
            # Rejected events are not balls (US-D4): drop their phantom rows.
            db.delete(existing.pop(ball_no))
            removed += 1
        for ball_no, tracks in _tracks_by_ball(db, session_id, valid_balls).items():
            outcome = _estimate_ball(db, ctx.store, session, tracks, mapping_resolver)
            if isinstance(outcome, _Estimated):
                # Idempotent upsert on the (session_id, ball_no) unique pair.
                row = existing.get(ball_no)
                if row is None:
                    row = BounceEstimate(session_id=session_id, ball_no=ball_no)
                    db.add(row)
                    existing[ball_no] = row
                _apply(row, outcome, config, handedness)
                estimated += 1
            else:
                skipped.append((ball_no, outcome.reason))
                if isinstance(outcome, _NoBounce) and ball_no in existing:
                    # The re-read track says no bounce: the stale auto row lies.
                    db.delete(existing.pop(ball_no))
                    removed += 1
            db.commit()  # per-ball durability: crash-resume is a plain re-run
    return BounceEstimationSummary(
        session_id=str(session_id),
        estimated=estimated,
        removed=removed,
        skipped=tuple(skipped),
    )
