"""Unit tests for the ``probe`` stage (US-J1): footage evidence, degraded-but-honest."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from cricai_data.db import create_all
from cricai_data.enums import BowlerSource, SessionType, VideoStatus
from cricai_data.models import Player, Video
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.probe_media import probe_session_media
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def ctx(tmp_path: Any) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed_session(ctx: WorkerContext, *, expected_cameras: list[str]) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            expected_cameras=expected_cameras,
        )
        db.add_all([player, session])
        db.commit()
        return session.id


def _add_video(
    ctx: WorkerContext, session_id: uuid.UUID, camera_id: str, status: VideoStatus
) -> None:
    with ctx.session_factory() as db:
        db.add(
            Video(
                session_id=session_id,
                camera_id=camera_id,
                object_key=f"vids/{session_id}/{camera_id}",
                filename=f"{camera_id}.mp4",
                checksum_sha256=f"{camera_id:_>64}",
                size_bytes=1,
                status=status,
            )
        )
        db.commit()


def test_missing_session_raises_lookup_error(ctx: WorkerContext) -> None:
    with pytest.raises(LookupError, match="not found"):
        probe_session_media(ctx, uuid.uuid4())


def test_no_expected_cameras_is_complete_and_not_degraded(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx, expected_cameras=[])
    out = probe_session_media(ctx, session_id)
    assert out == {
        "expected_cameras": [],
        "cameras_with_evidence": [],
        "missing_cameras": [],
        "video_count": 0,
        "evidence_count": 0,
        "video_checksums": [],
        "degraded": False,
    }


def test_all_expected_cameras_present_is_not_degraded(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx, expected_cameras=["C1", "C2"])
    _add_video(ctx, session_id, "C1", VideoStatus.UPLOADED)
    _add_video(ctx, session_id, "C2", VideoStatus.PROBED)
    out = probe_session_media(ctx, session_id)
    assert out["cameras_with_evidence"] == ["C1", "C2"]
    assert out["missing_cameras"] == []
    assert out["degraded"] is False
    assert out["evidence_count"] == 2
    assert out["video_checksums"] == [["C1", f"{'C1':_>64}"], ["C2", f"{'C2':_>64}"]]


def test_missing_and_non_evidence_cameras_are_reported_not_raised(ctx: WorkerContext) -> None:
    """A FAILED video is not evidence; a never-uploaded camera is missing — both honest."""
    session_id = _seed_session(ctx, expected_cameras=["C1", "C2", "C3"])
    _add_video(ctx, session_id, "C1", VideoStatus.METADATA_CONFLICT)  # still evidence
    _add_video(ctx, session_id, "C2", VideoStatus.FAILED)  # bytes gone: NOT evidence
    # C3 has no video row at all.
    out = probe_session_media(ctx, session_id)
    assert out["cameras_with_evidence"] == ["C1"]
    assert out["missing_cameras"] == ["C2", "C3"]
    assert out["video_count"] == 2  # both rows counted
    assert out["evidence_count"] == 1  # only C1 proves usable footage
    assert [camera for camera, _ in out["video_checksums"]] == ["C1"]  # evidence only
    assert out["degraded"] is True


def test_payload_tracks_the_stored_byte_identity(ctx: WorkerContext) -> None:
    """Phase-6 finding 5: the probe payload — the digest currency downstream
    media stages resume against — must change when a video's bytes are
    replaced in place (same row, same status, new checksum)."""
    session_id = _seed_session(ctx, expected_cameras=["C1"])
    _add_video(ctx, session_id, "C1", VideoStatus.PROBED)
    before = probe_session_media(ctx, session_id)
    with ctx.session_factory() as db:
        video = db.scalars(select(Video).where(Video.session_id == session_id)).one()
        video.checksum_sha256 = "f" * 64
        db.commit()
    after = probe_session_media(ctx, session_id)
    assert before != after  # the output digest downstream resumes on now differs
    assert after["video_checksums"] == [["C1", "f" * 64]]
