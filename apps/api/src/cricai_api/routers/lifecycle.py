"""US-A3: start/stop a session as one unit.

State changes go through ``cricai_data.lifecycle.ensure_transition`` — never
hand-set. Start is gated on the safety checklist for machine sessions (US-A5
contract), validates every requested camera against the active registry
(US-A1/US-A2: config is asserted at session start) and persists the roster to
``Session.expected_cameras``. Stop never trusts client-supplied camera lists:
degraded/missing_views are derived from that persisted roster plus footage
evidence via :mod:`cricai_api.services.capture_state`, then widened by any
cameras the operator reported as failed.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from cricai_data.enums import BowlerSource, Role
from cricai_data.lifecycle import SessionLifecycleError, SessionState, ensure_transition, is_noop
from cricai_data.models import CameraConfig, Session, utcnow
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.services.capture_state import recompute_missing_views

router = APIRouter(prefix="/sessions", tags=["lifecycle"])

#: Camera ids are path segments in object keys — keep them storage-safe.
CameraId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,8}$")]

#: US-A2: the primary batting camera must record at analysis-grade fps.
PRIMARY_CAMERA_ID = "C1"
MIN_PRIMARY_FPS = 120


def _as_utc(value: datetime | None) -> datetime | None:
    """Stored timestamps are UTC; some dialects (SQLite) round-trip them naive."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


#: Response timestamps: always timezone-aware UTC, whatever the DB dialect returned.
UtcTimestamp = Annotated[datetime | None, AfterValidator(_as_utc)]


class StartIn(BaseModel):
    cameras: list[CameraId] = Field(min_length=1)


class StartOut(BaseModel):
    state: SessionState
    started_at: UtcTimestamp
    cameras: list[str]
    warnings: list[str]
    idempotent: bool


class CameraReport(BaseModel):
    ok: bool


class StopIn(BaseModel):
    """Stop payload — deliberately minimal.

    Expected cameras are NOT client-supplied at stop time: the roster was
    validated and persisted at start (``Session.expected_cameras``).
    ``cameras_reporting`` lets the operator flag a camera as failed
    (``ok: false``) so it counts as missing even if some footage exists.
    """

    cameras_reporting: dict[CameraId, CameraReport] = Field(default_factory=dict)


class StopOut(BaseModel):
    state: SessionState
    degraded: bool
    missing_views: list[str]
    stopped_at: UtcTimestamp
    idempotent: bool


class LifecycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: SessionState
    degraded: bool
    missing_views: list[str]
    started_at: UtcTimestamp
    stopped_at: UtcTimestamp


def _get_session_or_404(db: OrmSession, session_id: uuid.UUID) -> Session:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _active_configs(db: OrmSession, camera_ids: set[str]) -> dict[str, CameraConfig]:
    """Active registry row per camera id (at most one active era per camera)."""
    rows = db.scalars(
        select(CameraConfig).where(
            CameraConfig.camera_id.in_(sorted(camera_ids)),
            CameraConfig.active.is_(True),
        )
    ).all()
    return {config.camera_id: config for config in rows}


def _fps_warnings(configs: dict[str, CameraConfig]) -> list[str]:
    """US-A2: C1 must record at >=120 fps — asserted (never blocking) at start."""
    primary = configs.get(PRIMARY_CAMERA_ID)
    if primary is not None and primary.fps < MIN_PRIMARY_FPS:
        return [
            f"{PRIMARY_CAMERA_ID} is registered at {primary.fps} fps, below the "
            f"{MIN_PRIMARY_FPS} fps floor for analysis-grade capture (US-A2)"
        ]
    return []


def _apply_stop_verdict(db: OrmSession, session: Session, payload: StopIn) -> None:
    """missing_views = (expected - evidence) UNION (expected cameras reported not-ok).

    Evidence is defined solely by :mod:`cricai_api.services.capture_state`
    (Video rows in ``EVIDENCE_STATUSES`` — FAILED uploads never count). An
    operator ``ok: false`` report forces that camera into missing_views even
    when footage exists; reports for cameras outside the expected roster are
    ignored, so only start-validated ids can appear in missing_views. Reports
    are per-request, not sticky: a re-stop without one recomputes purely from
    evidence.
    """
    recompute_missing_views(db, session)
    expected = set(session.expected_cameras)
    reported_failed = {
        camera_id
        for camera_id, report in payload.cameras_reporting.items()
        if not report.ok and camera_id in expected
    }
    if reported_failed:
        session.missing_views = sorted(set(session.missing_views) | reported_failed)
        session.degraded = True


@router.post("/{session_id}/start")
def start_session(
    session_id: uuid.UUID,
    payload: StartIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> StartOut:
    session = _get_session_or_404(db, session_id)
    if is_noop(session.state, SessionState.RECORDING):
        # Repeated start: the payload is ignored — the persisted roster is truth.
        configs = _active_configs(db, set(session.expected_cameras))
        return StartOut(
            state=session.state,
            started_at=session.started_at,
            cameras=list(session.expected_cameras),
            warnings=_fps_warnings(configs),
            idempotent=True,
        )
    if session.bowler_source is BowlerSource.MACHINE and session.checklist_ack_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "safety checklist not acknowledged")
    cameras = sorted(set(payload.cameras))
    configs = _active_configs(db, set(cameras))
    unknown = [camera_id for camera_id in cameras if camera_id not in configs]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": "cameras not registered as active in the camera registry",
                "unknown_cameras": unknown,
            },
        )
    try:
        session.state = ensure_transition(session.state, SessionState.RECORDING)
    except SessionLifecycleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.expected_cameras = cameras
    session.started_at = utcnow()
    db.flush()
    return StartOut(
        state=session.state,
        started_at=session.started_at,
        cameras=cameras,
        warnings=_fps_warnings(configs),
        idempotent=False,
    )


@router.post("/{session_id}/stop")
def stop_session(
    session_id: uuid.UUID,
    payload: StopIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> StopOut:
    session = _get_session_or_404(db, session_id)
    if is_noop(session.state, SessionState.CAPTURED):
        # Re-stop RECOMPUTES the verdict from current evidence (a late upload
        # can clear a missing view) instead of echoing the frozen one; the
        # state and stopped_at timestamp are untouched.
        _apply_stop_verdict(db, session, payload)
        db.flush()
        return StopOut(
            state=session.state,
            degraded=session.degraded,
            missing_views=session.missing_views,
            stopped_at=session.stopped_at,
            idempotent=True,
        )
    try:
        session.state = ensure_transition(session.state, SessionState.CAPTURED)
    except SessionLifecycleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    _apply_stop_verdict(db, session, payload)
    session.stopped_at = utcnow()
    db.flush()
    return StopOut(
        state=session.state,
        degraded=session.degraded,
        missing_views=session.missing_views,
        stopped_at=session.stopped_at,
        idempotent=False,
    )


@router.get("/{session_id}/lifecycle")
def get_lifecycle(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> LifecycleOut:
    return LifecycleOut.model_validate(_get_session_or_404(db, session_id))
