"""US-F1 frame sampling job: strata, provenance, idempotent upsert, injected writer."""

import datetime
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    EventSource,
    SessionType,
    VideoStatus,
)
from cricai_data.models import BallEvent, FrameSample, Player, SessionBlock, Video
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_vision.ffmpeg import FfmpegError, FfmpegUnavailableError
from cricai_worker.context import WorkerContext
from cricai_worker.sample_frames import (
    SAMPLER_VERSION,
    FrameSamplingError,
    build_frame_command,
    ffmpeg_frame_writer,
    frame_key,
    lighting_stratum,
    sample_session_frames,
    speed_band,
)
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

UTC = datetime.UTC

#: Ball n occupies [n*10_000, n*10_000 + 3_000] ms; at 120 fps the default three
#: samples land on frames n*1200, n*1200+180, n*1200+360.
BALL_WINDOW_MS = 3_000

TEN_AM = datetime.datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

_PROBE_120: dict[str, Any] = {"fps": 120.0}


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
    valid_balls: tuple[int, ...] = (1,),
    rejected_balls: tuple[int, ...] = (),
    started_at: datetime.datetime | None = TEN_AM,
    machine_settings: dict[str, Any] | None = None,
) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 8),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            machine_settings=machine_settings,
            started_at=started_at,
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
                    end_ms=ball_no * 10_000 + BALL_WINDOW_MS,
                    confidence=0.9,
                    source=EventSource.AUTO,
                    detector_version="det-1",
                    valid=ball_no not in rejected_balls,
                )
            )
        db.commit()
        return session.id


def _seed_video(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    camera_id: str,
    *,
    status: VideoStatus = VideoStatus.PROBED,
    claimed_fps: float | None = None,
    claimed_duration_s: float | None = None,
    probe: dict[str, Any] | None = _PROBE_120,
) -> None:
    with ctx.session_factory() as db:
        db.add(
            Video(
                session_id=session_id,
                camera_id=camera_id,
                object_key=f"sessions/{session_id}/{camera_id}/full.mp4",
                filename="full.mp4",
                checksum_sha256=uuid.uuid4().hex * 2,
                size_bytes=1000,
                claimed_fps=claimed_fps,
                claimed_duration_s=claimed_duration_s,
                status=status,
                probe=probe,
            )
        )
        db.commit()


def _seed_block(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    block_no: int,
    start_s: float,
    end_s: float | None,
    intent: BlockIntent,
) -> None:
    with ctx.session_factory() as db:
        db.add(
            SessionBlock(
                session_id=session_id,
                block_no=block_no,
                start_s=start_s,
                end_s=end_s,
                bowler_source=BowlerSource.MACHINE,
                intent=intent,
            )
        )
        db.commit()


@dataclass
class RecordingWriter:
    """Fake FrameWriter: records calls and stores fake JPEG bytes."""

    store: FsObjectStore
    calls: list[tuple[str, int, str]] = field(default_factory=list)

    def __call__(self, video: Video, ts_ms: int, key: str) -> None:
        self.calls.append((video.camera_id, ts_ms, key))
        self.store.put(key, b"jpeg")


def _rows(ctx: WorkerContext, session_id: uuid.UUID) -> list[FrameSample]:
    with ctx.session_factory() as db:
        return list(
            db.scalars(
                select(FrameSample)
                .where(FrameSample.session_id == session_id)
                .order_by(FrameSample.camera_id, FrameSample.frame_no)
            )
        )


# --- happy path -----------------------------------------------------------------


def test_samples_frames_with_provenance_and_strata(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(
        ctx,
        valid_balls=(1, 2),
        rejected_balls=(3,),
        machine_settings={"speed_kph": 92},
    )
    _seed_video(ctx, session_id, "C1")
    _seed_block(ctx, session_id, 1, 0.0, 15.0, BlockIntent.TECHNICAL)
    _seed_block(ctx, session_id, 2, 15.0, None, BlockIntent.DECISION)
    writer = RecordingWriter(ctx.store)
    summary = sample_session_frames(ctx, session_id, frame_writer=writer)

    assert summary.session_id == str(session_id)
    assert summary.sampled == 6  # 2 valid balls x 3 frames; rejected ball 3 skipped
    assert summary.skipped == 0
    assert summary.missing_cameras == ()
    assert summary.unusable_cameras == ()
    rows = _rows(ctx, session_id)
    assert [(r.ball_no, r.frame_no, r.ts_ms) for r in rows] == [
        (1, 1200, 10_000),
        (1, 1380, 11_500),
        (1, 1560, 13_000),
        (2, 2400, 20_000),
        (2, 2580, 21_500),
        (2, 2760, 23_000),
    ]
    for row in rows:
        assert row.camera_id == "C1"
        assert row.sampler_version == SAMPLER_VERSION
        assert row.object_key == frame_key(session_id, "C1", row.frame_no)
        assert ctx.store.get(row.object_key) == b"jpeg"
    # Ball 1 (10s) sits in the technical block; ball 2 (20s) in the open decision block.
    tech = {"lighting": "daylight", "speed_band": "medium", "intent": "technical"}
    decision = {"lighting": "daylight", "speed_band": "medium", "intent": "decision"}
    assert rows[0].stratum == tech
    assert rows[3].stratum == decision
    assert summary.strata == tuple(
        sorted([(json.dumps(tech, sort_keys=True), 3), (json.dumps(decision, sort_keys=True), 3)])
    )
    assert [c[0] for c in writer.calls] == ["C1"] * 6


def test_single_frame_per_ball_samples_the_midpoint(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1")
    writer = RecordingWriter(ctx.store)
    summary = sample_session_frames(ctx, session_id, frame_writer=writer, frames_per_ball=1)
    assert summary.sampled == 1
    (row,) = _rows(ctx, session_id)
    assert row.ts_ms == 11_500  # midpoint of [10_000, 13_000]


def test_low_fps_collisions_deduplicate(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1", probe={"fps": 0.5})  # 2s per frame
    writer = RecordingWriter(ctx.store)
    summary = sample_session_frames(ctx, session_id, frame_writer=writer)
    # Three samples over a 3s window at 0.5 fps map to frames 5, 6, 6 -> two rows.
    assert summary.sampled == 2
    assert [r.frame_no for r in _rows(ctx, session_id)] == [5, 6]


def test_multiple_cameras_and_explicit_camera_selection(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1")
    _seed_video(ctx, session_id, "C3")
    writer = RecordingWriter(ctx.store)
    summary = sample_session_frames(ctx, session_id, frame_writer=writer)
    assert summary.sampled == 6  # both evidence cameras by default
    ctx2 = _context(tmp_path / "second")
    session2 = _seed_session(ctx2)
    _seed_video(ctx2, session2, "C1")
    _seed_video(ctx2, session2, "C3")
    writer2 = RecordingWriter(ctx2.store)
    summary2 = sample_session_frames(ctx2, session2, frame_writer=writer2, cameras=("C3", "C9"))
    assert summary2.sampled == 3  # C1 not requested
    assert summary2.missing_cameras == ("C9",)  # requested but no footage: loud
    assert {r.camera_id for r in _rows(ctx2, session2)} == {"C3"}


# --- loud gaps ------------------------------------------------------------------


def test_camera_without_evidence_footage_is_reported(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1", status=VideoStatus.FAILED)  # bytes deleted
    writer = RecordingWriter(ctx.store)
    summary = sample_session_frames(ctx, session_id, frame_writer=writer, cameras=("C1",))
    assert summary.sampled == 0
    assert summary.missing_cameras == ("C1",)
    assert writer.calls == []


def test_camera_without_usable_fps_is_reported(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1", probe=None, claimed_fps=None)
    writer = RecordingWriter(ctx.store)
    summary = sample_session_frames(ctx, session_id, frame_writer=writer)
    assert summary.sampled == 0
    assert summary.unusable_cameras == ("C1",)
    assert _rows(ctx, session_id) == []


def test_probe_fps_wins_over_claimed_and_claimed_is_fallback(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1", probe={"fps": 120.0}, claimed_fps=60.0)
    writer = RecordingWriter(ctx.store)
    sample_session_frames(ctx, session_id, frame_writer=writer, frames_per_ball=1)
    (row,) = _rows(ctx, session_id)
    assert row.frame_no == 1380  # 11.5s at 120 fps, not 690 at the claimed 60
    ctx2 = _context(tmp_path / "second")
    session2 = _seed_session(ctx2)
    _seed_video(ctx2, session2, "C1", probe={"fps": "fast"}, claimed_fps=60.0)
    writer2 = RecordingWriter(ctx2.store)
    sample_session_frames(ctx2, session2, frame_writer=writer2, frames_per_ball=1)
    (row2,) = _rows(ctx2, session2)
    assert row2.frame_no == 690  # non-numeric probe fps: claimed wins


def test_zero_fps_is_unusable_not_a_crash(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1", probe=None, claimed_fps=0.0)
    summary = sample_session_frames(ctx, session_id, frame_writer=RecordingWriter(ctx.store))
    assert summary.unusable_cameras == ("C1",)


# --- idempotency + crash safety ---------------------------------------------------


def test_rerun_skips_existing_frames_and_never_duplicates(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2))
    _seed_video(ctx, session_id, "C1")
    first_writer = RecordingWriter(ctx.store)
    first = sample_session_frames(ctx, session_id, frame_writer=first_writer)
    second_writer = RecordingWriter(ctx.store)
    second = sample_session_frames(ctx, session_id, frame_writer=second_writer)
    assert first.sampled == 6
    assert (second.sampled, second.skipped) == (0, 6)
    assert second_writer.calls == []  # nothing rewritten
    assert len(_rows(ctx, session_id)) == 6  # upsert on the unique triple
    assert first.strata == second.strata  # skipped rows still audited


def test_missing_object_is_rewritten_on_rerun(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1")
    sample_session_frames(ctx, session_id, frame_writer=RecordingWriter(ctx.store))
    lost = frame_key(session_id, "C1", 1380)
    assert ctx.store.delete(lost)
    writer = RecordingWriter(ctx.store)
    summary = sample_session_frames(ctx, session_id, frame_writer=writer)
    assert (summary.sampled, summary.skipped) == (1, 2)
    assert [c[2] for c in writer.calls] == [lost]
    assert ctx.store.get(lost) == b"jpeg"


@dataclass
class CrashingWriter:
    """Delegates to RecordingWriter until the Nth call, then raises."""

    inner: RecordingWriter
    crash_on_call: int
    calls: int = 0

    def __call__(self, video: Video, ts_ms: int, key: str) -> None:
        self.calls += 1
        if self.calls == self.crash_on_call:
            raise RuntimeError("frame extraction crashed")
        self.inner(video, ts_ms, key)


def test_mid_run_crash_keeps_committed_frames_and_resumes(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2))
    _seed_video(ctx, session_id, "C1")
    crashing = CrashingWriter(inner=RecordingWriter(ctx.store), crash_on_call=4)
    with pytest.raises(RuntimeError, match="frame extraction crashed"):
        sample_session_frames(ctx, session_id, frame_writer=crashing)
    rows = _rows(ctx, session_id)
    assert [r.frame_no for r in rows] == [1200, 1380, 1560]  # ball 1 landed durably
    for row in rows:
        assert ctx.store.exists(row.object_key)  # committed rows describe stored bytes
    resume_writer = RecordingWriter(ctx.store)
    resumed = sample_session_frames(ctx, session_id, frame_writer=resume_writer)
    assert (resumed.sampled, resumed.skipped) == (3, 3)
    assert len(_rows(ctx, session_id)) == 6


# --- per-frame containment + footage clamp -------------------------------------------


def test_picks_beyond_footage_end_are_dropped_loudly(tmp_path: Path) -> None:
    """A camera that stopped recording before the ball window ends must not wedge
    the job: picks past the known footage duration are never attempted."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)  # ball 1 window [10_000, 13_000]
    _seed_video(ctx, session_id, "C1", probe={"fps": 120.0, "duration_s": 12.0})

    def eof_writer(video: Video, ts_ms: int, key: str) -> None:
        if ts_ms >= 12_000:  # what real ffmpeg does past EOF: no output, loud error
            raise FrameSamplingError(f"ffmpeg reported success but produced no frame for {key}")
        ctx.store.put(key, b"jpeg")

    summary = sample_session_frames(ctx, session_id, frame_writer=eof_writer)
    assert (summary.sampled, summary.beyond_footage, summary.failed) == (2, 1, 0)
    assert [r.ts_ms for r in _rows(ctx, session_id)] == [10_000, 11_500]
    # Re-runs stay stable: the unreachable pick is dropped again, never retried.
    resumed = sample_session_frames(ctx, session_id, frame_writer=eof_writer)
    assert (resumed.sampled, resumed.skipped, resumed.beyond_footage) == (0, 2, 1)


def test_claimed_duration_clamps_when_probe_has_none(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(
        ctx,
        session_id,
        "C1",
        probe={"fps": 120.0, "duration_s": "long"},  # non-numeric probe duration
        claimed_duration_s=12.0,
    )
    summary = sample_session_frames(ctx, session_id, frame_writer=RecordingWriter(ctx.store))
    assert (summary.sampled, summary.beyond_footage) == (2, 1)  # claimed clamp wins


def test_one_unextractable_frame_does_not_abort_the_run(tmp_path: Path) -> None:
    """A per-frame extraction failure is contained (cut_clips pattern): the rest
    of the camera, the other cameras, and re-runs all proceed."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1")
    _seed_video(ctx, session_id, "C3")
    inner = RecordingWriter(ctx.store)
    calls = 0

    def flaky_writer(video: Video, ts_ms: int, key: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FrameSamplingError(f"ffmpeg reported success but produced no frame for {key}")
        inner(video, ts_ms, key)

    summary = sample_session_frames(ctx, session_id, frame_writer=flaky_writer)
    assert (summary.sampled, summary.failed) == (5, 1)
    rows = _rows(ctx, session_id)
    assert sum(1 for r in rows if r.camera_id == "C3") == 3  # later camera still sampled
    # The failed frame heals on re-run (idempotent + crash-safe claim holds).
    resumed = sample_session_frames(ctx, session_id, frame_writer=RecordingWriter(ctx.store))
    assert (resumed.sampled, resumed.skipped, resumed.failed) == (1, 5, 0)
    assert len(_rows(ctx, session_id)) == 6


def test_ffmpeg_error_is_contained_per_frame(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1")
    inner = RecordingWriter(ctx.store)
    calls = 0

    def bad_source_writer(video: Video, ts_ms: int, key: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FfmpegError("ffmpeg exited with code 1")
        inner(video, ts_ms, key)

    summary = sample_session_frames(ctx, session_id, frame_writer=bad_source_writer)
    assert (summary.sampled, summary.failed) == (2, 1)


def test_ffmpeg_unavailable_still_aborts_the_run(tmp_path: Path) -> None:
    # Infra unavailability is retryable, not a property of the footage: abort loudly.
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_video(ctx, session_id, "C1")

    def no_ffmpeg(video: Video, ts_ms: int, key: str) -> None:
        raise FfmpegUnavailableError("ffmpeg not found: install ffmpeg or set PATH")

    with pytest.raises(FfmpegUnavailableError):
        sample_session_frames(ctx, session_id, frame_writer=no_ffmpeg)
    assert _rows(ctx, session_id) == []


# --- validation ---------------------------------------------------------------------


def test_unknown_session_raises(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="session not found"):
        sample_session_frames(ctx, uuid.uuid4(), frame_writer=RecordingWriter(ctx.store))


def test_invalid_frames_per_ball_raises(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(FrameSamplingError, match="frames_per_ball"):
        sample_session_frames(
            ctx, uuid.uuid4(), frame_writer=RecordingWriter(ctx.store), frames_per_ball=0
        )


def test_frame_key_layout_is_pinned() -> None:
    sid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert frame_key(sid, "C1", 42) == f"sessions/{sid}/frames/C1/frame-000042.jpg"
    with pytest.raises(FrameSamplingError, match="frame_no"):
        frame_key(sid, "C1", -1)


# --- strata helpers -------------------------------------------------------------------


def test_lighting_stratum_rules() -> None:
    ten_am = datetime.datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    nine_pm = datetime.datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
    assert lighting_stratum(ten_am, None) == "daylight"
    assert lighting_stratum(nine_pm, None) == "artificial"
    assert lighting_stratum(None, None) == "unknown"
    # Explicit metadata beats the clock; blank/non-string tags are ignored.
    assert lighting_stratum(ten_am, {"lighting": "floodlit"}) == "floodlit"
    assert lighting_stratum(ten_am, {"lighting": ""}) == "daylight"
    assert lighting_stratum(None, {"lighting": 3}) == "unknown"


def test_speed_band_rules() -> None:
    assert speed_band(None) == "unknown"
    assert speed_band({}) == "unknown"
    assert speed_band({"speed_kph": "quick"}) == "unknown"
    assert speed_band({"speed_kph": True}) == "unknown"  # bool is not a speed
    assert speed_band({"speed_kph": 79.9}) == "slow"
    assert speed_band({"speed_kph": 80}) == "medium"
    assert speed_band({"speed_kph": 105.0}) == "fast"


def test_ball_outside_every_block_gets_unknown_intent(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)  # ball 1 starts at 10s
    _seed_video(ctx, session_id, "C1")
    _seed_block(ctx, session_id, 1, 20.0, 30.0, BlockIntent.FUN)  # starts after the ball
    sample_session_frames(
        ctx, session_id, frame_writer=RecordingWriter(ctx.store), frames_per_ball=1
    )
    (row,) = _rows(ctx, session_id)
    assert row.stratum["intent"] == "unknown"


def test_no_started_at_and_no_settings_stratum_is_unknown(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, started_at=None)
    _seed_video(ctx, session_id, "C1")
    sample_session_frames(
        ctx, session_id, frame_writer=RecordingWriter(ctx.store), frames_per_ball=1
    )
    (row,) = _rows(ctx, session_id)
    assert row.stratum == {"lighting": "unknown", "speed_band": "unknown", "intent": "unknown"}


# --- ffmpeg default writer (argv construction only; never a real ffmpeg) --------------


def test_build_frame_command_argv() -> None:
    argv = build_frame_command(Path("/src/full.mp4"), Path("/tmp/f.jpg"), 11_500)
    assert argv == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        "11.500",
        "-i",
        "/src/full.mp4",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "/tmp/f.jpg",
    ]
    custom = build_frame_command(Path("s.mp4"), Path("d.jpg"), 0, ffmpeg="/opt/ffmpeg")
    assert custom[0] == "/opt/ffmpeg"
    with pytest.raises(FrameSamplingError, match="ts_ms"):
        build_frame_command(Path("s.mp4"), Path("d.jpg"), -1)


def _video_row(session_id: uuid.UUID) -> Video:
    return Video(
        id=uuid.uuid4(),
        session_id=session_id,
        camera_id="C1",
        object_key=f"sessions/{session_id}/C1/full.mp4",
        filename="full.mp4",
        checksum_sha256="0" * 64,
        size_bytes=4,
        status=VideoStatus.PROBED,
    )


def test_ffmpeg_frame_writer_stores_the_extracted_frame(tmp_path: Path) -> None:
    store = FsObjectStore(tmp_path / "store")
    video = _video_row(uuid.uuid4())
    store.put(video.object_key, b"mp4-bytes")  # default resolver streams from the store
    ran: list[list[str]] = []

    def fake_runner(argv: list[str]) -> None:
        ran.append(argv)
        Path(argv[-1]).write_bytes(b"frame-jpeg")

    writer = ffmpeg_frame_writer(store, tmp_path / "work", runner=fake_runner)
    writer(video, 11_500, "sessions/s/frames/C1/frame-001380.jpg")
    assert store.get("sessions/s/frames/C1/frame-001380.jpg") == b"frame-jpeg"
    (argv,) = ran
    assert argv[:6] == ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", "11.500"]
    assert not list((tmp_path / "work").glob("*.jpg"))  # temp frame cleaned up


def test_ffmpeg_frame_writer_with_injected_resolver(tmp_path: Path) -> None:
    store = FsObjectStore(tmp_path / "store")
    source = tmp_path / "local.mp4"
    source.write_bytes(b"mp4")
    resolved: list[str] = []

    def resolver(video: Video) -> Path:
        resolved.append(video.camera_id)
        return source

    def fake_runner(argv: list[str]) -> None:
        assert argv[7] == str(source)  # -i <resolved source>
        Path(argv[-1]).write_bytes(b"frame")

    writer = ffmpeg_frame_writer(
        store, tmp_path / "work", source_resolver=resolver, runner=fake_runner
    )
    writer(_video_row(uuid.uuid4()), 0, "k.jpg")
    assert resolved == ["C1"]
    assert store.get("k.jpg") == b"frame"


def test_ffmpeg_frame_writer_no_output_is_loud(tmp_path: Path) -> None:
    store = FsObjectStore(tmp_path / "store")

    def silent_runner(argv: Sequence[str]) -> None:
        return None  # "succeeds" but writes nothing

    writer = ffmpeg_frame_writer(
        store, tmp_path / "work", source_resolver=lambda _video: Path("s.mp4"), runner=silent_runner
    )
    with pytest.raises(FrameSamplingError, match=r"produced no frame for k\.jpg"):
        writer(_video_row(uuid.uuid4()), 0, "k.jpg")
    assert not store.exists("k.jpg")
