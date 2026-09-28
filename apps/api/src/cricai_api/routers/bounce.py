"""US-C5: manual bounce-point tagging mapped to pitch coordinates (V1 pitch map).

One click on the bounce frame stores the raw pixel, camera, frame, the pitch
x/y computed through the right calibration, and derived line/length zone
classes. The pixel->pitch pipeline (session-aware calibration resolution,
intrinsic undistortion, error wrapping) lives in
:mod:`cricai_api.services.bounce_mapping` so the calibration verify/backfill
flow shares it. Raw px and pitch xy are persisted so zone boundaries stay
configuration: reclassify re-derives classes for a whole session without
re-clicking. Same-ball marks from two cameras that disagree by more than
``bounce_mapping.CROSS_CAMERA_AGREEMENT_M`` flag every mark of that ball for
review; agreement is re-evaluated on every save, so a corrected re-click
clears the flag.
"""

import uuid
from typing import Annotated, Any

from cricai_data.enums import Handedness, Length, Line, Role, SessionType
from cricai_data.models import BounceMark, Session
from cricai_vision.zones import ZoneConfig, ZoneConfigError, classify
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.services.bounce_mapping import (
    MappingUnavailableError,
    map_click,
    reevaluate_agreement,
)

router = APIRouter(prefix="/sessions/{session_id}/bounce-marks", tags=["bounce"])

CAMERA_ID_PATTERN = r"^C[1-8]$"


class PixelPoint(BaseModel):
    x: float
    y: float


class BounceMarkIn(BaseModel):
    ball_no: int = Field(ge=1)
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN)
    frame_no: int = Field(ge=0)
    px: PixelPoint
    zone_config: dict[str, Any] | None = None


class BounceMarkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    ball_no: int
    camera_id: str
    frame_no: int
    px_x: float
    px_y: float
    pitch_x: float
    pitch_y: float
    line: Line | None
    length: Length | None
    calibration_id: uuid.UUID | None
    flagged_for_review: bool


class AgreementInfo(BaseModel):
    """Cross-camera agreement for the saved mark's ball (US-C5)."""

    other_camera: str
    distance_m: float


class BounceMarkSaved(BaseModel):
    mark: BounceMarkOut
    agreement: AgreementInfo | None


class ReclassifyIn(BaseModel):
    zone_config: dict[str, Any] | None = None


class ReclassifyOut(BaseModel):
    count: int
    marks: list[BounceMarkOut]


def _get_session(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _zone_config(raw: dict[str, Any] | None) -> ZoneConfig:
    if raw is None:
        return ZoneConfig()
    try:
        return ZoneConfig.from_dict(raw)
    except ZoneConfigError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid zone config: {exc}"
        ) from exc


def _zone_handedness(session: Session) -> Handedness:
    """The frame the line channels are read in for this session's marks.

    In a BATTING session the player IS the batter, so their handedness frames
    the zones and a left-hander mirrors the line channels (US-C5). In a BOWLING
    session the player is the bowler and their batting handedness is irrelevant
    to where a delivery pitched: zones use the fixed canonical right-hand frame
    — the SAME frame US-I4 target scoring pins
    (``trajectory.score_delivery`` defaults ``Handedness.RIGHT``) — so the
    pitch map can never mirror against the accuracy scorecard for a
    left-handed kid's bowling session (mirrors ``estimate_bounces``' auto leg).
    """
    if session.session_type is SessionType.BOWLING:
        return Handedness.RIGHT
    return session.player.handedness


def _classify_or_422(
    config: ZoneConfig, pitch_x: float, pitch_y: float, handedness: Handedness
) -> tuple[Line, Length]:
    try:
        return classify(config, pitch_x, pitch_y, handedness)
    except ZoneConfigError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"bounce point ({pitch_x:.3f}, {pitch_y:.3f}) m cannot be classified: {exc}",
        ) from exc


@router.post("")
def upsert_bounce_mark(
    session_id: uuid.UUID,
    payload: BounceMarkIn,
    response: Response,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> BounceMarkSaved:
    session = _get_session(db, session_id, role)
    config = _zone_config(payload.zone_config)
    try:
        # Session-attached calibration first (US-C3 era consistency), then the
        # camera's current era; intrinsic undistortion applied when available.
        point = map_click(db, payload.camera_id, (payload.px.x, payload.px.y), session=session)
    except MappingUnavailableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    # LH batters mirror line channels inside classify; a bowling session reads
    # the canonical right-hand frame so the mark never mirrors against the
    # US-I4 scorecard (guest players included).
    line, length = _classify_or_422(config, point.pitch_x, point.pitch_y, _zone_handedness(session))

    mark = db.scalar(
        select(BounceMark).where(
            BounceMark.session_id == session_id,
            BounceMark.ball_no == payload.ball_no,
            BounceMark.camera_id == payload.camera_id,
        )
    )
    created = mark is None
    if mark is None:
        mark = BounceMark(
            session_id=session_id, ball_no=payload.ball_no, camera_id=payload.camera_id
        )
        db.add(mark)
    # A re-click replaces the raw pixel and everything derived from it.
    mark.frame_no = payload.frame_no
    mark.px_x = payload.px.x
    mark.px_y = payload.px.y
    mark.pitch_x = point.pitch_x
    mark.pitch_y = point.pitch_y
    mark.line = line
    mark.length = length
    mark.calibration_id = point.calibration_id
    db.flush()

    result = reevaluate_agreement(
        db, session_id, payload.ball_no, perspective_camera=payload.camera_id
    )
    agreement = (
        None
        if result is None
        else AgreementInfo(other_camera=result.other_camera, distance_m=result.distance_m)
    )
    db.flush()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return BounceMarkSaved(mark=BounceMarkOut.model_validate(mark), agreement=agreement)


@router.get("")
def list_bounce_marks(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    ball_no: Annotated[int | None, Query(ge=1)] = None,
    flagged: bool | None = None,
) -> list[BounceMarkOut]:
    _get_session(db, session_id, role)
    query = select(BounceMark).where(BounceMark.session_id == session_id)
    if ball_no is not None:
        query = query.where(BounceMark.ball_no == ball_no)
    if flagged is not None:
        query = query.where(BounceMark.flagged_for_review.is_(flagged))
    marks = db.scalars(query.order_by(BounceMark.ball_no, BounceMark.camera_id)).all()
    return [BounceMarkOut.model_validate(mark) for mark in marks]


@router.post("/reclassify")
def reclassify_bounce_marks(
    session_id: uuid.UUID,
    payload: ReclassifyIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> ReclassifyOut:
    """Re-derive line/length for every stored mark from raw pitch xy (US-C5 AC:
    zone boundaries are configuration; changing them never requires re-clicking).
    """
    session = _get_session(db, session_id, role)
    config = _zone_config(payload.zone_config)
    handedness = _zone_handedness(session)
    marks = db.scalars(
        select(BounceMark)
        .where(BounceMark.session_id == session_id)
        .order_by(BounceMark.ball_no, BounceMark.camera_id)
    ).all()
    for mark in marks:
        mark.line, mark.length = _classify_or_422(config, mark.pitch_x, mark.pitch_y, handedness)
    db.flush()
    return ReclassifyOut(count=len(marks), marks=[BounceMarkOut.model_validate(m) for m in marks])
