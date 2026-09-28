"""Per-ball clip listing, camera-set view and loud gap report (US-D2).

Read-only surfaces over the ``clips`` table maintained by the
``cricai_worker.cut_clips`` job:

- ``GET /sessions/{id}/clips`` — list/filter every per-ball per-camera row;
- ``GET /sessions/{id}/clips/gaps`` — per-ball missing-camera report, the
  loud form of the US-D2 "recorded gap, not silent absence" AC;
- ``GET /sessions/{id}/balls/{ball_no}/clips`` — one ball's camera set with
  object keys and statuses, the dashboard camera-switch source.

All roles may read; players never see guest-player sessions (US-L3 — the
same 404-hiding rule as the bounce/heatmap routers).
"""

import uuid
from typing import Annotated

from cricai_data.enums import ClipStatus, Role
from cricai_data.models import Clip, Session
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/sessions/{session_id}", tags=["clips"])

CAMERA_ID_PATTERN = r"^C[1-8]$"

READ_ROLES = (Role.PARENT, Role.COACH, Role.PLAYER)


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    ball_no: int
    camera_id: str
    object_key: str | None
    start_ms: int
    end_ms: int
    status: ClipStatus
    error: str | None


class BallClipsOut(BaseModel):
    """One ball's camera set — the dashboard camera-switch source (US-D2)."""

    ball_no: int
    cameras: list[ClipOut]


class BallGapReport(BaseModel):
    """Cameras a ball is missing a playable clip for, with the recorded why."""

    ball_no: int
    missing_cameras: list[str]
    reasons: dict[str, str]


def _get_session(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


@router.get("/clips")
def list_clips(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    ball_no: Annotated[int | None, Query(ge=1)] = None,
    camera_id: Annotated[str | None, Query(pattern=CAMERA_ID_PATTERN)] = None,
    status_filter: Annotated[ClipStatus | None, Query(alias="status")] = None,
) -> list[ClipOut]:
    _get_session(db, session_id, role)
    query = select(Clip).where(Clip.session_id == session_id)
    if ball_no is not None:
        query = query.where(Clip.ball_no == ball_no)
    if camera_id is not None:
        query = query.where(Clip.camera_id == camera_id)
    if status_filter is not None:
        query = query.where(Clip.status == status_filter)
    clips = db.scalars(query.order_by(Clip.ball_no, Clip.camera_id)).all()
    return [ClipOut.model_validate(clip) for clip in clips]


@router.get("/clips/gaps")
def clip_gap_report(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> list[BallGapReport]:
    """Per-ball missing-camera report (US-D2 AC: gaps are loud, never silent).

    Any row that is not CUT counts as missing — GAP (no footage), FAILED
    (ffmpeg error) and PENDING (not processed yet) — with the recorded reason.
    """
    _get_session(db, session_id, role)
    rows = db.scalars(
        select(Clip)
        .where(Clip.session_id == session_id, Clip.status != ClipStatus.CUT)
        .order_by(Clip.ball_no, Clip.camera_id)
    ).all()
    reports: dict[int, BallGapReport] = {}
    for clip in rows:
        report = reports.setdefault(
            clip.ball_no, BallGapReport(ball_no=clip.ball_no, missing_cameras=[], reasons={})
        )
        report.missing_cameras.append(clip.camera_id)
        report.reasons[clip.camera_id] = _reason(clip)
    return list(reports.values())


def _reason(clip: Clip) -> str:
    label = clip.status.value
    return f"{label}: {clip.error}" if clip.error else label


@router.get("/balls/{ball_no}/clips")
def ball_clips(
    session_id: uuid.UUID,
    ball_no: Annotated[int, PathParam(ge=1)],
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> BallClipsOut:
    """One ball's clips across every camera (dashboard camera-switch, US-D2)."""
    _get_session(db, session_id, role)
    clips = db.scalars(
        select(Clip)
        .where(Clip.session_id == session_id, Clip.ball_no == ball_no)
        .order_by(Clip.camera_id)
    ).all()
    if not clips:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no clips for ball {ball_no}")
    return BallClipsOut(ball_no=ball_no, cameras=[ClipOut.model_validate(clip) for clip in clips])
