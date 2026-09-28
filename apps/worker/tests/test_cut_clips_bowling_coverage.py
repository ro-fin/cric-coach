"""US-I1 acceptance: bowling-mode ball events clip C5-C7 alongside C2/C3.

The clip stage (US-D2) iterates ``expected_cameras | evidence videos`` with no
per-role special casing, so bowling-side cameras ride the existing pipeline —
these tests prove that by deriving the coverage expectation from the camera
registry's roles (US-I1) rather than hard-coding ids. ``cut_clips`` itself is
deliberately not modified by the bowling story.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    CameraRole,
    ClipStatus,
    SessionType,
    VideoStatus,
)
from cricai_data.models import BallEvent, CameraConfig, Clip, Player, Session, Video
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.cut_clips import cut_session_clips
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

#: The US-I1 bowling trio: registry role + capture fps per camera.
BOWLING_ROLES: dict[str, tuple[CameraRole, int]] = {
    "C5": (CameraRole.BOWLING_SIDE, 120),
    "C6": (CameraRole.FRONT_ON, 120),
    "C7": (CameraRole.WRIST, 240),
}

#: A bowling-capture role means the camera feeds the leg-spin pipeline.
BOWLING_CAPTURE_ROLES = frozenset({CameraRole.BOWLING_SIDE, CameraRole.FRONT_ON, CameraRole.WRIST})

BALLS = ((1, 1000, 2000), (2, 10000, 14000))


class FakeRunner:
    """Simulates ffmpeg by writing the dest file (same as test_cut_clips)."""

    def __call__(self, argv: list[str]) -> None:
        Path(argv[-1]).write_bytes(b"clip:" + Path(argv[-1]).name.encode())


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )


def _add_video(db: OrmSession, session_id: uuid.UUID, camera_id: str) -> None:
    db.add(
        Video(
            session_id=session_id,
            camera_id=camera_id,
            object_key=f"sessions/{session_id}/{camera_id}/a.mp4",
            filename=f"{camera_id}-a.mp4",
            checksum_sha256=f"{camera_id}-a".ljust(64, "0"),
            size_bytes=1024,
            status=VideoStatus.UPLOADED,
            created_at=datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
        )
    )


def _seed_bowling_session(ctx: WorkerContext, *, footage_for: tuple[str, ...]) -> uuid.UUID:
    """A 5-camera bowling session (C2/C3 batting-era cams + the C5-C7 trio)
    with the trio registered under their US-I1 roles."""
    with ctx.session_factory() as db:
        for camera_id, (camera_role, fps) in BOWLING_ROLES.items():
            db.add(
                CameraConfig(
                    camera_id=camera_id,
                    era_no=1,
                    active=True,
                    role=camera_role,
                    position_label=f"{camera_role.value} of the bowling crease",
                    xyz_offset_m={"x": 5.0, "y": 0.0, "z": 1.2},
                    height_m=1.2,
                    fps=fps,
                    resolution="1920x1080",
                    protected=camera_role is CameraRole.WRIST,
                )
            )
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
            expected_cameras=["C2", "C3", *BOWLING_ROLES],
        )
        db.add(session)
        db.flush()
        for camera_id in footage_for:
            _add_video(db, session.id, camera_id)
        for ball_no, start_ms, end_ms in BALLS:
            db.add(
                BallEvent(
                    session_id=session.id,
                    ball_no=ball_no,
                    start_ms=start_ms,
                    release_ms=start_ms + 200,
                    contact_ms=None,
                    end_ms=end_ms,
                    confidence=0.9,
                    valid=True,
                )
            )
        db.commit()
        return session.id


def _bowling_capture_cameras(ctx: WorkerContext) -> set[str]:
    """Registry-driven expectation: every active camera with a bowling role."""
    with ctx.session_factory() as db:
        rows = db.scalars(
            select(CameraConfig.camera_id).where(
                CameraConfig.active.is_(True),
                CameraConfig.role.in_(sorted(BOWLING_CAPTURE_ROLES)),
            )
        ).all()
        return set(rows)


def _clips(ctx: WorkerContext, session_id: uuid.UUID) -> dict[tuple[int, str], Clip]:
    with ctx.session_factory() as db:
        rows = db.scalars(select(Clip).where(Clip.session_id == session_id)).all()
        return {(clip.ball_no, clip.camera_id): clip for clip in rows}


def _static_resolver(tmp_path: Path) -> Callable[[Video], Path]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")

    def resolve(video: Video) -> Path:
        return source

    return resolve


def test_bowling_role_cameras_are_clipped_per_ball(ctx: WorkerContext, tmp_path: Path) -> None:
    """US-I1 IT: every role=bowling_side/front_on/wrist camera in the roster
    gets a CUT clip per ball under the pinned key layout, alongside C2/C3."""
    session_id = _seed_bowling_session(ctx, footage_for=("C2", "C3", "C5", "C6", "C7"))

    summary = cut_session_clips(
        ctx, session_id, source_resolver=_static_resolver(tmp_path), runner=FakeRunner()
    )

    bowling_cameras = _bowling_capture_cameras(ctx)
    assert bowling_cameras == {"C5", "C6", "C7"}
    assert bowling_cameras < set(summary.cameras)
    assert summary.cut == 10  # 2 balls x 5 cameras
    clips = _clips(ctx, session_id)
    for ball_no, _, _ in BALLS:
        for camera_id in bowling_cameras | {"C2", "C3"}:
            clip = clips[(ball_no, camera_id)]
            assert clip.status is ClipStatus.CUT
            assert clip.object_key == f"sessions/{session_id}/balls/{ball_no}/{camera_id}.mp4"
            assert ctx.store.exists(clip.object_key)


def test_missing_bowling_cam_is_a_loud_gap(ctx: WorkerContext, tmp_path: Path) -> None:
    """US-I1/US-D2: an expected bowling-side camera without footage produces
    GAP rows naming the camera — never a silent absence."""
    session_id = _seed_bowling_session(ctx, footage_for=("C2", "C3", "C6", "C7"))

    cut_session_clips(
        ctx, session_id, source_resolver=_static_resolver(tmp_path), runner=FakeRunner()
    )

    clips = _clips(ctx, session_id)
    for ball_no, _, _ in BALLS:
        gap = clips[(ball_no, "C5")]
        assert gap.status is ClipStatus.GAP
        assert gap.object_key is None
        assert gap.error is not None
        assert "C5" in gap.error
