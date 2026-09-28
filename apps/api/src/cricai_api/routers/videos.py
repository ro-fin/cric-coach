"""US-B2: resumable multi-camera video upload — parts, checksum verify, probe, dedupe.

Design: the API holds two ids per upload — the ``UploadSession`` row id (DB
bookkeeping, resume source of truth) and the object store's multipart upload
id, persisted on the row at creation (US-L3 purge depends on that copy). The
client still echoes the store id on every part/complete call and it must match
the persisted value. Files assemble into a per-upload staging key, and every
DB-side conflict check (dedupe, filename clash) runs BEFORE staging is
consumed, so a rejected complete never wedges a resumable upload; only
verified content is promoted to the canonical
``sessions/{session_id}/{camera_id}/{filename}`` key.
"""

import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from cricai_data.enums import Role, VideoStatus
from cricai_data.lifecycle import SessionState
from cricai_data.models import Session, UploadPart, UploadSession, Video
from cricai_data.probe import ProbeError, ProbeUnavailableError, VideoProbe, probe_video
from cricai_data.storage import FsObjectStore, StorageError, video_key
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db, get_store
from cricai_api.services.capture_state import EVIDENCE_STATUSES, recompute_missing_views

router = APIRouter(tags=["videos"])

#: Probed fps may differ from the claim by at most 1% before flagging a conflict.
FPS_TOLERANCE = 0.01
#: Probed duration may differ from the claim by at most 2%.
DURATION_TOLERANCE = 0.02
#: Keeps every object key (final and staging) within the 255-char column.
MAX_FILENAME_LENGTH = 180
#: Camera ids come from the fixed rig registry (C1..C8) — anything else would
#: pollute per-camera storage prefixes and the clip retention tier.
CAMERA_ID_PATTERN = r"^C[1-8]$"
#: Session states in which late footage must re-derive missing_views (US-A3 seam).
_RECOMPUTE_STATES = frozenset(
    {SessionState.CAPTURED, SessionState.PROCESSING, SessionState.ANALYZED}
)

_ingest_roles = require_roles(Role.PARENT, Role.COACH)


class UploadCreateIn(BaseModel):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN)
    filename: str = Field(min_length=1, max_length=MAX_FILENAME_LENGTH)
    declared_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_size: int = Field(ge=1)
    claimed_fps: float | None = Field(default=None, gt=0)
    claimed_resolution: str | None = Field(default=None, pattern=r"^\d+x\d+$", max_length=16)
    claimed_duration_s: float | None = Field(default=None, gt=0)


class UploadCreated(BaseModel):
    upload_id: uuid.UUID
    store_upload_id: str


class PartOut(BaseModel):
    part_no: int
    size_bytes: int
    checksum_sha256: str


class UploadStatusOut(BaseModel):
    upload_id: uuid.UUID
    declared_size: int
    completed: bool
    received_parts: list[PartOut]


class CompleteIn(BaseModel):
    store_upload_id: str = Field(min_length=1)
    part_count: int = Field(ge=1)


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    camera_id: str
    object_key: str
    filename: str
    checksum_sha256: str
    size_bytes: int
    claimed_fps: float | None
    claimed_resolution: str | None
    claimed_duration_s: float | None
    codec: str | None
    status: VideoStatus
    probe: dict[str, Any] | None
    error: str | None


class CompleteOut(BaseModel):
    video: VideoOut
    deduplicated: bool


@router.post("/sessions/{session_id}/videos/uploads", status_code=status.HTTP_201_CREATED)
def create_upload(
    session_id: uuid.UUID,
    payload: UploadCreateIn,
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    _role: Annotated[Role, Depends(_ingest_roles)],
) -> UploadCreated:
    if db.get(Session, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    try:
        video_key(str(session_id), payload.camera_id, payload.filename)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid upload target: {exc}"
        ) from exc
    store_upload_id = store.begin_multipart()
    upload = UploadSession(
        session_id=session_id,
        camera_id=payload.camera_id,
        filename=payload.filename,
        declared_checksum=payload.declared_checksum,
        declared_size=payload.declared_size,
        store_upload_id=store_upload_id,
        claimed_fps=payload.claimed_fps,
        claimed_resolution=payload.claimed_resolution,
        claimed_duration_s=payload.claimed_duration_s,
    )
    db.add(upload)
    db.flush()
    return UploadCreated(upload_id=upload.id, store_upload_id=store_upload_id)


def _matching_store_id(upload: UploadSession, client_value: str) -> str:
    """The persisted multipart id is authoritative; the client echo must agree.

    Guards against parts landing in another upload's staging area (and keeps
    the US-L3 purge path — which only knows the persisted id — trustworthy).
    """
    persisted = upload.store_upload_id
    if persisted is None or persisted != client_value:
        raise HTTPException(status.HTTP_409_CONFLICT, "store_upload_id does not match this upload")
    return persisted


@router.put("/videos/uploads/{upload_id}/parts/{part_no}")
async def upload_part(
    upload_id: uuid.UUID,
    part_no: Annotated[int, PathParam(ge=1)],
    store_upload_id: Annotated[str, Query(min_length=1)],
    request: Request,
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    _role: Annotated[Role, Depends(_ingest_roles)],
) -> PartOut:
    upload = db.get(UploadSession, upload_id)
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")
    if upload.completed:
        raise HTTPException(status.HTTP_409_CONFLICT, "upload already completed")
    store_id = _matching_store_id(upload, store_upload_id)
    data = await request.body()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "part body must not be empty")
    try:
        checksum = store.put_part(store_id, part_no, data)
    except StorageError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown store upload: {store_id}") from exc
    part = db.scalar(
        select(UploadPart).where(UploadPart.upload_id == upload.id, UploadPart.part_no == part_no)
    )
    if part is None:
        part = UploadPart(
            upload_id=upload.id, part_no=part_no, size_bytes=len(data), checksum_sha256=checksum
        )
        db.add(part)
    else:  # re-upload after an interruption replaces the previous part
        part.size_bytes = len(data)
        part.checksum_sha256 = checksum
    db.flush()
    return PartOut(part_no=part_no, size_bytes=len(data), checksum_sha256=checksum)


@router.get("/videos/uploads/{upload_id}")
def get_upload(
    upload_id: uuid.UUID,
    store_upload_id: Annotated[str, Query(min_length=1)],
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    _role: Annotated[Role, Depends(_ingest_roles)],
) -> UploadStatusOut:
    upload = db.get(UploadSession, upload_id)
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")
    store_id = _matching_store_id(upload, store_upload_id)
    if upload.completed:  # staging is gone; the DB rows are the record
        parts = [
            PartOut(part_no=p.part_no, size_bytes=p.size_bytes, checksum_sha256=p.checksum_sha256)
            for p in upload.parts
        ]
        return UploadStatusOut(
            upload_id=upload.id,
            declared_size=upload.declared_size,
            completed=True,
            received_parts=parts,
        )
    try:
        store_parts = store.list_parts(store_id)
    except StorageError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown store upload: {store_id}") from exc
    received = [
        PartOut(
            part_no=p.part_no,
            size_bytes=store_parts[p.part_no],
            checksum_sha256=p.checksum_sha256,
        )
        for p in upload.parts
        if p.part_no in store_parts  # only parts confirmed in BOTH places are resumable
    ]
    return UploadStatusOut(
        upload_id=upload.id,
        declared_size=upload.declared_size,
        completed=False,
        received_parts=received,
    )


@router.post("/videos/uploads/{upload_id}/complete")
def complete_upload(
    upload_id: uuid.UUID,
    payload: CompleteIn,
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    _role: Annotated[Role, Depends(_ingest_roles)],
) -> CompleteOut:
    upload = db.get(UploadSession, upload_id)
    if upload is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")
    if upload.completed:
        raise HTTPException(status.HTTP_409_CONFLICT, "upload already completed")
    store_id = _matching_store_id(upload, payload.store_upload_id)
    try:
        received = store.list_parts(store_id)
    except StorageError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown store upload: {store_id}") from exc
    missing = [n for n in range(1, payload.part_count + 1) if n not in received]
    if missing:
        raise HTTPException(status.HTTP_409_CONFLICT, f"cannot complete: missing parts {missing}")

    # DB-side conflict checks run BEFORE staging is consumed: a dedupe or a 409
    # must never destroy the parts a client needs to resume or retry (US-B2).
    duplicate = _video_with_checksum(db, upload, upload.declared_checksum)
    if duplicate is not None and _is_verified_copy(store, duplicate):
        store.abort_multipart(store_id)
        upload.completed = True
        db.flush()
        return CompleteOut(video=VideoOut.model_validate(duplicate), deduplicated=True)

    final_key = video_key(str(upload.session_id), upload.camera_id, upload.filename)
    clash = db.scalar(
        select(Video).where(
            Video.object_key == final_key, Video.checksum_sha256 != upload.declared_checksum
        )
    )
    if clash is not None:  # same filename, different content — never overwrite
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a different file was already uploaded for this camera and filename",
        )

    staging_key = f"staging/{upload.id.hex}/{upload.filename}"
    assembled_sha = store.complete_multipart(store_id, staging_key, payload.part_count)
    size_bytes = store.size(staging_key)
    upload.completed = True

    if assembled_sha != upload.declared_checksum:
        return _record_failed_assembly(db, store, upload, staging_key, assembled_sha, size_bytes)

    store.copy(staging_key, final_key)
    store.delete(staging_key)
    video = _upsert_video(db, upload, duplicate, final_key, assembled_sha, size_bytes)
    db.flush()
    _probe_and_grade(db, store, upload, video, final_key)
    _sync_session_capture_state(db, upload, video)
    return CompleteOut(video=VideoOut.model_validate(video), deduplicated=False)


@router.get("/sessions/{session_id}/videos")
def list_session_videos(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(_ingest_roles)],
) -> list[VideoOut]:
    if db.get(Session, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    videos = db.scalars(
        select(Video)
        .where(Video.session_id == session_id)
        .order_by(Video.camera_id, Video.filename)
    ).all()
    return [VideoOut.model_validate(v) for v in videos]


def _video_with_checksum(db: OrmSession, upload: UploadSession, checksum: str) -> Video | None:
    """The session's video row holding this exact content, whatever its status."""
    return db.scalar(
        select(Video).where(
            Video.session_id == upload.session_id, Video.checksum_sha256 == checksum
        )
    )


def _is_verified_copy(store: FsObjectStore, video: Video) -> bool:
    """True when the row counts as footage evidence AND its bytes are still stored.

    FAILED rows (bytes deleted) and evidence rows whose object was purged must
    not swallow a corrective re-upload via dedupe.
    """
    return video.status in EVIDENCE_STATUSES and store.exists(video.object_key)


def _record_failed_assembly(
    db: OrmSession,
    store: FsObjectStore,
    upload: UploadSession,
    staging_key: str,
    assembled_sha: str,
    size_bytes: int,
) -> CompleteOut:
    """The assembled bytes do not hash to the declaration: drop them, record FAILED.

    One escape hatch: when the bytes are an intact copy of content this session
    already stores (the *declaration* was wrong, not the file — proven by the
    checksum matching a verified row), answer with a dedupe instead.
    """
    store.delete(staging_key)
    twin = _video_with_checksum(db, upload, assembled_sha)
    if twin is not None and _is_verified_copy(store, twin):
        db.flush()
        return CompleteOut(video=VideoOut.model_validate(twin), deduplicated=True)
    video = _upsert_video(db, upload, twin, staging_key, assembled_sha, size_bytes)
    video.status = VideoStatus.FAILED
    video.error = (
        f"checksum mismatch: the assembled file hashes to {assembled_sha} but "
        f"{upload.declared_checksum} was declared; the upload is likely corrupted "
        "— please upload the file again"
    )
    db.flush()
    return CompleteOut(video=VideoOut.model_validate(video), deduplicated=False)


def _upsert_video(
    db: OrmSession,
    upload: UploadSession,
    existing: Video | None,
    object_key: str,
    checksum: str,
    size_bytes: int,
) -> Video:
    """Create the video row — or refresh a superseded row holding this checksum.

    (session_id, checksum_sha256) is unique: a FAILED attempt, or an evidence
    row whose object was purged, must be reused rather than duplicated when
    the same content is uploaded again.
    """
    if existing is None:
        video = _new_video(upload, object_key, checksum, size_bytes)
        db.add(video)
        return video
    existing.camera_id = upload.camera_id
    existing.object_key = object_key
    existing.filename = upload.filename
    existing.size_bytes = size_bytes
    existing.claimed_fps = upload.claimed_fps
    existing.claimed_resolution = upload.claimed_resolution
    existing.claimed_duration_s = upload.claimed_duration_s
    existing.codec = None
    existing.status = VideoStatus.UPLOADED
    existing.probe = None
    existing.error = None
    return existing


def _new_video(upload: UploadSession, object_key: str, checksum: str, size_bytes: int) -> Video:
    return Video(
        session_id=upload.session_id,
        camera_id=upload.camera_id,
        object_key=object_key,
        filename=upload.filename,
        checksum_sha256=checksum,
        size_bytes=size_bytes,
        claimed_fps=upload.claimed_fps,
        claimed_resolution=upload.claimed_resolution,
        claimed_duration_s=upload.claimed_duration_s,
        status=VideoStatus.UPLOADED,
    )


def _probe_and_grade(
    db: OrmSession, store: FsObjectStore, upload: UploadSession, video: Video, key: str
) -> None:
    """Probe the stored object and grade the video row against the claims.

    The object is streamed to a temp file (never loaded into memory whole). A
    file the probe cannot decode is corrupted: FAILED with a readable error
    (US-B2). A probe that could not run keeps the row UPLOADED for a retry.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".video") as handle:
            local_copy = Path(handle.name)
            store.write_to_path(key, local_copy)
            result = probe_video(local_copy)
    except ProbeUnavailableError as exc:  # environment problem — re-probe later
        video.error = f"probe unavailable: {exc}"
        db.flush()
        return
    except ProbeError as exc:
        video.status = VideoStatus.FAILED
        video.error = f"corrupted video, cannot be decoded: {exc} — please re-record or re-upload"
        db.flush()
        return
    video.probe = asdict(result)
    video.codec = result.codec
    conflicts = _metadata_conflicts(upload, result)
    if conflicts:
        video.status = VideoStatus.METADATA_CONFLICT
        video.error = "metadata conflict: " + "; ".join(conflicts)
    else:
        video.status = VideoStatus.PROBED
    db.flush()


def _sync_session_capture_state(db: OrmSession, upload: UploadSession, video: Video) -> None:
    """Footage landing after stop clears missing_views/degraded (US-A3 seam)."""
    if video.status not in EVIDENCE_STATUSES:
        return
    session = db.scalars(select(Session).where(Session.id == upload.session_id)).one()
    if session.state in _RECOMPUTE_STATES:
        recompute_missing_views(db, session)
        db.flush()


def _metadata_conflicts(upload: UploadSession, result: VideoProbe) -> list[str]:
    """Compare claimed metadata against probed values (fps ±1%, duration ±2%)."""
    conflicts: list[str] = []
    if (
        upload.claimed_fps is not None
        and abs(result.fps - upload.claimed_fps) > FPS_TOLERANCE * upload.claimed_fps
    ):
        conflicts.append(f"fps: claimed {upload.claimed_fps}, probed {result.fps}")
    if upload.claimed_resolution is not None and upload.claimed_resolution != result.resolution:
        conflicts.append(
            f"resolution: claimed {upload.claimed_resolution}, probed {result.resolution}"
        )
    if (
        upload.claimed_duration_s is not None
        and abs(result.duration_s - upload.claimed_duration_s)
        > DURATION_TOLERANCE * upload.claimed_duration_s
    ):
        conflicts.append(
            f"duration_s: claimed {upload.claimed_duration_s}, probed {result.duration_s}"
        )
    return conflicts
