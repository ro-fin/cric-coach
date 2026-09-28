"""US-E1 pose extraction job tests: fan-out, pinned keys, idempotent upsert, flags."""

import datetime
import json
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import BowlerSource, EventSource, SessionType
from cricai_data.models import BallEvent, Player, Session
from cricai_data.models import PoseTrack as PoseTrackRow
from cricai_data.storage import FsObjectStore
from cricai_vision.pose import FakePoseProvider, PoseTrack, track_from_payload
from cricai_worker.context import WorkerContext
from cricai_worker.extract_pose import DEFAULT_CAMERAS, extract_session_pose, pose_key
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

#: FakePoseProvider only looks at len(frames); real providers get ndarrays.
FRAMES: list[bytes] = [b"frame"] * 8


def _context(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed_session(
    ctx: WorkerContext,
    *,
    valid_balls: tuple[int, ...] = (1, 2),
    rejected_balls: tuple[int, ...] = (),
) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.flush()
        for ball_no in (*valid_balls, *rejected_balls):
            db.add(
                BallEvent(
                    session_id=session.id,
                    ball_no=ball_no,
                    start_ms=ball_no * 10_000,
                    release_ms=ball_no * 10_000 + 400,
                    end_ms=ball_no * 10_000 + 4_000,
                    confidence=0.9,
                    source=EventSource.AUTO,
                    detector_version="det-1",
                    valid=ball_no not in rejected_balls,
                )
            )
        db.commit()
        return session.id


def _all_footage(
    session_id: uuid.UUID, ball_no: int, camera_id: str
) -> tuple[Sequence[bytes], float]:
    return (FRAMES, 120.0)


def test_extracts_tracks_for_valid_events_and_cameras(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2), rejected_balls=(3,))
    summary = extract_session_pose(
        ctx, session_id, provider=FakePoseProvider(), frames_resolver=_all_footage
    )
    assert summary.session_id == str(session_id)
    assert summary.extracted == 4
    assert summary.missing == ()
    assert summary.low_availability == ()
    with ctx.session_factory() as db:
        rows = (
            db.execute(select(PoseTrackRow).order_by(PoseTrackRow.ball_no, PoseTrackRow.camera_id))
            .scalars()
            .all()
        )
    # Rejected ball 3 gets no pose row; every valid ball links to one row per camera.
    assert [(r.ball_no, r.camera_id) for r in rows] == [
        (1, "C1"),
        (1, "C3"),
        (2, "C1"),
        (2, "C3"),
    ]
    for row in rows:
        assert row.session_id == session_id
        assert row.model_name == "fake-pose"
        assert row.model_version == "1"
        assert row.landmarks_key == pose_key(session_id, row.ball_no, row.camera_id)
        assert row.frame_count == len(FRAMES)
        assert row.availability == 1.0
        assert row.subject_confidence == 0.99
        track = track_from_payload(json.loads(ctx.store.get(row.landmarks_key)))
        assert track.frame_count == row.frame_count
        assert track.model_name == row.model_name


def test_missing_footage_is_reported_not_fabricated(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2))

    def only_c1(
        session_id: uuid.UUID, ball_no: int, camera_id: str
    ) -> tuple[Sequence[bytes], float] | None:
        return (FRAMES, 120.0) if camera_id == "C1" else None

    summary = extract_session_pose(
        ctx, session_id, provider=FakePoseProvider(), frames_resolver=only_c1
    )
    assert summary.extracted == 2
    assert summary.missing == ((1, "C3"), (2, "C3"))
    with ctx.session_factory() as db:
        rows = db.execute(select(PoseTrackRow)).scalars().all()
    assert {(r.ball_no, r.camera_id) for r in rows} == {(1, "C1"), (2, "C1")}
    assert not ctx.store.exists(pose_key(session_id, 1, "C3"))


def test_rerun_upserts_rows_and_overwrites_payloads(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    first = extract_session_pose(
        ctx,
        session_id,
        provider=FakePoseProvider(visibility=0.9),
        frames_resolver=_all_footage,
        cameras=("C1",),
    )
    key = pose_key(session_id, 1, "C1")
    first_payload = ctx.store.get(key)
    second = extract_session_pose(
        ctx,
        session_id,
        provider=FakePoseProvider(visibility=0.7),
        frames_resolver=_all_footage,
        cameras=("C1",),
    )
    assert first.extracted == second.extracted == 1
    with ctx.session_factory() as db:
        rows = db.execute(select(PoseTrackRow)).scalars().all()
    assert len(rows) == 1  # upsert on the unique triple, never a duplicate row
    second_payload = ctx.store.get(key)
    assert second_payload != first_payload
    track = track_from_payload(json.loads(second_payload))
    assert track.frames[0].landmarks[0].visibility == 0.7


class _CrashOnCall:
    """Delegates to FakePoseProvider until the Nth extract call, then raises.

    Its tracks carry a DIFFERENT model_version ("2") and a sub-bar visibility
    (below PoseTrack.availability's 0.5 median bar), so every row-visible field
    a crashed re-run writes differs from the previous run's committed rows —
    a stale run-1 row left describing an overwritten run-2 payload cannot pass
    the row-metadata-vs-payload assertions below (it did when both runs wrote
    identical model_version/availability).
    """

    model_name = "fake-pose"
    model_version = "2"

    def __init__(self, *, crash_on_call: int, visibility: float) -> None:
        self._inner = FakePoseProvider(visibility=visibility)
        self._crash_on_call = crash_on_call
        self._calls = 0

    def extract(self, frames: Sequence[bytes], *, fps: float) -> PoseTrack:
        self._calls += 1
        if self._calls == self._crash_on_call:
            raise RuntimeError("pose model crashed")
        return replace(self._inner.extract(frames, fps=fps), model_version=self.model_version)


def test_mid_run_crash_keeps_every_ball_row_and_payload_consistent(tmp_path: Path) -> None:
    """Each ball's store write + row upsert commit together (crash-safe re-runs)."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2))
    extract_session_pose(
        ctx,
        session_id,
        provider=FakePoseProvider(visibility=0.9),
        frames_resolver=_all_footage,
        cameras=("C1",),
    )
    crashing = _CrashOnCall(crash_on_call=2, visibility=0.4)
    with pytest.raises(RuntimeError, match="pose model crashed"):
        extract_session_pose(
            ctx, session_id, provider=crashing, frames_resolver=_all_footage, cameras=("C1",)
        )
    with ctx.session_factory() as db:
        rows = db.execute(select(PoseTrackRow).order_by(PoseTrackRow.ball_no)).scalars().all()
    assert [(r.ball_no, r.camera_id) for r in rows] == [(1, "C1"), (2, "C1")]
    # Ball 1: the re-run's payload landed WITH its row (committed before the
    # crash) — the row now shows run 2's model_version and 0.0 availability.
    # Ball 2: untouched by the failed re-run — still the first run's payload + row.
    assert [(r.model_version, r.availability) for r in rows] == [("2", 0.0), ("1", 1.0)]
    for row, visibility in zip(rows, (0.4, 0.9), strict=True):
        track = track_from_payload(json.loads(ctx.store.get(row.landmarks_key)))
        assert track.frames[0].landmarks[0].visibility == visibility
        # Committed row metadata always describes the stored payload bytes.
        assert row.model_name == track.model_name
        assert row.model_version == track.model_version
        assert row.frame_count == track.frame_count
        assert row.availability == track.availability()
        assert row.subject_confidence == track.subject_confidence


def test_low_availability_tracks_flagged_never_padded(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    summary = extract_session_pose(
        ctx,
        session_id,
        provider=FakePoseProvider(visibility=0.2),
        frames_resolver=_all_footage,
        cameras=("C1",),
    )
    assert summary.extracted == 1
    assert summary.low_availability == ((1, "C1"),)
    with ctx.session_factory() as db:
        row = db.execute(select(PoseTrackRow)).scalars().one()
    assert row.availability == 0.0
    assert row.frame_count == len(FRAMES)  # stored as observed, never padded


def test_unknown_session_raises(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="session not found"):
        extract_session_pose(
            ctx, uuid.uuid4(), provider=FakePoseProvider(), frames_resolver=_all_footage
        )


def test_pose_key_layout_is_pinned() -> None:
    sid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert pose_key(sid, 12, "C1") == f"sessions/{sid}/balls/12/pose-C1.json"
    with pytest.raises(ValueError, match="ball_no"):
        pose_key(sid, 0, "C1")


def test_default_cameras_reference_first() -> None:
    assert DEFAULT_CAMERAS == ("C1", "C3")
