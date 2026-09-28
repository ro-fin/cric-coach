"""US-D2 acceptance: per-ball multi-camera clip cutting, loud gaps, crash-resume.

The runner is always a fake that records the exact argv (and writes the dest
file the way real ffmpeg would) — real ffmpeg is never required. DB is
in-memory SQLite for units; one integration test runs the same job against
temp PostgreSQL.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import BowlerSource, ClipStatus, SessionType, VideoStatus
from cricai_data.models import BallEvent, Clip, Player, Session, Video
from cricai_data.storage import FsObjectStore, StorageError
from cricai_vision.ffmpeg import (
    FfmpegError,
    FfmpegTimeoutError,
    FfmpegUnavailableError,
    build_clip_command,
    clip_window,
)
from cricai_worker.context import WorkerContext
from cricai_worker.cut_clips import (
    EVIDENCE_STATUSES,
    ClipRunSummary,
    _truncate,
    cut_session_clips,
    store_source_resolver,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

#: (ball_no, start_ms, end_ms) — ball 1 exercises the pre-roll clamp at 0.
DEFAULT_BALLS = ((1, 1000, 2000), (2, 10000, 14000))


class FakeRunner:
    """Records every argv; simulates ffmpeg by writing the dest file."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.raises: dict[str, Exception] = {}  # dest basename -> exception
        self.write_output = True

    def __call__(self, argv: list[str]) -> None:
        self.commands.append(list(argv))
        dest = Path(argv[-1])
        exc = self.raises.get(dest.name)
        if exc is not None:
            raise exc
        if self.write_output:
            dest.write_bytes(b"clip:" + dest.name.encode())


@dataclass
class RecordingResolver:
    """Maps every video to one local file, remembering which keys it saw."""

    path: Path
    seen: list[str] = field(default_factory=list)
    raises: Exception | None = None

    def __call__(self, video: Video) -> Path:
        self.seen.append(video.object_key)
        if self.raises is not None:
            raise self.raises
        return self.path


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def resolver(tmp_path: Path) -> RecordingResolver:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    return RecordingResolver(path=source)


def _seed_session(
    ctx: WorkerContext,
    *,
    expected: tuple[str, ...] = ("C1", "C2"),
    videos: dict[str, VideoStatus] | None = None,
    balls: tuple[tuple[int, int, int], ...] = DEFAULT_BALLS,
) -> uuid.UUID:
    if videos is None:
        videos = {"C1": VideoStatus.UPLOADED, "C2": VideoStatus.PROBED}
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.COACH,
            expected_cameras=list(expected),
        )
        db.add(session)
        db.flush()
        for camera_id, video_status in videos.items():
            _add_video(db, session.id, camera_id, video_status)
        for ball_no, start_ms, end_ms in balls:
            _add_event(db, session.id, ball_no, start_ms, end_ms)
        db.commit()
        return session.id


def _add_video(
    db: OrmSession,
    session_id: uuid.UUID,
    camera_id: str,
    video_status: VideoStatus,
    *,
    suffix: str = "a",
    created_at: datetime | None = None,
    claimed_duration_s: float | None = None,
    probe: dict[str, float] | None = None,
) -> str:
    key = f"sessions/{session_id}/{camera_id}/{suffix}.mp4"
    db.add(
        Video(
            session_id=session_id,
            camera_id=camera_id,
            object_key=key,
            filename=f"{camera_id}-{suffix}.mp4",
            checksum_sha256=f"{camera_id}-{suffix}".ljust(64, "0"),
            size_bytes=1024,
            status=video_status,
            claimed_duration_s=claimed_duration_s,
            probe=probe,
            created_at=created_at or datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
        )
    )
    return key


def _add_event(
    db: OrmSession,
    session_id: uuid.UUID,
    ball_no: int,
    start_ms: int,
    end_ms: int,
    *,
    valid: bool = True,
) -> None:
    db.add(
        BallEvent(
            session_id=session_id,
            ball_no=ball_no,
            start_ms=start_ms,
            release_ms=start_ms + 200,
            contact_ms=None,
            end_ms=end_ms,
            confidence=0.9,
            valid=valid,
        )
    )


def _clip_rows(ctx: WorkerContext, session_id: uuid.UUID) -> list[Clip]:
    with ctx.session_factory() as db:
        return list(
            db.scalars(
                select(Clip)
                .where(Clip.session_id == session_id)
                .order_by(Clip.ball_no, Clip.camera_id)
            )
        )


def _row(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int, camera_id: str) -> Clip:
    matches = [
        clip
        for clip in _clip_rows(ctx, session_id)
        if clip.ball_no == ball_no and clip.camera_id == camera_id
    ]
    assert len(matches) == 1
    return matches[0]


def test_cuts_every_ball_camera_pair_with_exact_ffmpeg_argv(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx)
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)

    assert summary == ClipRunSummary(
        session_id=str(session_id),
        balls=2,
        cameras=("C1", "C2"),
        cut=4,
        skipped=0,
        gaps=0,
        failed=0,
        pending=0,
        stopped_early=False,
    )
    # Cameras outer (sorted), balls inner (by ball_no) — deterministic order.
    dests = [Path(argv[-1]).name for argv in runner.commands]
    assert dests == ["C1-1.mp4", "C1-2.mp4", "C2-1.mp4", "C2-2.mp4"]
    # Exact command per pair, including the +/-1.5s window math and clamp at 0.
    for argv, (_ball_no, start_ms, end_ms) in zip(
        runner.commands, [*DEFAULT_BALLS, *DEFAULT_BALLS], strict=True
    ):
        expected = build_clip_command(resolver.path, Path(argv[-1]), clip_window(start_ms, end_ms))
        assert argv == expected
    # Ball 1 pre-roll clamps at 0; ball 2 is an interior window.
    assert runner.commands[0][5:10] == ["0.000", "-i", str(resolver.path), "-t", "3.500"]
    assert runner.commands[1][5:6] == ["8.500"]

    rows = _clip_rows(ctx, session_id)
    assert [(c.ball_no, c.camera_id, c.status) for c in rows] == [
        (1, "C1", ClipStatus.CUT),
        (1, "C2", ClipStatus.CUT),
        (2, "C1", ClipStatus.CUT),
        (2, "C2", ClipStatus.CUT),
    ]
    for clip in rows:
        assert clip.error is None
        key = clip.object_key
        assert key == f"sessions/{session_id}/balls/{clip.ball_no}/{clip.camera_id}.mp4"
        assert ctx.store.get(key) == f"clip:{clip.camera_id}-{clip.ball_no}.mp4".encode()
    window_1 = clip_window(1000, 2000)
    assert (rows[0].start_ms, rows[0].end_ms) == (window_1.start_ms, window_1.end_ms) == (0, 3500)
    window_2 = clip_window(10000, 14000)
    assert (rows[2].start_ms, rows[2].end_ms) == (window_2.start_ms, window_2.end_ms)


def test_missing_cameras_get_loud_gap_rows(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    # C2's only video FAILED its upload (bytes deleted — not evidence); C3 has
    # no video at all; C4 has footage despite not being expected.
    session_id = _seed_session(
        ctx,
        expected=("C1", "C2", "C3"),
        videos={
            "C1": VideoStatus.UPLOADED,
            "C2": VideoStatus.FAILED,
            "C4": VideoStatus.PROBED,
        },
    )
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)

    assert summary.cameras == ("C1", "C2", "C3", "C4")
    assert (summary.cut, summary.gaps, summary.failed) == (4, 4, 0)
    assert _row(ctx, session_id, 1, "C2").error == (
        "no evidence footage for camera C2 (video statuses: failed)"
    )
    assert _row(ctx, session_id, 2, "C3").error == "no video uploaded for camera C3"
    for camera_id in ("C2", "C3"):
        for ball_no in (1, 2):
            clip = _row(ctx, session_id, ball_no, camera_id)
            assert clip.status == ClipStatus.GAP
            assert clip.object_key is None
    assert _row(ctx, session_id, 1, "C4").status == ClipStatus.CUT


def test_resume_skips_cut_recuts_missing_object_and_retries_failed(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(
        ctx,
        expected=("C1",),
        videos={"C1": VideoStatus.UPLOADED},
        balls=((1, 2000, 4000), (2, 8000, 10000), (3, 15000, 17000)),
    )
    first = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (first.cut, first.skipped) == (3, 0)

    # Ball 1: cut but its object vanished -> must be re-cut.
    ctx.store.delete(f"sessions/{session_id}/balls/1/C1.mp4")
    # Ball 2: mark failed (as if a previous run errored) -> must be retried.
    with ctx.session_factory() as db:
        clip = db.scalar(select(Clip).where(Clip.session_id == session_id, Clip.ball_no == 2))
        assert clip is not None
        clip.status = ClipStatus.FAILED
        clip.error = "previous ffmpeg failure"
        db.commit()

    runner.commands.clear()
    second = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (second.cut, second.skipped) == (2, 1)
    assert [Path(argv[-1]).name for argv in runner.commands] == ["C1-1.mp4", "C1-2.mp4"]
    rows = _clip_rows(ctx, session_id)
    assert len(rows) == 3  # upsert on the unique triple: no duplicate rows
    assert all(clip.status == ClipStatus.CUT and clip.error is None for clip in rows)
    assert ctx.store.exists(f"sessions/{session_id}/balls/1/C1.mp4")


def test_rerun_is_idempotent(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx)
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    runner.commands.clear()
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (summary.cut, summary.skipped) == (0, 4)
    assert runner.commands == []  # nothing re-cut
    assert len(_clip_rows(ctx, session_id)) == 4


def test_corrected_event_timings_recut_stale_clips(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    """US-D4: after a human timing correction the stored clip window no longer
    matches the event, so a re-run must re-cut (overwrite object + update row)
    instead of skipping — corrections are ground truth."""
    session_id = _seed_session(ctx)
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)

    # Coach corrects ball 2's timings (as the US-D4 adjust endpoint would).
    with ctx.session_factory() as db:
        event = db.scalar(
            select(BallEvent).where(BallEvent.session_id == session_id, BallEvent.ball_no == 2)
        )
        assert event is not None
        event.start_ms, event.release_ms, event.end_ms = 20000, 20200, 24000
        db.commit()
    # Plant stale bytes so the overwrite is observable.
    stale_key = f"sessions/{session_id}/balls/2/C1.mp4"
    ctx.store.put(stale_key, b"stale-pre-correction-footage")

    runner.commands.clear()
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    # Ball 2 re-cut on both cameras; untouched ball 1 skipped.
    assert (summary.cut, summary.skipped) == (2, 2)
    assert [Path(argv[-1]).name for argv in runner.commands] == ["C1-2.mp4", "C2-2.mp4"]
    corrected = clip_window(20000, 24000)
    for argv in runner.commands:
        assert argv[argv.index("-ss") + 1] == "18.500"
    for camera_id in ("C1", "C2"):
        clip = _row(ctx, session_id, 2, camera_id)
        assert clip.status == ClipStatus.CUT
        assert (clip.start_ms, clip.end_ms) == (corrected.start_ms, corrected.end_ms)
    assert ctx.store.get(stale_key) == b"clip:C1-2.mp4"  # object overwritten

    # Once re-cut, the corrected clips are skipped like any other.
    third = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (third.cut, third.skipped) == (0, 4)


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        pytest.param(FfmpegError("ffmpeg failed (rc=1): boom"), "ffmpeg failed", id="error"),
        pytest.param(
            FfmpegTimeoutError("ffmpeg timed out after 120.0s"),
            "ffmpeg failed (timeout)",
            id="timeout",
        ),
        pytest.param(None, "ffmpeg reported success but produced no output", id="no-output"),
    ],
)
def test_failed_drift_recut_never_keeps_stale_object_key(
    ctx: WorkerContext,
    runner: FakeRunner,
    resolver: RecordingResolver,
    failure: FfmpegError | None,
    expected_error: str,
) -> None:
    """Regression (review issue 7): when a window-drift re-cut fails, the
    FAILED row must not keep pointing at the pre-correction object while its
    bounds claim the corrected window — object_key is cleared in every FAILED
    branch, mirroring the ffmpeg-unavailable path (the clips API serves
    object_key/start_ms/end_ms/error verbatim to every read role)."""
    session_id = _seed_session(
        ctx, expected=("C1",), videos={"C1": VideoStatus.UPLOADED}, balls=((2, 10000, 14000),)
    )
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    key = _row(ctx, session_id, 2, "C1").object_key
    assert key is not None

    # Coach corrects the timings (US-D4 adjust), then the re-cut fails.
    with ctx.session_factory() as db:
        event = db.scalar(select(BallEvent).where(BallEvent.session_id == session_id))
        assert event is not None
        event.start_ms, event.release_ms, event.end_ms = 20000, 20200, 24000
        db.commit()
    if failure is None:
        runner.write_output = False
    else:
        runner.raises["C1-2.mp4"] = failure

    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.failed == 1
    clip = _row(ctx, session_id, 2, "C1")
    assert clip.status == ClipStatus.FAILED
    assert clip.object_key is None  # never serves stale-window footage
    assert clip.error == expected_error
    corrected = clip_window(20000, 24000)
    assert (clip.start_ms, clip.end_ms) == (corrected.start_ms, corrected.end_ms)
    # The stale object stays in the store (its key is deterministic, so the
    # next successful run overwrites it) but is unreachable through the row.
    assert ctx.store.exists(key)

    # FAILED is retryable: the next clean run re-cuts and relinks the object.
    runner.raises.clear()
    runner.write_output = True
    healed = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert healed.cut == 1
    clip = _row(ctx, session_id, 2, "C1")
    assert clip.status == ClipStatus.CUT
    assert clip.object_key == key
    assert ctx.store.get(key) == b"clip:C1-2.mp4"


def test_ffmpeg_unavailable_leaves_pending_and_stops_run(
    ctx: WorkerContext,
    runner: FakeRunner,
    resolver: RecordingResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = _seed_session(ctx)
    runner.raises["C1-2.mp4"] = FfmpegUnavailableError("ffmpeg not runnable: /opt/bin/ffmpeg")

    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.stopped_early is True
    assert (summary.cut, summary.pending) == (1, 1)
    rows = _clip_rows(ctx, session_id)
    # C2 was never reached: ffmpeg won't come back mid-run.
    assert [(c.ball_no, c.camera_id, c.status) for c in rows] == [
        (1, "C1", ClipStatus.CUT),
        (2, "C1", ClipStatus.PENDING),
    ]
    # Player-visible error is classified; the raw detail (paths) is log-only.
    assert rows[1].error == "ffmpeg unavailable"
    assert "/opt/bin/ffmpeg" in caplog.text

    # Once ffmpeg is back, the pending row and the untouched camera catch up.
    runner.raises.clear()
    resumed = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert resumed.stopped_early is False
    assert (resumed.cut, resumed.skipped) == (3, 1)
    assert all(clip.status == ClipStatus.CUT for clip in _clip_rows(ctx, session_id))


def test_ffmpeg_unavailable_resets_failed_row_to_pending(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(
        ctx, expected=("C1",), videos={"C1": VideoStatus.UPLOADED}, balls=((1, 2000, 4000),)
    )
    runner.raises["C1-1.mp4"] = FfmpegError("bad input")
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert _row(ctx, session_id, 1, "C1").status == ClipStatus.FAILED

    runner.raises["C1-1.mp4"] = FfmpegUnavailableError("ffmpeg not runnable: permission denied")
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.stopped_early is True
    clip = _row(ctx, session_id, 1, "C1")
    assert clip.status == ClipStatus.PENDING  # retryable, not failed
    assert clip.object_key is None
    assert clip.error == "ffmpeg unavailable"


def test_ffmpeg_failure_marks_row_failed_and_run_continues(
    ctx: WorkerContext,
    runner: FakeRunner,
    resolver: RecordingResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = _seed_session(ctx)
    runner.raises["C1-1.mp4"] = FfmpegError("ffmpeg failed (rc=1): moov atom not found in /tmp/x")

    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.stopped_early is False
    assert (summary.cut, summary.failed) == (3, 1)
    clip = _row(ctx, session_id, 1, "C1")
    assert clip.status == ClipStatus.FAILED
    assert clip.object_key is None
    # Classified message only — raw ffmpeg stderr (with paths) stays in the log.
    assert clip.error == "ffmpeg failed"
    assert "moov atom not found in /tmp/x" in caplog.text


def test_ffmpeg_timeout_marks_row_failed_and_run_continues(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    """A per-input hang (timeout) must not stall the session: the clip fails
    loudly, everything else still gets cut, and a later run can retry it."""
    session_id = _seed_session(ctx)
    runner.raises["C1-1.mp4"] = FfmpegTimeoutError("ffmpeg timed out after 120.0s")

    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.stopped_early is False
    assert (summary.cut, summary.failed, summary.pending) == (3, 1, 0)
    clip = _row(ctx, session_id, 1, "C1")
    assert clip.status == ClipStatus.FAILED
    assert clip.object_key is None
    assert clip.error == "ffmpeg failed (timeout)"

    # FAILED is retryable: once the source cuts cleanly, the row heals.
    runner.raises.clear()
    healed = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (healed.cut, healed.skipped) == (1, 3)
    assert all(c.status == ClipStatus.CUT for c in _clip_rows(ctx, session_id))


def test_gap_heals_when_footage_arrives_later(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx, videos={"C1": VideoStatus.UPLOADED})
    first = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (first.cut, first.gaps) == (2, 2)

    with ctx.session_factory() as db:
        _add_video(db, session_id, "C2", VideoStatus.UPLOADED)
        db.commit()
    second = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (second.cut, second.skipped, second.gaps) == (2, 2, 0)
    rows = _clip_rows(ctx, session_id)
    assert len(rows) == 4
    assert all(clip.status == ClipStatus.CUT for clip in rows)


def test_cut_clips_survive_when_footage_evidence_is_later_lost(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx)
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)

    with ctx.session_factory() as db:
        videos = db.scalars(select(Video).where(Video.camera_id == "C2")).all()
        for video in videos:
            video.status = VideoStatus.FAILED
        db.commit()
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    # Already-cut C2 clips with live objects are kept, not regressed to gaps.
    assert (summary.skipped, summary.gaps) == (4, 0)
    assert all(clip.status == ClipStatus.CUT for clip in _clip_rows(ctx, session_id))


def test_clamped_cut_clip_survives_when_duration_knowledge_is_lost(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    """Regression (review issue 6): losing the camera's evidence video also
    loses its duration, so the gap path recomputes UNclamped windows; a clip
    previously cut with a duration-clamped end is still consistent with the
    event under that past clamp and must stay CUT with its object_key intact —
    never demoted to GAP with the ground-truth footage reference severed."""
    session_id = _seed_session(ctx, expected=("C1",), videos={}, balls=((2, 10000, 14000),))
    with ctx.session_factory() as db:
        _add_video(db, session_id, "C1", VideoStatus.PROBED, probe={"duration_s": 15.0})
        db.commit()
    first = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert first.cut == 1
    clip = _row(ctx, session_id, 2, "C1")
    assert (clip.start_ms, clip.end_ms) == (8500, 15000)  # post-roll clamped at EOF
    key = clip.object_key
    assert key is not None

    # The camera loses evidence status (e.g. a corrupt replacement upload
    # after a retention purge is graded FAILED) — duration is now unknown.
    with ctx.session_factory() as db:
        video = db.scalar(select(Video).where(Video.session_id == session_id))
        assert video is not None
        video.status = VideoStatus.FAILED
        db.commit()

    second = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (second.skipped, second.gaps) == (1, 0)
    clip = _row(ctx, session_id, 2, "C1")
    assert clip.status == ClipStatus.CUT
    assert clip.object_key == key
    assert (clip.start_ms, clip.end_ms) == (8500, 15000)  # clamped window untouched
    assert ctx.store.exists(key)


@pytest.mark.parametrize(
    ("corrected_start_ms", "corrected_end_ms"),
    [
        pytest.param(20000, 24000, id="start-moved"),
        pytest.param(10000, 12000, id="end-shrank-past-unclamped-bound"),
    ],
)
def test_gap_path_still_demotes_cut_clip_on_genuine_event_drift(
    ctx: WorkerContext,
    runner: FakeRunner,
    resolver: RecordingResolver,
    corrected_start_ms: int,
    corrected_end_ms: int,
) -> None:
    """The duration-unknown leniency covers ONLY a missing end clamp: a stored
    window whose start no longer matches the event, or whose end exceeds even
    the unclamped bound (US-D4 correction while footage is gone), is a genuine
    drift — the clip no longer shows the event and demotes to GAP."""
    session_id = _seed_session(ctx, expected=("C1",), videos={}, balls=((2, 10000, 14000),))
    with ctx.session_factory() as db:
        _add_video(db, session_id, "C1", VideoStatus.PROBED, probe={"duration_s": 15.0})
        db.commit()
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (_row(ctx, session_id, 2, "C1").start_ms, _row(ctx, session_id, 2, "C1").end_ms) == (
        8500,
        15000,
    )

    with ctx.session_factory() as db:
        event = db.scalar(select(BallEvent).where(BallEvent.session_id == session_id))
        assert event is not None
        event.start_ms = corrected_start_ms
        event.release_ms = corrected_start_ms + 200
        event.end_ms = corrected_end_ms
        video = db.scalar(select(Video).where(Video.session_id == session_id))
        assert video is not None
        video.status = VideoStatus.FAILED
        db.commit()

    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (summary.skipped, summary.gaps) == (0, 1)
    clip = _row(ctx, session_id, 2, "C1")
    assert clip.status == ClipStatus.GAP
    assert clip.object_key is None
    assert clip.error == "no evidence footage for camera C1 (video statuses: failed)"


def test_unusable_event_window_marks_failed_not_silent(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(
        ctx,
        expected=("C1",),
        videos={"C1": VideoStatus.UPLOADED},
        balls=((1, 5000, 5000), (2, 8000, 10000)),
    )
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (summary.cut, summary.failed) == (1, 1)
    clip = _row(ctx, session_id, 1, "C1")
    assert clip.status == ClipStatus.FAILED
    assert clip.error is not None and clip.error.startswith("invalid event window:")
    assert (clip.start_ms, clip.end_ms) == (5000, 5000)  # raw event bounds kept visible


def test_source_resolver_failure_records_sanitized_gap_rows(
    ctx: WorkerContext,
    runner: FakeRunner,
    resolver: RecordingResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = _seed_session(ctx, expected=("C1",), videos={"C1": VideoStatus.UPLOADED})
    resolver.raises = StorageError("object not found: '/srv/objects/sessions/x/a.mp4'")
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.gaps == 2
    clip = _row(ctx, session_id, 1, "C1")
    assert clip.status == ClipStatus.GAP
    # No raw exception text (server paths) reaches the player-visible row;
    # the detail is preserved in the worker log.
    assert clip.error == "source video unavailable for camera C1"
    assert "/srv/objects/sessions/x/a.mp4" in caplog.text
    assert runner.commands == []


def test_runner_success_without_output_is_a_failure(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx, expected=("C1",), videos={"C1": VideoStatus.UPLOADED})
    runner.write_output = False
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.failed == 2
    clip = _row(ctx, session_id, 1, "C1")
    assert clip.status == ClipStatus.FAILED
    assert clip.object_key is None
    assert clip.error == "ffmpeg reported success but produced no output"


def test_clip_windows_clamp_to_probed_video_duration(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    """US-D2: the stored window never overstates the media — the last ball's
    post-roll clamps at the camera's real (probed) duration, which beats a
    wrong client claim."""
    session_id = _seed_session(ctx, expected=("C1",), videos={}, balls=DEFAULT_BALLS)
    with ctx.session_factory() as db:
        _add_video(
            db,
            session_id,
            "C1",
            VideoStatus.PROBED,
            claimed_duration_s=99.0,
            probe={"duration_s": 15.0, "fps": 30.0},
        )
        db.commit()
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)

    clip = _row(ctx, session_id, 2, "C1")  # event 10000-14000, post-roll hits EOF
    assert (clip.start_ms, clip.end_ms) == (8500, 15000)
    assert runner.commands[1][5:10] == ["8.500", "-i", str(resolver.path), "-t", "6.500"]
    # Interior ball untouched by the clamp.
    assert (_row(ctx, session_id, 1, "C1").start_ms, _row(ctx, session_id, 1, "C1").end_ms) == (
        0,
        3500,
    )


def test_clip_windows_clamp_to_claimed_duration_when_unprobed(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx, expected=("C1",), videos={}, balls=((2, 10000, 14000),))
    with ctx.session_factory() as db:
        _add_video(db, session_id, "C1", VideoStatus.UPLOADED, claimed_duration_s=15.0)
        db.commit()
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    clip = _row(ctx, session_id, 2, "C1")
    assert (clip.start_ms, clip.end_ms) == (8500, 15000)


def test_long_errors_truncate_to_column_width() -> None:
    """Guard for the 255-char Clip.error column: classified messages are short
    today, but anything routed through _truncate must never overflow."""
    assert _truncate("short") == "short"
    assert _truncate("x" * 255) == "x" * 255
    truncated = _truncate("x" * 400)
    assert len(truncated) == 255
    assert truncated.endswith("...")


def test_rejected_events_are_not_clipped(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(
        ctx, expected=("C1",), videos={"C1": VideoStatus.UPLOADED}, balls=((1, 2000, 4000),)
    )
    with ctx.session_factory() as db:
        _add_event(db, session_id, 2, 8000, 10000, valid=False)
        db.commit()
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.balls == 1
    assert [clip.ball_no for clip in _clip_rows(ctx, session_id)] == [1]


def test_newest_evidence_video_wins_per_camera(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx, expected=("C1",), videos={}, balls=((1, 2000, 4000),))
    with ctx.session_factory() as db:
        _add_video(
            db,
            session_id,
            "C1",
            VideoStatus.UPLOADED,
            suffix="old",
            created_at=datetime(2026, 7, 7, 8, 0, tzinfo=UTC),
        )
        newest = _add_video(
            db,
            session_id,
            "C1",
            VideoStatus.PROBED,
            suffix="new",
            created_at=datetime(2026, 7, 7, 11, 0, tzinfo=UTC),
        )
        db.commit()
    cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert resolver.seen == [newest]


def test_session_without_events_or_cameras_is_a_no_op(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    session_id = _seed_session(ctx, expected=(), videos={}, balls=())
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary == ClipRunSummary(
        session_id=str(session_id),
        balls=0,
        cameras=(),
        cut=0,
        skipped=0,
        gaps=0,
        failed=0,
        pending=0,
        stopped_early=False,
    )
    assert _clip_rows(ctx, session_id) == []


def test_unknown_session_raises(
    ctx: WorkerContext, runner: FakeRunner, resolver: RecordingResolver
) -> None:
    with pytest.raises(ValueError, match="session not found"):
        cut_session_clips(ctx, uuid.uuid4(), source_resolver=resolver, runner=runner)


def test_store_source_resolver_streams_once_and_caches(ctx: WorkerContext, tmp_path: Path) -> None:
    session_id = _seed_session(
        ctx, expected=("C1",), videos={"C1": VideoStatus.UPLOADED}, balls=((1, 2000, 4000),)
    )
    with ctx.session_factory() as db:
        video = db.scalar(select(Video).where(Video.session_id == session_id))
        assert video is not None
    ctx.store.put(video.object_key, b"camera-file-bytes")

    resolve = store_source_resolver(ctx.store, tmp_path / "work")
    first = resolve(video)
    assert first.read_bytes() == b"camera-file-bytes"
    # Cached: a second resolve never re-reads the store (object deleted).
    ctx.store.delete(video.object_key)
    assert resolve(video) == first
    assert first.is_file()


def test_evidence_rule_excludes_rows_without_playable_bytes() -> None:
    """The canonical evidence set (cricai_data.enums) never counts FAILED
    (bytes deleted on checksum mismatch) or PENDING (nothing stored yet)."""
    assert VideoStatus.FAILED not in EVIDENCE_STATUSES
    assert VideoStatus.PENDING not in EVIDENCE_STATUSES
    expected = frozenset({VideoStatus.UPLOADED, VideoStatus.PROBED, VideoStatus.METADATA_CONFLICT})
    assert expected == EVIDENCE_STATUSES


@pytest.mark.integration
def test_event_to_clip_link_integrity_and_resume_on_postgres(pg_url: str, tmp_path: Path) -> None:
    """IT (US-D2): event -> clip -> DB link integrity plus crash-resume on
    real PostgreSQL."""
    engine = make_engine(pg_url)
    create_all(engine)
    ctx = WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    resolver = RecordingResolver(path=source)
    runner = FakeRunner()

    session_id = _seed_session(ctx)
    summary = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert summary.cut == 4

    with ctx.session_factory() as db:
        events = db.scalars(select(BallEvent).where(BallEvent.session_id == session_id)).all()
        clips = db.scalars(select(Clip).where(Clip.session_id == session_id)).all()
        assert {event.ball_no for event in events} == {clip.ball_no for clip in clips}
        for clip in clips:
            assert clip.object_key is not None
            assert ctx.store.exists(clip.object_key)

    # Crash-resume: lose one object, re-run, nothing duplicates.
    ctx.store.delete(f"sessions/{session_id}/balls/1/C1.mp4")
    resumed = cut_session_clips(ctx, session_id, source_resolver=resolver, runner=runner)
    assert (resumed.cut, resumed.skipped) == (1, 3)
    with ctx.session_factory() as db:
        assert len(db.scalars(select(Clip).where(Clip.session_id == session_id)).all()) == 4
