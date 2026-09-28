"""US-F3 tracking job tests: fan-out, pinned keys, idempotent upsert, pitch mapping."""

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    CalibrationKind,
    EventSource,
    LabelClass,
    SessionType,
    VideoStatus,
)
from cricai_data.models import (
    BallEvent,
    BallTrack,
    BounceMark,
    Calibration,
    CameraConfig,
    Player,
    Video,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore, ObjectStore
from cricai_vision.detect import Detection, FakeDetectionProvider
from cricai_vision.extrinsics import PlaneCalibration, to_params
from cricai_vision.track import TRACKER_VERSION
from cricai_worker.context import WorkerContext
from cricai_worker.track_balls import MIN_COVERAGE, TrackRunSummary, track_key, track_session_balls
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

PROBE = {
    "fps": 30.0,
    "width": 1920,
    "height": 1080,
    "resolution": "1920x1080",
    "codec": "h264",
    "duration_s": 120.0,
}

#: pitch = px / 100 — a valid, invertible extrinsic params payload.
SCALE_PARAMS = to_params(
    PlaneCalibration(
        matrix=((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (0.0, 0.0, 1.0)),
        rms_px=0.5,
        rms_m=0.01,
        n_landmarks=8,
    )
)


def _context(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _file_context(tmp_path: Path) -> WorkerContext:
    """File-backed DB: two overlapping runs each need their own connection."""
    engine = create_engine(f"sqlite:///{tmp_path / 'tracks.sqlite'}")
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
        session = _add_session(db)
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


def _add_session(db: OrmSession) -> SessionRow:
    player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
    session = SessionRow(
        player=player,
        session_date=datetime.date(2026, 7, 7),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add_all([player, session])
    db.flush()
    return session


def _seed_video(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    camera_id: str,
    *,
    status: VideoStatus = VideoStatus.PROBED,
    probe: dict[str, Any] | None = None,
    claimed_fps: float | None = None,
    claimed_resolution: str | None = None,
) -> None:
    with ctx.session_factory() as db:
        db.add(
            Video(
                session_id=session_id,
                camera_id=camera_id,
                object_key=f"sessions/{session_id}/{camera_id}/{uuid.uuid4().hex}.mp4",
                filename=f"{camera_id}.mp4",
                checksum_sha256=uuid.uuid4().hex * 2,
                size_bytes=1024,
                status=status,
                probe=probe,
                claimed_fps=claimed_fps,
                claimed_resolution=claimed_resolution,
            )
        )
        db.commit()


def _rows(ctx: WorkerContext) -> list[BallTrack]:
    with ctx.session_factory() as db:
        return list(
            db.execute(select(BallTrack).order_by(BallTrack.ball_no, BallTrack.camera_id))
            .scalars()
            .all()
        )


def _run(ctx: WorkerContext, session_id: uuid.UUID, **kwargs: Any) -> TrackRunSummary:
    return track_session_balls(ctx, session_id, **kwargs)


def test_tracks_every_valid_event_on_every_evidence_camera(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2), rejected_balls=(3,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    _seed_video(ctx, session_id, "C3", probe=PROBE)
    _seed_video(ctx, session_id, "C4", status=VideoStatus.FAILED, probe=PROBE)

    summary = _run(ctx, session_id)
    assert summary.session_id == str(session_id)
    assert summary.tracked == 4
    assert summary.skipped_cameras == ()
    assert summary.low_coverage == ()
    assert summary.failed == ()
    rows = _rows(ctx)
    # Rejected ball 3 and evidence-less camera C4 get no rows.
    assert [(r.ball_no, r.camera_id) for r in rows] == [
        (1, "C1"),
        (1, "C3"),
        (2, "C1"),
        (2, "C3"),
    ]
    for row in rows:
        assert row.session_id == session_id
        assert row.tracker_version == TRACKER_VERSION
        assert row.points_key == track_key(session_id, row.ball_no, row.camera_id)
        payload = json.loads(ctx.store.get(row.points_key))
        assert set(payload) == {"points", "segments", "flags"}
        # The row mirrors the stored payload (pinned contract).
        assert row.coverage == 1.0
        assert row.flags == payload["flags"]
        assert row.segments == payload["segments"]
        assert [s["kind"] for s in row.segments] == ["pre_bounce", "post_bounce"]
        assert row.flags["pitch_mapped"] is False
        assert row.confidence == pytest.approx(0.9)


def test_rerun_upserts_rows_and_overwrites_payloads(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    first = _run(ctx, session_id, provider=FakeDetectionProvider())
    key = track_key(session_id, 1, "C1")
    first_payload = ctx.store.get(key)
    second = _run(ctx, session_id, provider=FakeDetectionProvider(seed=3, dropout=0.4))
    assert first.tracked == second.tracked == 1
    rows = _rows(ctx)
    assert len(rows) == 1  # upsert on the unique triple, never a duplicate row
    assert rows[0].tracker_version == TRACKER_VERSION
    second_payload = ctx.store.get(key)
    assert second_payload != first_payload
    # The committed row always mirrors the currently stored payload bytes.
    assert rows[0].flags == json.loads(second_payload)["flags"]
    assert rows[0].segments == json.loads(second_payload)["segments"]


def test_rerun_never_touches_other_tables(tmp_path: Path) -> None:
    """Tracks are auto-derived: re-running replaces only its own rows (pinned)."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    with ctx.session_factory() as db:
        db.add(
            BounceMark(
                session_id=session_id,
                ball_no=1,
                camera_id="C3",
                frame_no=42,
                px_x=1000.0,
                px_y=600.0,
                pitch_x=14.0,
                pitch_y=0.2,
            )
        )
        db.commit()
    _run(ctx, session_id)
    _run(ctx, session_id)
    with ctx.session_factory() as db:
        mark = db.execute(select(BounceMark)).scalars().one()
        assert (mark.ball_no, mark.px_x, mark.pitch_x) == (1, 1000.0, 14.0)


class _CrashOnCall:
    """Delegates to FakeDetectionProvider until the Nth detect call, then raises."""

    version = "crash-detect"

    def __init__(self, *, crash_on_call: int) -> None:
        self._crash_on_call = crash_on_call
        self._calls = 0

    def detect(self, *, start_ms: float, end_ms: float, fps: float) -> list[Detection]:
        self._calls += 1
        if self._calls == self._crash_on_call:
            raise RuntimeError("detector crashed")
        return FakeDetectionProvider().detect(start_ms=start_ms, end_ms=end_ms, fps=fps)


def test_mid_run_crash_keeps_committed_balls_consistent(tmp_path: Path) -> None:
    """Each ball's store write + row upsert commit together (crash-safe re-runs)."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    with pytest.raises(RuntimeError, match="detector crashed"):
        _run(ctx, session_id, provider=_CrashOnCall(crash_on_call=2))
    rows = _rows(ctx)
    assert [(r.ball_no, r.camera_id) for r in rows] == [(1, "C1")]
    payload = json.loads(ctx.store.get(rows[0].points_key))
    assert rows[0].flags == payload["flags"]  # committed row describes stored bytes
    assert not ctx.store.exists(track_key(session_id, 2, "C1"))


class _CrashAfterPut:
    """Real store, but the process dies right after the Nth completed put
    (a kill between the payload write and the row commit)."""

    def __init__(self, inner: ObjectStore, *, crash_after_put: int) -> None:
        self._inner = inner
        self._crash_after_put = crash_after_put
        self._puts = 0

    def put(self, key: str, data: bytes) -> None:
        self._inner.put(key, data)
        self._puts += 1
        if self._puts == self._crash_after_put:
            raise RuntimeError("killed after store write")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CrashAfterCopy:
    """Real store, but the process dies right after the first completed copy
    (a kill between the pinned-key promote and the final row commit)."""

    def __init__(self, inner: ObjectStore) -> None:
        self._inner = inner

    def copy(self, src_key: str, dst_key: str) -> None:
        self._inner.copy(src_key, dst_key)
        raise RuntimeError("killed after promote")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_rerun_kill_between_payload_write_and_row_commit_stays_consistent(tmp_path: Path) -> None:
    """A re-run killed between its store write and row commit must never leave
    new payload bytes behind the stale committed row (review track_balls.py:296)."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    _run(ctx, session_id)
    key = track_key(session_id, 1, "C1")
    first_payload = ctx.store.get(key)
    crashing = WorkerContext(
        session_factory=ctx.session_factory,
        store=_CrashAfterPut(ctx.store, crash_after_put=1),
    )
    with pytest.raises(RuntimeError, match="killed after store write"):
        _run(crashing, session_id, provider=FakeDetectionProvider(seed=3, dropout=0.4))
    assert ctx.store.get(key) == first_payload  # pinned bytes never clobbered early
    (row,) = _rows(ctx)
    served = json.loads(ctx.store.get(row.points_key))
    assert row.flags == served["flags"]  # the committed row mirrors its bytes
    assert row.segments == served["segments"]
    # The next run converges: new payload at the pinned key, row mirroring it,
    # staging leftovers swept.
    _run(ctx, session_id, provider=FakeDetectionProvider(seed=3, dropout=0.4))
    (row,) = _rows(ctx)
    assert row.points_key == key
    converged = json.loads(ctx.store.get(key))
    assert ctx.store.get(key) != first_payload
    assert row.flags == converged["flags"]
    assert row.segments == converged["segments"]
    assert ctx.store.list_keys(f"sessions/{session_id}/balls/1") == [key]


def test_rerun_kill_between_promote_and_final_commit_converges(tmp_path: Path) -> None:
    """A re-run killed after promoting the payload but before the final row
    commit leaves the row on consistent staging bytes; the next run re-points
    it at the pinned key and sweeps staging (review track_balls.py:296)."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    _run(ctx, session_id)
    key = track_key(session_id, 1, "C1")
    crashing = WorkerContext(
        session_factory=ctx.session_factory,
        store=_CrashAfterCopy(ctx.store),
    )
    with pytest.raises(RuntimeError, match="killed after promote"):
        _run(crashing, session_id, provider=FakeDetectionProvider(seed=3, dropout=0.4))
    (row,) = _rows(ctx)
    assert row.points_key != key  # mid-protocol: the row rides the staging copy
    served = json.loads(ctx.store.get(row.points_key))
    assert row.flags == served["flags"]
    assert row.segments == served["segments"]
    _run(ctx, session_id, provider=FakeDetectionProvider(seed=3, dropout=0.4))
    (row,) = _rows(ctx)
    assert row.points_key == key
    assert row.segments == json.loads(ctx.store.get(key))["segments"]
    assert ctx.store.list_keys(f"sessions/{session_id}/balls/1") == [key]


class _SiblingRunAfterStagingPut:
    """Run A's store: right after A stages its payload, a full sibling run B
    executes end to end, and B's namespace-wide staging sweep deletes A's
    staging object before A can promote it (audit track_balls.py:326)."""

    def __init__(self, inner: ObjectStore, ctx: WorkerContext, session_id: uuid.UUID) -> None:
        self._inner = inner
        self._ctx = ctx
        self._session_id = session_id
        self._fired = False

    def put(self, key: str, data: bytes) -> None:
        self._inner.put(key, data)
        if ".staging-" in key and not self._fired:
            self._fired = True
            track_session_balls(self._ctx, self._session_id)  # sibling run B
            assert not self._inner.exists(key)  # B's sweep deleted A's staging object

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_parallel_runs_sweep_never_aborts_or_strands_the_sibling(tmp_path: Path) -> None:
    """A parallel run's sweep deletes this run's staging object between its
    row commit and its promote copy; the run must converge on the pinned key
    instead of aborting with an uncaught StorageError and leaving the
    committed row pointing at the deleted key (audit track_balls.py:326)."""
    ctx = _file_context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    interleaved = WorkerContext(
        session_factory=ctx.session_factory,
        store=_SiblingRunAfterStagingPut(ctx.store, ctx, session_id),
    )
    summary = track_session_balls(interleaved, session_id)
    assert summary.tracked == 1
    assert summary.failed == ()
    key = track_key(session_id, 1, "C1")
    (row,) = _rows(ctx)
    assert row.points_key == key  # never left pointing at the deleted staging key
    served = json.loads(ctx.store.get(key))
    assert row.flags == served["flags"]  # the committed row mirrors its bytes
    assert row.segments == served["segments"]
    assert ctx.store.list_keys(f"sessions/{session_id}/balls/1") == [key]


def test_camera_without_usable_fps_is_skipped_loudly(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", status=VideoStatus.UPLOADED)  # no probe, no claims
    summary = _run(ctx, session_id)
    assert summary.tracked == 0
    assert summary.skipped_cameras == (
        ("C1", "no usable fps for camera C1 (neither probed nor claimed)"),
    )
    assert _rows(ctx) == []


def test_camera_without_usable_resolution_is_skipped_loudly(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", claimed_fps=30.0, claimed_resolution="wat")
    summary = _run(ctx, session_id)
    assert summary.skipped_cameras == (
        ("C1", "no usable resolution for camera C1 (neither probed nor claimed)"),
    )


@pytest.mark.parametrize(
    ("probe", "claimed_fps", "claimed_resolution", "expect_skip"),
    [
        (None, 30.0, "1920x1080", False),  # claims alone are enough
        ({"fps": True, "width": 1920, "height": 1080}, 30.0, None, False),  # bool fps ignored
        ({"fps": 30.0, "width": "wide"}, None, "1280x720", False),  # bad probe size -> claim
        ({"fps": -30.0}, None, "1920x1080", True),  # nonpositive fps unusable
        (None, 30.0, "0x1080", True),  # zero-pixel claim unusable
        (None, None, "1920x1080", True),  # no fps anywhere
        ({"fps": 30.0}, None, None, True),  # no resolution anywhere
    ],
)
def test_metadata_fallbacks(
    tmp_path: Path,
    probe: dict[str, Any] | None,
    claimed_fps: float | None,
    claimed_resolution: str | None,
    expect_skip: bool,
) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(
        ctx,
        session_id,
        "C1",
        status=VideoStatus.UPLOADED,
        probe=probe,
        claimed_fps=claimed_fps,
        claimed_resolution=claimed_resolution,
    )
    summary = _run(ctx, session_id)
    assert bool(summary.skipped_cameras) is expect_skip
    assert summary.tracked == (0 if expect_skip else 1)


def _seed_calibration(
    ctx: WorkerContext,
    camera_id: str,
    *,
    params: dict[str, Any] | None = None,
    kind: CalibrationKind = CalibrationKind.EXTRINSIC,
    era_no: int = 1,
    valid: bool = True,
    register_camera: bool = True,
) -> uuid.UUID:
    with ctx.session_factory() as db:
        if register_camera:
            db.add(
                CameraConfig(
                    camera_id=camera_id,
                    era_no=era_no,
                    position_label="side-on",
                    xyz_offset_m={"x": 0.0, "y": 3.0, "z": 1.5},
                    height_m=1.5,
                    fps=30,
                    resolution="1920x1080",
                )
            )
        record = Calibration(
            camera_id=camera_id,
            era_no=era_no,
            kind=kind,
            params=params if params is not None else SCALE_PARAMS,
            valid=valid,
        )
        db.add(record)
        db.commit()
        return record.id


def _link_session_calibration(
    ctx: WorkerContext, session_id: uuid.UUID, calibration_id: uuid.UUID
) -> None:
    with ctx.session_factory() as db:
        session = db.get(SessionRow, session_id)
        assert session is not None
        session.calibration_id = calibration_id
        db.commit()


def test_session_linked_calibration_enriches_points_with_pitch(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    record_id = _seed_calibration(ctx, "C1")
    _link_session_calibration(ctx, session_id, record_id)

    summary = _run(ctx, session_id)
    assert summary.pixel_only_cameras == ()
    (row,) = _rows(ctx)
    assert row.flags["pitch_mapped"] is True
    payload = json.loads(ctx.store.get(row.points_key))
    point = payload["points"][0]
    assert point["pitch_x"] == pytest.approx(point["px_x"] / 100.0)
    assert point["pitch_y"] == pytest.approx(point["px_y"] / 100.0)


def test_era_calibration_used_when_link_is_for_another_camera(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    other = _seed_calibration(ctx, "C3")
    _link_session_calibration(ctx, session_id, other)
    _seed_calibration(ctx, "C1")

    summary = _run(ctx, session_id)
    assert summary.pixel_only_cameras == ()
    (row,) = _rows(ctx)
    assert row.flags["pitch_mapped"] is True


def test_invalidated_link_falls_back_to_current_era(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    stale = _seed_calibration(ctx, "C1", valid=False)
    _link_session_calibration(ctx, session_id, stale)
    _seed_calibration(ctx, "C1", register_camera=False)

    summary = _run(ctx, session_id)
    assert summary.pixel_only_cameras == ()
    (row,) = _rows(ctx)
    assert row.flags["pitch_mapped"] is True


def test_unregistered_camera_stays_pixel_only(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    summary = _run(ctx, session_id)
    assert summary.pixel_only_cameras == (("C1", "no valid extrinsic calibration for camera C1"),)
    (row,) = _rows(ctx)
    assert row.flags["pitch_mapped"] is False
    payload = json.loads(ctx.store.get(row.points_key))
    assert "pitch_x" not in payload["points"][0]


def test_intrinsic_only_era_stays_pixel_only(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    _seed_calibration(ctx, "C1", kind=CalibrationKind.INTRINSIC, params={"version": 1})
    summary = _run(ctx, session_id)
    assert summary.pixel_only_cameras == (("C1", "no valid extrinsic calibration for camera C1"),)


def test_corrupt_stored_calibration_degrades_to_pixel_only_loudly(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    record_id = _seed_calibration(ctx, "C1", params={"version": 99})
    _link_session_calibration(ctx, session_id, record_id)
    summary = _run(ctx, session_id)
    ((camera_id, reason),) = summary.pixel_only_cameras
    assert camera_id == "C1"
    assert "unusable" in reason
    (row,) = _rows(ctx)
    assert row.flags["pitch_mapped"] is False


def test_deleted_linked_calibration_falls_back(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    _link_session_calibration(ctx, session_id, uuid.uuid4())  # dangling link
    summary = _run(ctx, session_id)
    assert summary.pixel_only_cameras == (("C1", "no valid extrinsic calibration for camera C1"),)


class _ScriptedProvider:
    """Emits a fixed detection list regardless of the window (US-F3 tests)."""

    version = "scripted-detect"

    def __init__(self, detections: list[Detection]) -> None:
        self._detections = detections

    def detect(self, *, start_ms: float, end_ms: float, fps: float) -> list[Detection]:
        del start_ms, end_ms, fps  # scripted: the fixture pins the exact detections
        return list(self._detections)


def test_low_coverage_is_flagged_never_padded(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1,))
    _seed_video(ctx, session_id, "C1", claimed_fps=10.0, claimed_resolution="1920x1080")
    # Event window 10s..14s at 10 fps -> frames 100..140; only 5 early frames detected.
    detections = [
        Detection(
            frame_no=100 + i,
            ts_ms=10_000.0 + 100.0 * i,
            label=LabelClass.BALL,
            cx=(100.0 + 30.0 * i) / 1920,
            cy=0.5,
            w=0.02,
            h=0.02,
            score=0.9,
        )
        for i in range(5)
    ]
    summary = _run(ctx, session_id, provider=_ScriptedProvider(detections))
    assert summary.tracked == 1
    assert summary.low_coverage == ((1, "C1"),)
    (row,) = _rows(ctx)
    assert row.coverage < MIN_COVERAGE
    assert row.coverage == pytest.approx(5 / 41)


def test_unusable_event_window_is_recorded_failed_not_fatal(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, valid_balls=(1, 2))
    with ctx.session_factory() as db:
        event = db.execute(select(BallEvent).where(BallEvent.ball_no == 1)).scalar_one()
        event.end_ms = event.start_ms  # zero-length window
        db.commit()
    _seed_video(ctx, session_id, "C1", probe=PROBE)
    summary = _run(ctx, session_id)
    assert summary.tracked == 1  # ball 2 still processed
    ((ball_no, camera_id, reason),) = summary.failed
    assert (ball_no, camera_id) == (1, "C1")
    assert "empty window" in reason
    assert [(r.ball_no, r.camera_id) for r in _rows(ctx)] == [(2, "C1")]


def test_unknown_session_raises(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="session not found"):
        track_session_balls(ctx, uuid.uuid4())


def test_track_key_layout_is_pinned() -> None:
    sid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert track_key(sid, 12, "C1") == f"sessions/{sid}/balls/12/track-C1.json"
    with pytest.raises(ValueError, match="ball_no"):
        track_key(sid, 0, "C1")
