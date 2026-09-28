"""US-A4: pre-session health check — run the pure framework, persist the report."""

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from cricai_data.enums import Role
from cricai_data.healthcheck import (
    CameraStats,
    FrameStats,
    HealthCheckInputs,
    run_health_check,
)
from cricai_data.models import HealthCheckRecord, Session
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/health-checks", tags=["health-checks"])


class FrameStatsIn(BaseModel):
    mean_brightness: float = Field(ge=0, le=255)
    frames_delta: int = Field(ge=0)


class CameraStatsIn(BaseModel):
    camera_id: str = Field(min_length=1, max_length=8)
    frame_stats: FrameStatsIn
    measured_fps: float = Field(ge=0)


class HealthCheckIn(BaseModel):
    session_id: uuid.UUID | None = None
    cameras: list[CameraStatsIn] = Field(min_length=1)
    target_fps: float = Field(gt=0)
    free_disk_bytes: int = Field(ge=0)
    expected_session_bytes: int = Field(ge=0)
    max_sync_offset_ms: float = Field(ge=0)


class HealthCheckRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID | None
    at: datetime
    passed: bool
    results: dict[str, Any]


@router.post("", status_code=status.HTTP_201_CREATED)
def run_and_record_health_check(
    payload: HealthCheckIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> HealthCheckRecordOut:
    if payload.session_id is not None and db.get(Session, payload.session_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown session_id")
    inputs = HealthCheckInputs(
        cameras=tuple(
            CameraStats(
                camera_id=camera.camera_id,
                frame_stats=FrameStats(
                    mean_brightness=camera.frame_stats.mean_brightness,
                    frames_delta=camera.frame_stats.frames_delta,
                ),
                measured_fps=camera.measured_fps,
            )
            for camera in payload.cameras
        ),
        target_fps=payload.target_fps,
        free_disk_bytes=payload.free_disk_bytes,
        expected_session_bytes=payload.expected_session_bytes,
        max_sync_offset_ms=payload.max_sync_offset_ms,
    )
    report = run_health_check(inputs)
    record = HealthCheckRecord(
        session_id=payload.session_id,
        passed=report.passed,
        results={"checks": [asdict(result) for result in report.results]},
    )
    db.add(record)
    db.flush()
    return HealthCheckRecordOut.model_validate(record)


@router.get("")
def list_health_checks(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    session_id: uuid.UUID | None = None,
) -> list[HealthCheckRecordOut]:
    query = select(HealthCheckRecord)
    if session_id is not None:
        query = query.where(HealthCheckRecord.session_id == session_id)
    records = db.scalars(query.order_by(HealthCheckRecord.at.desc())).all()
    return [HealthCheckRecordOut.model_validate(record) for record in records]
