"""Session-aware pixel->pitch bounce mapping (US-C1/C2/C5 seam).

One place owns how a raw bounce click becomes pitch coordinates so the bounce
router and the calibration verify/backfill flow can never disagree:

* Calibration resolution is era-consistent: a session explicitly linked to a
  valid extrinsic calibration of the clicked camera (US-C3) maps through that
  record even after the camera opens a new placement era; otherwise the newest
  valid extrinsic calibration of the camera's current active era is used, and
  having neither raises :class:`MappingUnavailableError` (callers map to 422 -
  "calibrate first" - never 500).
* Clicks are undistorted through the newest valid intrinsic calibration of the
  same camera era before the homography (US-C1), when one exists.
* Every stored/derived mapping error (:class:`~cricai_vision.extrinsics.
  ExtrinsicsError`, horizon w ~ 0, malformed intrinsics) is wrapped in
  :class:`MappingUnavailableError` so it can never escape as a 500.
"""

from __future__ import annotations

import itertools
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from cricai_data.enums import CalibrationKind
from cricai_data.models import BounceMark, Calibration, CameraConfig, Session
from cricai_vision import extrinsics, intrinsics
from cricai_vision.zones import ZoneConfig, classify
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

#: Same-ball marks from different cameras must agree within this pitch-plane
#: distance (US-C5 initial target: 15 cm) or the ball is flagged for review.
CROSS_CAMERA_AGREEMENT_M = 0.15


@dataclass(frozen=True)
class MappedPoint:
    """A click mapped to pitch coordinates plus its mapping provenance."""

    pitch_x: float
    pitch_y: float
    calibration_id: uuid.UUID
    intrinsics_applied: bool


@dataclass(frozen=True)
class AgreementResult:
    """Cross-camera agreement for a ball, from the saved mark's perspective."""

    other_camera: str
    distance_m: float


class MappingUnavailableError(Exception):
    """No usable calibration for one or more cameras - callers map to 422."""

    def __init__(self, cameras: Sequence[str], message: str | None = None) -> None:
        self.cameras = list(cameras)
        if message is None:
            joined = ", ".join(self.cameras)
            message = f"no valid extrinsic calibration for cameras {joined}; calibrate first"
        super().__init__(message)


def _newest_valid(
    db: OrmSession, camera_id: str, era_no: int, kind: CalibrationKind
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


def _session_calibration(
    db: OrmSession, session: Session | None, camera_id: str
) -> Calibration | None:
    """The session-attached calibration, when it is usable for this camera.

    US-C3 era-consistency: a session explicitly linked to a calibration keeps
    mapping through it even after the camera opens a new placement era. The
    link is ignored (falling back to the current era) when the record is gone,
    invalidated (US-C4: camera moved), or belongs to a different camera.
    """
    if session is None or session.calibration_id is None:
        return None
    record = db.get(Calibration, session.calibration_id)
    if record is not None and record.valid and record.camera_id == camera_id:
        return record
    return None


def _current_era_calibration(db: OrmSession, camera_id: str) -> Calibration:
    """Newest valid extrinsic calibration for the camera's active placement era."""
    era_no = db.scalar(
        select(CameraConfig.era_no)
        .where(CameraConfig.camera_id == camera_id, CameraConfig.active.is_(True))
        .order_by(CameraConfig.era_no.desc())
    )
    if era_no is None:
        raise MappingUnavailableError(
            [camera_id],
            f"camera {camera_id} has no active registration; register it and calibrate first",
        )
    record = _newest_valid(db, camera_id, int(era_no), CalibrationKind.EXTRINSIC)
    if record is None:
        raise MappingUnavailableError(
            [camera_id],
            f"no valid extrinsic calibration for camera {camera_id} (era {era_no}); "
            "calibrate first",
        )
    return record


def map_click(
    db: OrmSession,
    camera_id: str,
    px: tuple[float, float],
    *,
    session: Session | None = None,
) -> MappedPoint:
    """Map a raw click pixel to pitch coordinates through the right calibration.

    Resolution rule: the session-attached calibration wins when it is valid and
    belongs to ``camera_id``; otherwise the newest valid extrinsic calibration
    of the camera's current active era. The pixel is undistorted through the
    newest valid intrinsic calibration of the same (camera, era) when one
    exists (US-C1). Raises :class:`MappingUnavailableError` when no usable
    calibration exists or any stored calibration fails to apply.
    """
    record = _session_calibration(db, session, camera_id)
    if record is None:
        record = _current_era_calibration(db, camera_id)
    intrinsic = _newest_valid(db, camera_id, record.era_no, CalibrationKind.INTRINSIC)
    intrinsics_applied = False
    if intrinsic is not None:
        try:
            px = intrinsics.undistort_pixel(intrinsic.params, px)
        except ValueError as exc:
            raise MappingUnavailableError(
                [camera_id],
                f"stored intrinsic calibration for camera {camera_id} is unusable "
                f"({exc}); recalibrate first",
            ) from exc
        intrinsics_applied = True
    try:
        plane = extrinsics.from_params(record.params)
        pitch_x, pitch_y = extrinsics.pixel_to_pitch_xy(plane, px)
    except extrinsics.ExtrinsicsError as exc:
        raise MappingUnavailableError(
            [camera_id],
            f"stored extrinsic calibration for camera {camera_id} is unusable "
            f"({exc}); recalibrate first",
        ) from exc
    return MappedPoint(
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        calibration_id=record.id,
        intrinsics_applied=intrinsics_applied,
    )


def _distance_m(a: BounceMark, b: BounceMark) -> float:
    return math.hypot(a.pitch_x - b.pitch_x, a.pitch_y - b.pitch_y)


def reevaluate_agreement(
    db: OrmSession,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    perspective_camera: str | None = None,
) -> AgreementResult | None:
    """Recompute the cross-camera review flag for one ball (US-C5).

    Any camera pair for this ball disagreeing by more than
    :data:`CROSS_CAMERA_AGREEMENT_M` flags ALL the ball's marks; agreement
    within the threshold clears them (a corrected re-click un-flags the ball).
    The returned result is oriented from ``perspective_camera`` (the mark just
    saved) toward its farthest other-camera mark; without a perspective the
    most recently created mark is used. ``None`` when the ball has no marks
    from more than one camera.
    """
    marks = db.scalars(
        select(BounceMark).where(BounceMark.session_id == session_id, BounceMark.ball_no == ball_no)
    ).all()
    if not marks:
        return None
    by_camera = {mark.camera_id: mark for mark in marks}
    if perspective_camera is None:
        saved = max(marks, key=lambda mark: (mark.created_at, mark.camera_id))
    else:
        saved = by_camera[perspective_camera]
    others = [mark for mark in marks if mark.camera_id != saved.camera_id]
    if not others:
        return None
    worst = max(_distance_m(a, b) for a, b in itertools.combinations(marks, 2))
    flagged = worst > CROSS_CAMERA_AGREEMENT_M
    for mark in marks:
        mark.flagged_for_review = flagged
    other = max(others, key=lambda mark: _distance_m(saved, mark))
    return AgreementResult(other_camera=other.camera_id, distance_m=_distance_m(saved, other))


def recompute_session_marks(db: OrmSession, session: Session) -> int:
    """Remap every bounce mark of ``session`` from its stored raw pixel.

    Used by the calibration verify/backfill flow: each mark is remapped via
    :func:`map_click` (session-aware resolution), its line/length re-derived
    with the default :class:`~cricai_vision.zones.ZoneConfig`, its pitch xy and
    ``calibration_id`` updated, and cross-camera agreement re-evaluated per
    ball. Cameras lacking a usable calibration abort the whole recompute with
    :class:`MappingUnavailableError` listing them sorted (no partial updates);
    a remapped point that can no longer be classified raises
    ``ZoneConfigError`` (a ``ValueError`` - callers map both to 422). Returns
    the number of marks updated.
    """
    marks = db.scalars(
        select(BounceMark)
        .where(BounceMark.session_id == session.id)
        .order_by(BounceMark.ball_no, BounceMark.camera_id)
    ).all()
    mapped: dict[uuid.UUID, MappedPoint] = {}
    unavailable: set[str] = set()
    for mark in marks:
        try:
            mapped[mark.id] = map_click(db, mark.camera_id, (mark.px_x, mark.px_y), session=session)
        except MappingUnavailableError as exc:
            unavailable.update(exc.cameras)
    if unavailable:
        raise MappingUnavailableError(sorted(unavailable))
    config = ZoneConfig()
    handedness = session.player.handedness
    for mark in marks:
        point = mapped[mark.id]
        mark.pitch_x = point.pitch_x
        mark.pitch_y = point.pitch_y
        mark.calibration_id = point.calibration_id
        mark.line, mark.length = classify(config, point.pitch_x, point.pitch_y, handedness)
    for ball_no in sorted({mark.ball_no for mark in marks}):
        reevaluate_agreement(db, session.id, ball_no)
    db.flush()
    return len(marks)
