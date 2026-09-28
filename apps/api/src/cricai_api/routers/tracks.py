"""Ball-track read API (US-F3): list rows, per-ball payload, coverage/flag filters.

Read-only surfaces over the ``ball_tracks`` table maintained by the
``cricai_worker.track_balls`` job:

- ``GET /sessions/{id}/tracks`` — list/filter every per-ball per-camera row
  (ball, camera, minimum coverage, identity_risk/long_gap flags);
- ``GET /sessions/{id}/balls/{ball_no}/track?camera=`` — one row plus its
  pinned trajectory payload straight from the object store.

A row whose payload is missing or unreadable is a 409 (the row is provenance
for stored bytes; re-running the tracking job heals it) — never a silent 500.
All roles may read; players never see guest-player sessions (US-L3 — the same
404-hiding rule as the clips router).
"""

import json
import uuid
from typing import Annotated, Any

from cricai_data.enums import Role
from cricai_data.models import BallTrack, Session
from cricai_data.storage import FsObjectStore, StorageError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db, get_store

router = APIRouter(prefix="/sessions/{session_id}", tags=["tracks"])

CAMERA_ID_PATTERN = r"^C[1-8]$"

READ_ROLES = (Role.PARENT, Role.COACH, Role.PLAYER)


class TrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    ball_no: int
    camera_id: str
    tracker_version: str
    points_key: str
    coverage: float
    segments: list[dict[str, Any]]
    flags: dict[str, Any]
    confidence: float


class BallTrackOut(TrackOut):
    """One track row plus its full trajectory payload (pinned US-F3 contract)."""

    payload: dict[str, Any]


def _get_session(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


@router.get("/tracks")
def list_tracks(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    ball_no: Annotated[int | None, Query(ge=1)] = None,
    camera_id: Annotated[str | None, Query(pattern=CAMERA_ID_PATTERN)] = None,
    min_coverage: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    identity_risk: Annotated[bool | None, Query()] = None,
    long_gap: Annotated[bool | None, Query()] = None,
) -> list[TrackOut]:
    _get_session(db, session_id, role)
    query = select(BallTrack).where(BallTrack.session_id == session_id)
    if ball_no is not None:
        query = query.where(BallTrack.ball_no == ball_no)
    if camera_id is not None:
        query = query.where(BallTrack.camera_id == camera_id)
    if min_coverage is not None:
        query = query.where(BallTrack.coverage >= min_coverage)
    rows = db.scalars(query.order_by(BallTrack.ball_no, BallTrack.camera_id)).all()
    # Flag filters run in Python: ``flags`` is a JSON column and this API must
    # behave identically on SQLite (tests) and PostgreSQL (production).
    wanted = {"identity_risk": identity_risk, "long_gap": long_gap}
    return [
        TrackOut.model_validate(row)
        for row in rows
        if all(
            value is None or bool(row.flags.get(name, False)) == value
            for name, value in wanted.items()
        )
    ]


@router.get("/balls/{ball_no}/track")
def ball_track(
    session_id: uuid.UUID,
    ball_no: Annotated[int, PathParam(ge=1)],
    camera: Annotated[str, Query(pattern=CAMERA_ID_PATTERN)],
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    store: Annotated[FsObjectStore, Depends(get_store)],
) -> BallTrackOut:
    """One ball's track on one camera, row + trajectory payload (US-F3)."""
    _get_session(db, session_id, role)
    row = db.execute(
        select(BallTrack).where(
            BallTrack.session_id == session_id,
            BallTrack.ball_no == ball_no,
            BallTrack.camera_id == camera,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no track for ball {ball_no} camera {camera}"
        )
    try:
        payload = json.loads(store.get(row.points_key))
    except StorageError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "track payload missing from store; re-run the tracking job",
        ) from exc
    except ValueError as exc:  # malformed JSON / encoding
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "track payload unreadable; re-run the tracking job",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "track payload malformed; re-run the tracking job",
        )
    return BallTrackOut(**TrackOut.model_validate(row).model_dump(), payload=payload)
