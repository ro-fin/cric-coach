"""US-A1 / US-I1: camera registry — per-camera placement eras with FOV
reference images and capture roles.

Each POST for an existing ``camera_id`` opens a new placement era (US-C4
dependency): the era number auto-increments and previous eras deactivate.
The 4-camera MVP uses C1..C4; C5..C8 stay flagged as reserved by the mount
plan *until a registration declares a* :class:`~cricai_data.enums.CameraRole`
— a role-carrying registration is a real one (US-I1: C5-C7 join bowling-side
capture). A role is registry metadata plus the ``?role=`` listing filter; no
pipeline stage consumes cameras by role today — stages select by camera id
(bowling-action reads C5). Role-driven stage consumption is a future seam.
"""

import uuid
from typing import Annotated, Literal

from cricai_data.enums import CameraRole, Role
from cricai_data.models import CameraConfig
from cricai_data.storage import FsObjectStore
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from cricai_api.auth import require_roles
from cricai_api.deps import get_db, get_store

router = APIRouter(prefix="/cameras", tags=["cameras"])

CAMERA_ID_PATTERN = r"^C[1-8]$"
RESOLUTION_PATTERN = r"^[1-9]\d{2,4}x[1-9]\d{2,4}$"

#: Mount plan reserves C5..C8 for expansion. A reserved id becomes a real
#: registration once its active era declares a role (US-I1: C5-C7 bowling
#: capture); a role-less registration keeps the reserved flag.
RESERVED_CAMERA_IDS = frozenset({"C5", "C6", "C7", "C8"})


def fov_reference_key(camera_id: str, era_no: int) -> str:
    """Canonical object key for a camera era's FOV reference image."""
    return f"calibration/fov/{camera_id}/era{era_no}.png"


class XyzOffset(BaseModel):
    x: float
    y: float
    z: float


class CameraIn(BaseModel):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN)
    role: CameraRole | None = None  # US-I1: what this camera looks at
    position_label: str = Field(min_length=1, max_length=64)
    xyz_offset_m: XyzOffset
    height_m: float = Field(gt=0)
    fps: Literal[30, 60, 120, 240]
    resolution: str = Field(pattern=RESOLUTION_PATTERN)
    lens: str = Field(default="", max_length=64)
    mount: str = Field(default="", max_length=64)
    protected: bool = False
    fov_reference_key: str | None = Field(default=None, max_length=255)


class CameraRolePatch(BaseModel):
    """PATCH body (US-I1). ``role`` is required but nullable: an explicit
    ``null`` clears the role, returning a C5-C8 camera to reserved semantics;
    omitting the field entirely is a validation error."""

    role: CameraRole | None


class CameraOut(BaseModel):
    id: uuid.UUID
    camera_id: str
    era_no: int
    active: bool
    role: CameraRole | None
    position_label: str
    xyz_offset_m: dict[str, float]
    height_m: float
    fps: int
    resolution: str
    lens: str
    mount: str
    protected: bool
    fov_reference_key: str | None
    reserved_for_future: bool


def _to_out(config: CameraConfig) -> CameraOut:
    return CameraOut(
        id=config.id,
        camera_id=config.camera_id,
        era_no=config.era_no,
        active=config.active,
        role=config.role,
        position_label=config.position_label,
        xyz_offset_m=config.xyz_offset_m,
        height_m=config.height_m,
        fps=config.fps,
        resolution=config.resolution,
        lens=config.lens,
        mount=config.mount,
        protected=config.protected,
        fov_reference_key=config.fov_reference_key,
        # US-I1: a role-carrying registration is a real one, not a reservation.
        reserved_for_future=config.camera_id in RESERVED_CAMERA_IDS and config.role is None,
    )


def _active_config(db: Session, camera_id: str) -> CameraConfig | None:
    return db.scalars(
        select(CameraConfig)
        .where(CameraConfig.camera_id == camera_id, CameraConfig.active.is_(True))
        .order_by(CameraConfig.era_no.desc())
    ).first()


@router.post("", status_code=status.HTTP_201_CREATED)
def create_camera_config(
    payload: CameraIn,
    db: Annotated[Session, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> CameraOut:
    max_era = db.scalar(
        select(func.max(CameraConfig.era_no)).where(CameraConfig.camera_id == payload.camera_id)
    )
    if max_era is not None:
        db.execute(
            update(CameraConfig)
            .where(CameraConfig.camera_id == payload.camera_id, CameraConfig.active.is_(True))
            .values(active=False)
        )
    config = CameraConfig(
        camera_id=payload.camera_id,
        era_no=1 if max_era is None else max_era + 1,
        active=True,
        role=payload.role,
        position_label=payload.position_label,
        xyz_offset_m=payload.xyz_offset_m.model_dump(),
        height_m=payload.height_m,
        fps=payload.fps,
        resolution=payload.resolution,
        lens=payload.lens,
        mount=payload.mount,
        protected=payload.protected,
        fov_reference_key=payload.fov_reference_key,
    )
    db.add(config)
    db.flush()
    return _to_out(config)


@router.get("")
def list_camera_configs(
    db: Annotated[Session, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    include_history: bool = False,
    role: CameraRole | None = None,
) -> list[CameraOut]:
    """List configs; ``role`` filters to cameras registered for that view
    (US-I1: pipeline stages ask the registry which cameras to consume)."""
    query = select(CameraConfig).order_by(CameraConfig.camera_id, CameraConfig.era_no)
    if not include_history:
        query = query.where(CameraConfig.active.is_(True))
    if role is not None:
        query = query.where(CameraConfig.role == role)
    return [_to_out(c) for c in db.scalars(query).all()]


@router.patch("/{camera_id}")
def patch_camera_role(
    camera_id: str,
    payload: CameraRolePatch,
    db: Annotated[Session, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> CameraOut:
    """Set or clear the active era's role in place (US-I1).

    Assigning a role does not move the camera, so it must not open a new
    placement era — calibration and FOV references stay valid.
    """
    config = _active_config(db, camera_id)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active config for camera")
    config.role = payload.role
    db.flush()
    return _to_out(config)


@router.get("/{camera_id}")
def get_camera_config(
    camera_id: str,
    db: Annotated[Session, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> CameraOut:
    config = _active_config(db, camera_id)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active config for camera")
    return _to_out(config)


@router.post("/{camera_id}/fov-reference")
def upload_fov_reference(
    camera_id: str,
    image: Annotated[bytes, Body(media_type="image/png")],
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> CameraOut:
    config = _active_config(db, camera_id)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active config for camera")
    key = fov_reference_key(camera_id, config.era_no)
    store.put(key, image)
    config.fov_reference_key = key
    db.flush()
    return _to_out(config)
