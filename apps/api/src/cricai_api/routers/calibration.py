"""US-C1/C3/C4: calibration records, session linkage and drift detection.

Calibration records are per camera placement era (US-C2/C4): the era is
resolved from the active camera registry row at creation time, so moving a
camera (which opens a new era) naturally orphans old calibrations. Extrinsic
records are fitted server-side from operator landmark clicks resolved against
:func:`cricai_vision.geometry.landmark_catalog`; intrinsic records (owned by
US-C1 tooling) are stored after a minimal structural check only.

Intrinsics traceability (US-C1): when an extrinsic record is fitted while a
valid intrinsic record exists for the same (camera, active era), the landmark
click pixels are undistorted through it first and the intrinsic record's id
is stored as ``params["intrinsics_id"]`` inside the extrinsic params. A
session therefore references the intrinsics version used via the chain
``Session.calibration_id -> Calibration.params["intrinsics_id"]``
(``extrinsics.from_params`` tolerates the extra key).

The US-C3 analyzed-gate lives at lifecycle level, not here: the Phase 3
worker transitioning a session into ANALYZED must call
``cricai_data.lifecycle.ensure_transition(current, SessionState.ANALYZED,
has_calibration=session.calibration_id is not None)`` — ``False`` (and the
``None`` default: the gate is not opt-in) raises ``SessionLifecycleError``,
which API surfaces map to 409 (see ``routers/lifecycle.py`` for the
established pattern).

Drift flow (US-C4): a drift-check that trips the threshold sets
``Session.calibration_suspect`` and audit-logs it. The flag is deliberately
sticky — a later clean check never auto-clears it; a Parent must explicitly
POST ``/sessions/{id}/calibration/verify`` with an action of ``accept``
(keep the flagged data, audited) or ``recalibrated`` (remap every stored
bounce mark through the fresh calibration, then clear), so suspect data can
never silently rejoin trend queries.
"""

import math
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from cricai_data.enums import CalibrationKind, Role
from cricai_data.models import AuditLog, Calibration, CameraConfig, Session
from cricai_vision import extrinsics, intrinsics, triangulate
from cricai_vision.drift import DEFAULT_DRIFT_THRESHOLD_PX, is_drifted, landmark_shift_stats
from cricai_vision.geometry import landmark_catalog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.services import bounce_mapping

router = APIRouter(tags=["calibration"])

CAMERA_ID_PATTERN = r"^C[1-8]$"
MIN_EXTRINSIC_LANDMARKS = 6
#: Held-out landmark pairs used to measure honest mapping error (US-C2).
EXTRINSIC_HOLDOUT = 2
#: Per-request drift threshold bounds — the default is the US-C4 initial target.
DRIFT_THRESHOLD_MIN_PX = 1.0
DRIFT_THRESHOLD_MAX_PX = 50.0


class LandmarkClick(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    px: tuple[float, float]


class CalibrationIn(BaseModel):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN)
    kind: CalibrationKind
    params: dict[str, Any] | None = None
    landmarks: list[LandmarkClick] | None = None


class CalibrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    camera_id: str
    era_no: int
    kind: CalibrationKind
    params: dict[str, Any]
    rms: float | None
    valid: bool
    created_at: datetime


class CalibrationCreateOut(CalibrationOut):
    """Creation response (US-C1/C2): also states which intrinsic record (if
    any) undistorted the clicks, and whether the reported RMS was measured
    in-sample (adaptive holdout of 0 at the minimum click count) rather than
    on held-out landmarks."""

    intrinsics_id: uuid.UUID | None
    rms_in_sample: bool


class CalibrationVerifyIn(BaseModel):
    """US-C4 resolution: ``accept`` keeps the flagged data (audited);
    ``recalibrated`` backfills every stored bounce mark through the fresh
    calibration before clearing the flag."""

    action: Literal["accept", "recalibrated"]


class CalibrationAttachIn(BaseModel):
    """Exactly one of ``calibration_id`` (explicit record) or
    ``reuse_latest_for_camera`` (US-C3: explicitly reuse the newest valid
    extrinsic calibration of the camera's active era)."""

    calibration_id: uuid.UUID | None = None
    reuse_latest_for_camera: str | None = Field(default=None, pattern=CAMERA_ID_PATTERN)


class DriftCheckIn(BaseModel):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN)
    observed_landmarks: list[LandmarkClick] = Field(min_length=1)
    threshold_px: float = Field(
        default=DEFAULT_DRIFT_THRESHOLD_PX, ge=DRIFT_THRESHOLD_MIN_PX, le=DRIFT_THRESHOLD_MAX_PX
    )


class DriftCheckOut(BaseModel):
    median_px: float
    max_px: float
    per_landmark: dict[str, float]
    drifted: bool
    threshold_px: float


class SuspectStateOut(BaseModel):
    session_id: uuid.UUID
    calibration_suspect: bool


def _get_session_or_404(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _active_era_or_422(db: OrmSession, camera_id: str) -> int:
    era_no = db.scalar(
        select(CameraConfig.era_no).where(
            CameraConfig.camera_id == camera_id, CameraConfig.active.is_(True)
        )
    )
    if era_no is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"camera {camera_id} is not registered as active in the camera registry",
        )
    return int(era_no)


def _latest_valid(
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


def _is_3x3_numeric(matrix: object) -> bool:
    if not isinstance(matrix, list) or len(matrix) != 3:
        return False
    for row in matrix:
        if not isinstance(row, list) or len(row) != 3:
            return False
        for value in row:
            # NaN/Infinity serialize to null in JSON and poison later reads.
            if isinstance(value, bool) or not isinstance(value, int | float):
                return False
            if not math.isfinite(value):
                return False
    return True


def _validate_intrinsic_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Minimal structural check only — the US-C1 intrinsics module owns semantics."""
    if params is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "intrinsic calibration requires params"
        )
    if params.get("version") != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "intrinsic params must declare version 1"
        )
    if not _is_3x3_numeric(params.get("camera_matrix")):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "intrinsic params must include a 3x3 numeric camera_matrix",
        )
    return params


def _validate_stereo_params(
    camera_id: str, params: dict[str, Any] | None, landmarks: list[LandmarkClick] | None
) -> dict[str, Any]:
    """US-F6 row contract: ``params`` is exactly the ``triangulate.to_params``
    payload, stored under the pair's first camera id (see the
    ``cricai_vision.triangulate`` module docstring)."""
    if landmarks is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "stereo calibration takes a triangulate.to_params params payload, not"
            " landmark clicks; landmark fits are the extrinsic flow",
        )
    if params is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "stereo calibration requires params"
        )
    try:
        pair = triangulate.from_params(params)
    except triangulate.TriangulationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if pair.camera_a != camera_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"stereo records are stored under the pair's first camera: camera_pair"
            f" [{pair.camera_a!r}, {pair.camera_b!r}] requires camera_id"
            f" {pair.camera_a!r}, got {camera_id!r}",
        )
    return params


def _fit_extrinsic(
    landmarks: list[LandmarkClick] | None, intrinsic: Calibration | None
) -> tuple[dict[str, Any], float, bool]:
    """Landmark clicks → homography params + held-out RMS in meters (US-C2).

    When a valid intrinsic record is supplied its distortion model undistorts
    the click pixels before the fit and its id is recorded in the params as
    ``intrinsics_id`` (US-C1 traceability). The last element of the returned
    tuple is ``rms_in_sample``: True when the adaptive holdout was 0 (minimum
    click count), i.e. the RMS was not measured on held-out landmarks.
    """
    if landmarks is None or len(landmarks) < MIN_EXTRINSIC_LANDMARKS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"extrinsic calibration requires >= {MIN_EXTRINSIC_LANDMARKS} landmark clicks",
        )
    names = [landmark.name for landmark in landmarks]
    if len(set(names)) != len(names):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "landmark names must be unique")
    catalog = landmark_catalog()
    unknown = sorted(set(names) - set(catalog))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": "unknown landmark names", "unknown_landmarks": unknown},
        )
    pixel_xy = [landmark.px for landmark in landmarks]
    if intrinsic is not None:
        try:
            pixel_xy = intrinsics.undistort_pixels(intrinsic.params, pixel_xy)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"stored intrinsic calibration {intrinsic.id} is unusable ({exc}); "
                "re-run intrinsic calibration or invalidate the record",
            ) from exc
    pitch_xy = [catalog[landmark.name].xy for landmark in landmarks]
    # The fit itself needs >= MIN_EXTRINSIC_LANDMARKS pairs, so held-out RMS is
    # only affordable once enough extra clicks exist; at the US-C2 minimum the
    # RMS is in-sample.
    holdout = (
        EXTRINSIC_HOLDOUT if len(landmarks) >= MIN_EXTRINSIC_LANDMARKS + EXTRINSIC_HOLDOUT else 0
    )
    try:
        result = extrinsics.fit_homography(pixel_xy, pitch_xy, holdout=holdout)
    except extrinsics.ExtrinsicsError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    params = extrinsics.to_params(result)
    if intrinsic is not None:
        # US-C1 AC: sessions reference the intrinsics version used via
        # Session.calibration -> Calibration.params["intrinsics_id"].
        params["intrinsics_id"] = str(intrinsic.id)
    return params, result.rms_m, holdout == 0


@router.post("/calibrations", status_code=status.HTTP_201_CREATED)
def create_calibration(
    payload: CalibrationIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> CalibrationCreateOut:
    """Store an intrinsic record, or fit + store an extrinsic one (US-C1/C2).

    Extrinsic fits consume the newest valid intrinsic record of the same
    (camera, active era) when one exists: clicks are undistorted through it
    and its id lands in ``params["intrinsics_id"]`` — the traceability chain
    documented in the module docstring. The response is honest about fit
    quality: ``rms_in_sample`` is True when too few clicks were provided for
    a holdout, so the RMS is an in-sample (optimistic) figure. Intrinsic
    records have no fitted RMS here, so they report ``rms_in_sample=False``.
    """
    era_no = _active_era_or_422(db, payload.camera_id)
    rms: float | None
    intrinsic: Calibration | None = None
    rms_in_sample = False
    if payload.kind is CalibrationKind.INTRINSIC:
        params = _validate_intrinsic_params(payload.params)
        rms = None
    elif payload.kind is CalibrationKind.STEREO:
        params = _validate_stereo_params(payload.camera_id, payload.params, payload.landmarks)
        rms = None
    else:
        intrinsic = _latest_valid(db, payload.camera_id, era_no, CalibrationKind.INTRINSIC)
        params, rms, rms_in_sample = _fit_extrinsic(payload.landmarks, intrinsic)
    record = Calibration(
        camera_id=payload.camera_id,
        era_no=era_no,
        kind=payload.kind,
        params=params,
        rms=rms,
    )
    db.add(record)
    db.flush()
    return CalibrationCreateOut(
        **CalibrationOut.model_validate(record).model_dump(),
        intrinsics_id=None if intrinsic is None else intrinsic.id,
        rms_in_sample=rms_in_sample,
    )


@router.get("/calibrations")
def list_calibrations(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    camera_id: str | None = None,
    kind: CalibrationKind | None = None,
    valid: bool | None = None,
) -> list[CalibrationOut]:
    query = select(Calibration)
    if camera_id is not None:
        query = query.where(Calibration.camera_id == camera_id)
    if kind is not None:
        query = query.where(Calibration.kind == kind)
    if valid is not None:
        query = query.where(Calibration.valid.is_(valid))
    records = db.scalars(query.order_by(Calibration.created_at.desc())).all()
    return [CalibrationOut.model_validate(record) for record in records]


@router.post("/calibrations/{calibration_id}/invalidate")
def invalidate_calibration(
    calibration_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> CalibrationOut:
    """US-C4 resolution flow: the camera moved — retire the record explicitly."""
    record = db.get(Calibration, calibration_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "calibration not found")
    record.valid = False
    db.add(
        AuditLog(
            actor=role.value,
            action="calibration_invalidate",
            entity="calibration",
            entity_id=str(calibration_id),
            detail={"camera_id": record.camera_id, "era_no": record.era_no},
        )
    )
    db.flush()
    return CalibrationOut.model_validate(record)


def _resolve_attach_target(db: OrmSession, payload: CalibrationAttachIn) -> Calibration:
    exactly_one = (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "provide exactly one of calibration_id or reuse_latest_for_camera",
    )
    if payload.calibration_id is not None:
        if payload.reuse_latest_for_camera is not None:
            raise HTTPException(*exactly_one)
        record = db.get(Calibration, payload.calibration_id)
        if record is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown calibration_id")
        return record
    if payload.reuse_latest_for_camera is None:
        raise HTTPException(*exactly_one)
    era_no = _active_era_or_422(db, payload.reuse_latest_for_camera)
    latest = _latest_valid(db, payload.reuse_latest_for_camera, era_no, CalibrationKind.EXTRINSIC)
    if latest is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no valid extrinsic calibration to reuse for camera "
            f"{payload.reuse_latest_for_camera} era {era_no}",
        )
    return latest


@router.put("/sessions/{session_id}/calibration")
def attach_session_calibration(
    session_id: uuid.UUID,
    payload: CalibrationAttachIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> CalibrationOut:
    """US-C3: link the session to its calibration truth (fresh or explicit reuse).

    A record from a stale placement era (the camera has re-registered since,
    US-C2/C4) or an invalidated record must never attach: both mean the
    camera has moved since that calibration was fitted.
    """
    session = _get_session_or_404(db, session_id, role)
    record = _resolve_attach_target(db, payload)
    if record.kind is not CalibrationKind.EXTRINSIC:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "sessions link extrinsic (pitch-mapping) calibrations, not intrinsic records",
        )
    if not record.valid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "calibration record has been invalidated: "
            "camera has moved since this calibration; recalibrate",
        )
    if record.era_no != _active_era_or_422(db, record.camera_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"calibration is from placement era {record.era_no}, not camera "
            f"{record.camera_id}'s current era: camera has moved since this "
            "calibration; recalibrate",
        )
    session.calibration_id = record.id
    db.add(
        AuditLog(
            actor=role.value,
            action="calibration_attach",
            entity="session",
            entity_id=str(session_id),
            detail={
                "calibration_id": str(record.id),
                "camera_id": record.camera_id,
                "era_no": record.era_no,
                "reused_latest": payload.calibration_id is None,
            },
        )
    )
    db.flush()
    return CalibrationOut.model_validate(record)


@router.get("/sessions/{session_id}/calibration")
def get_session_calibration(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> CalibrationOut:
    session = _get_session_or_404(db, session_id, role)
    record = session.calibration
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session has no linked calibration")
    return CalibrationOut.model_validate(record)


@router.post("/sessions/{session_id}/drift-check")
def drift_check(
    session_id: uuid.UUID,
    payload: DriftCheckIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> DriftCheckOut:
    """US-C4: compare freshly observed landmark pixels against the stored era
    calibration. Drift flags the session suspect; the flag is sticky (see
    module docstring) and only ``/calibration/verify`` clears it."""
    session = _get_session_or_404(db, session_id, role)
    era_no = _active_era_or_422(db, payload.camera_id)
    record = _latest_valid(db, payload.camera_id, era_no, CalibrationKind.EXTRINSIC)
    if record is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no valid extrinsic calibration for camera {payload.camera_id} era {era_no}",
        )
    catalog = landmark_catalog()
    observed = {landmark.name: landmark.px for landmark in payload.observed_landmarks}
    try:
        calibration = extrinsics.from_params(record.params)
        expected = {
            name: extrinsics.pitch_xy_to_pixel(calibration, catalog[name].xy)
            for name in observed
            if name in catalog
        }
    except extrinsics.ExtrinsicsError as exc:
        # Malformed/singular stored params must read as 422, never a 500.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"stored calibration unusable; recalibrate ({exc})",
        ) from exc
    try:
        stats = landmark_shift_stats(expected, observed)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    drifted = is_drifted(stats, threshold_px=payload.threshold_px)
    if drifted:
        session.calibration_suspect = True
        db.add(
            AuditLog(
                actor=role.value,
                action="calibration_suspect",
                entity="session",
                entity_id=str(session_id),
                detail={
                    "camera_id": payload.camera_id,
                    "calibration_id": str(record.id),
                    "median_px": stats.median_px,
                    "max_px": stats.max_px,
                    "threshold_px": payload.threshold_px,
                    "n_landmarks": stats.n,
                },
            )
        )
        db.flush()
    return DriftCheckOut(
        median_px=stats.median_px,
        max_px=stats.max_px,
        per_landmark=stats.per_landmark,
        drifted=drifted,
        threshold_px=payload.threshold_px,
    )


@router.post("/sessions/{session_id}/calibration/verify")
def verify_session_calibration(
    session_id: uuid.UUID,
    payload: CalibrationVerifyIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> SuspectStateOut:
    """US-C4 resolution: the Parent must say HOW the suspicion was resolved.

    ``accept`` keeps the flagged marks as-is and clears the sticky
    ``calibration_suspect`` flag with an explicit accept-with-flag audit
    trail. ``recalibrated`` first remaps every stored bounce mark of the
    session through the current calibration truth
    (:func:`cricai_api.services.bounce_mapping.recompute_session_marks`) so
    no pitch coordinate derived from the drifted homography survives; a
    camera without a valid current extrinsic calibration turns into a 409
    listing what still needs recalibrating, and the flag stays set.
    """
    session = _get_session_or_404(db, session_id, role)
    if payload.action == "accept":
        action = "calibration_accept_with_flag"
        detail: dict[str, Any] | None = None
    else:
        try:
            marks_updated = bounce_mapping.recompute_session_marks(db, session)
        except bounce_mapping.MappingUnavailableError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "message": "recalibration incomplete: cameras still lack a valid "
                    "extrinsic calibration for their current era",
                    "cameras_needing_recalibration": exc.cameras,
                },
            ) from exc
        action = "calibration_backfill"
        detail = {"marks_updated": marks_updated}
    session.calibration_suspect = False
    db.add(
        AuditLog(
            actor=role.value,
            action=action,
            entity="session",
            entity_id=str(session_id),
            detail=detail,
        )
    )
    db.flush()
    return SuspectStateOut(session_id=session.id, calibration_suspect=session.calibration_suspect)
