"""Phase-6 CV call-adapter tests (US-J1/US-L1): honest input derivation per stage.

Real cv2 encodes/decodes tiny synthetic videos for the events/pose/detect
frame paths (opencv is a library, not a service — no integration marker
needed); the ffmpeg-bound clips job and the heavyweight model constructors
are exercised through monkeypatched seams and stub modules, mirroring the
``test_yolo_detect`` contract. The full real-media DAG run lives in
``test_stage_adapters_pg_integration.py``.
"""

from __future__ import annotations

import datetime
import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    BowlingVariation,
    ClipStatus,
    EventSource,
    Length,
    Line,
    MetricPhase,
    ModelStage,
    SessionType,
    TrainingStatus,
    VideoStatus,
)
from cricai_data.models import (
    BallEvent,
    BallMetrics,
    BallTrack,
    BounceEstimate,
    BowlingTarget,
    Clip,
    Dataset,
    DeliveryLabel,
    ModelRun,
    ModelVersion,
    Player,
    Video,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_vision.detect import FakeDetectionProvider
from cricai_worker import stage_adapters as sa
from cricai_worker.bowling_action import BOWLING_ACTION_KEYS
from cricai_worker.context import WorkerContext
from cricai_worker.cut_clips import ClipRunSummary
from cricai_worker.stage_adapters import (
    DETECTOR_MODEL_NAME,
    ENV_POSE_MODEL_ASSET,
    StageInputError,
    VideoFrameSource,
    clip_frames_resolver,
    production_detector,
    resolve_pose_provider,
    run_bowling_action_stage,
    run_bowling_flight_stage,
    run_classify_variations_stage,
    run_clips_stage,
    run_detect_track_stage,
    run_events_stage,
    run_metrics_stage,
    run_pose_stage,
)
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from test_bowling_action import FPS as SWING_FPS
from test_bowling_action import N_FRAMES, SwingPoseProvider

# ------------------------------------------------------------------ fixtures


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
def session_id(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            expected_cameras=["C1"],
        )
        db.add_all([player, session])
        db.commit()
        return session.id


def _add_video(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    camera_id: str,
    *,
    probe: dict[str, Any] | None,
    object_key: str | None = None,
    claimed_fps: float | None = None,
    status: VideoStatus = VideoStatus.PROBED,
) -> None:
    with ctx.session_factory() as db:
        db.add(
            Video(
                session_id=session_id,
                camera_id=camera_id,
                object_key=object_key or f"videos/{session_id}/{camera_id}.mp4",
                filename=f"{camera_id}.mp4",
                checksum_sha256=uuid.uuid4().hex * 2,
                size_bytes=1,
                claimed_fps=claimed_fps,
                status=status,
                probe=probe,
            )
        )
        db.commit()


def _add_event(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> None:
    with ctx.session_factory() as db:
        db.add(
            BallEvent(
                session_id=session_id,
                ball_no=ball_no,
                start_ms=ball_no * 10_000,
                release_ms=ball_no * 10_000 + 400,
                end_ms=ball_no * 10_000 + 2_000,
                confidence=0.9,
                source=EventSource.AUTO,
                detector_version="det-1",
                valid=True,
            )
        )
        db.commit()


def _write_video(
    path: Path, *, frames: int, moving: range | None = None, fps: float = 30.0
) -> None:
    """Encode a tiny 64x48 mp4: black frames, plus a sweeping block in ``moving``."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (64, 48))
    assert writer.isOpened()
    for index in range(frames):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        if moving is not None and index in moving:
            x = (index * 3) % 50
            frame[10:30, x : x + 10] = 255
        writer.write(frame)
    writer.release()


def _store_video(
    ctx: WorkerContext,
    tmp_path: Path,
    key: str,
    *,
    frames: int,
    moving: range | None = None,
    fps: float = 30.0,
) -> None:
    local = tmp_path / f"{uuid.uuid4().hex}.mp4"
    _write_video(local, frames=frames, moving=moving, fps=fps)
    ctx.store.put(key, local.read_bytes())


# ------------------------------------------------------------- events stage


class TestRunEventsStage:
    def test_missing_session_raises_lookup_error(self, ctx: WorkerContext) -> None:
        with pytest.raises(LookupError, match="not found"):
            run_events_stage(ctx, uuid.uuid4())

    def test_no_evidence_footage_is_an_honest_input_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        with pytest.raises(StageInputError, match="no evidence footage"):
            run_events_stage(ctx, session_id)

    def test_failed_uploads_are_not_evidence(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        _add_video(ctx, session_id, "C1", probe={"fps": 10.0}, status=VideoStatus.FAILED)
        with pytest.raises(StageInputError, match="no evidence footage"):
            run_events_stage(ctx, session_id)

    def test_no_usable_fps_is_an_honest_input_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        # A boolean probe fps is not a number and the claimed rate is zero.
        _add_video(ctx, session_id, "C1", probe={"fps": True}, claimed_fps=0.0)
        with pytest.raises(StageInputError, match="no usable fps"):
            run_events_stage(ctx, session_id)

    def test_probe_envelope_wins_and_onsets_ride_along(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        """Two clean bumps at 10 fps -> two events; the in-window onset becomes
        contact evidence, the pre-release clank never does (US-D1)."""
        quiet, active = [0.0] * 15, [1.0, 0.9, 0.8, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7]
        envelope = quiet + active + quiet + active + quiet
        probe = {"fps": 10.0, "motion_energy": envelope, "audio_onsets_ms": [100, 2000]}
        _add_video(ctx, session_id, "C1", probe=probe)
        payload = run_events_stage(ctx, session_id)
        assert payload["camera_id"] == "C1"
        assert payload["fps"] == 10.0
        assert payload["motion_energy_source"] == "probe"
        assert payload["frames"] == len(envelope)
        assert payload["audio_onsets"] == 2
        assert payload["detected"] == 2
        assert payload["first_ball_no"] == 1
        with ctx.session_factory() as db:
            events = db.scalars(
                select(BallEvent)
                .where(BallEvent.session_id == session_id)
                .order_by(BallEvent.ball_no)
            ).all()
        assert [event.ball_no for event in events] == [1, 2]
        assert events[0].contact_ms == 2000  # onset inside (release, end]
        assert events[1].contact_ms is None  # no onset in the second window

    def test_reference_camera_is_preferred_else_lowest(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        envelope = [0.0] * 15 + [1.0] * 10 + [0.0] * 15
        _add_video(ctx, session_id, "C3", probe={"fps": 10.0, "motion_energy": envelope})
        _add_video(ctx, session_id, "C2", probe={"fps": 10.0, "motion_energy": envelope})
        assert run_events_stage(ctx, session_id)["camera_id"] == "C2"  # lowest wins
        _add_video(ctx, session_id, "C1", probe={"fps": 10.0, "motion_energy": envelope})
        # C2's auto events have no dependent rows, so the replace is legal.
        assert run_events_stage(ctx, session_id)["camera_id"] == "C1"

    @pytest.mark.parametrize(
        ("probe", "match"),
        [
            ({"fps": 10.0, "motion_energy": "loud"}, "must be a list"),
            ({"fps": 10.0, "motion_energy": [0.1, True]}, "non-numeric sample at index 1"),
            ({"fps": 10.0, "motion_energy": [0.1, "x"]}, "non-numeric sample at index 1"),
            ({"fps": 10.0, "motion_energy": [0.0], "audio_onsets_ms": 5}, "integer milliseconds"),
            (
                {"fps": 10.0, "motion_energy": [0.0], "audio_onsets_ms": [True]},
                "integer milliseconds",
            ),
        ],
    )
    def test_malformed_probe_seam_data_fails_loud(
        self, ctx: WorkerContext, session_id: uuid.UUID, probe: dict[str, Any], match: str
    ) -> None:
        _add_video(ctx, session_id, "C1", probe=probe)
        with pytest.raises(StageInputError, match=match):
            run_events_stage(ctx, session_id)

    def test_energy_is_computed_from_the_stored_video(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        key = f"videos/{session_id}/C1.mp4"
        _store_video(ctx, tmp_path, key, frames=135, moving=range(45, 75), fps=30.0)
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0}, object_key=key)
        payload = run_events_stage(ctx, session_id)
        assert payload["motion_energy_source"] == "video"
        assert payload["frames"] == 135
        assert payload["audio_onsets"] is None
        assert payload["detected"] == 1

    def test_all_still_footage_detects_nothing_honestly(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        key = f"videos/{session_id}/C1.mp4"
        _store_video(ctx, tmp_path, key, frames=20, moving=None)
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0}, object_key=key)
        payload = run_events_stage(ctx, session_id)
        assert payload["detected"] == 0
        assert payload["first_ball_no"] is None

    def test_missing_source_bytes_fail_loud(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0}, object_key="videos/gone.mp4")
        with pytest.raises(StageInputError, match="unavailable in the object store"):
            run_events_stage(ctx, session_id)

    def test_undecodable_source_bytes_fail_loud(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        key = f"videos/{session_id}/C1.mp4"
        ctx.store.put(key, b"not a video at all")
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0}, object_key=key)
        with pytest.raises(StageInputError, match="cannot be decoded"):
            run_events_stage(ctx, session_id)

    def test_decodable_but_frameless_source_fails_loud(
        self, ctx: WorkerContext, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A container cv2 opens but reads zero frames from is an input gap."""

        class _EmptyCapture:
            def __init__(self, _path: str) -> None: ...

            def isOpened(self) -> bool:
                return True

            def read(self) -> tuple[bool, None]:
                return False, None

            def release(self) -> None: ...

        monkeypatch.setattr(sa.cv2, "VideoCapture", _EmptyCapture)
        key = f"videos/{session_id}/C1.mp4"
        ctx.store.put(key, b"header only")
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0}, object_key=key)
        with pytest.raises(StageInputError, match="no decodable frames"):
            run_events_stage(ctx, session_id)


# -------------------------------------------------------------- clips stage


class TestRunClipsStage:
    def test_passes_a_working_store_source_resolver(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        key = f"videos/{session_id}/C1.mp4"
        _store_video(ctx, tmp_path, key, frames=5)
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0}, object_key=key)
        seen: dict[str, Any] = {}

        def fake_cut(
            inner_ctx: WorkerContext, inner_session_id: uuid.UUID, *, source_resolver: Any
        ) -> ClipRunSummary:
            with inner_ctx.session_factory() as db:
                video = db.scalars(select(Video).where(Video.session_id == inner_session_id)).one()
                resolved = source_resolver(video)
            # Read INSIDE the job call: the adapter's workdir is stage-scoped.
            seen["resolved_bytes"] = resolved.read_bytes()
            return ClipRunSummary(
                session_id=str(inner_session_id),
                balls=2,
                cameras=("C1",),
                cut=2,
                skipped=0,
                gaps=0,
                failed=0,
                pending=0,
                stopped_early=False,
            )

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sa, "cut_session_clips", fake_cut)
            payload = run_clips_stage(ctx, session_id)
        assert seen["resolved_bytes"] != b""  # the resolver really downloads
        assert payload == {
            "balls": 2,
            "cameras": ["C1"],
            "cut": 2,
            "skipped": 0,
            "gaps": 0,
            "failed": 0,
            "pending": 0,
        }

    def test_ffmpeg_unavailable_fails_the_stage_honestly(
        self, ctx: WorkerContext, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def stopped(
            inner_ctx: WorkerContext, inner_session_id: uuid.UUID, *, source_resolver: Any
        ) -> ClipRunSummary:
            return ClipRunSummary(
                session_id=str(inner_session_id),
                balls=1,
                cameras=("C1",),
                cut=0,
                skipped=0,
                gaps=0,
                failed=0,
                pending=1,
                stopped_early=True,
            )

        monkeypatch.setattr(sa, "cut_session_clips", stopped)
        with pytest.raises(StageInputError, match="ffmpeg unavailable"):
            run_clips_stage(ctx, session_id)


# --------------------------------------------------------------- pose stage


class _StubMediapipe(types.ModuleType):
    __version__: str


def _install_mediapipe_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubMediapipe("mediapipe")
    stub.__version__ = "0.10.35-stub"
    monkeypatch.setitem(sys.modules, "mediapipe", stub)


class TestResolvePoseProvider:
    def test_unconfigured_asset_falls_back_to_the_fake_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_POSE_MODEL_ASSET, raising=False)
        provider, provenance, fallback = resolve_pose_provider()
        assert provenance == "fake-pose:1"
        assert fallback is not None and ENV_POSE_MODEL_ASSET in fallback
        assert provider.extract([b"f"] * 3, fps=30.0).frame_count == 3

    def test_missing_mediapipe_falls_back_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_POSE_MODEL_ASSET, "/models/pose.task")
        monkeypatch.delitem(sys.modules, "mediapipe", raising=False)
        _provider, provenance, fallback = resolve_pose_provider()
        assert provenance == "fake-pose:1"
        assert fallback is not None and "mediapipe unavailable" in fallback

    def test_configured_asset_builds_the_real_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_POSE_MODEL_ASSET, "/models/pose.task")
        _install_mediapipe_stub(monkeypatch)
        provider, provenance, fallback = resolve_pose_provider()
        assert provenance == "mediapipe-pose:0.10.35-stub"
        assert fallback is None
        assert type(provider).__name__ == "MediaPipePoseProvider"


class TestClipFramesResolver:
    def _seed_clip(
        self,
        ctx: WorkerContext,
        session_id: uuid.UUID,
        *,
        object_key: str | None,
        status: ClipStatus = ClipStatus.CUT,
    ) -> None:
        with ctx.session_factory() as db:
            db.add(
                Clip(
                    session_id=session_id,
                    ball_no=1,
                    camera_id="C1",
                    start_ms=0,
                    end_ms=300,
                    status=status,
                    object_key=object_key,
                )
            )
            db.commit()

    def test_no_cut_clip_row_is_a_loud_gap(self, ctx: WorkerContext, session_id: uuid.UUID) -> None:
        resolve = clip_frames_resolver(ctx)
        assert resolve(session_id, 1, "C1") is None
        self._seed_clip(ctx, session_id, object_key="clips/x.mp4", status=ClipStatus.PENDING)
        assert resolve(session_id, 1, "C1") is None

    def test_cut_clip_without_object_key_is_a_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        self._seed_clip(ctx, session_id, object_key=None)
        assert clip_frames_resolver(ctx)(session_id, 1, "C1") is None

    def test_missing_and_undecodable_bytes_are_gaps(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        self._seed_clip(ctx, session_id, object_key="clips/gone.mp4")
        resolve = clip_frames_resolver(ctx)
        assert resolve(session_id, 1, "C1") is None  # bytes missing from the store
        ctx.store.put("clips/gone.mp4", b"junk")
        assert resolve(session_id, 1, "C1") is None  # bytes undecodable

    def test_source_fps_wins_over_container_fps(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        key = f"clips/{session_id}/1-C1.mp4"
        _store_video(ctx, tmp_path, key, frames=6, fps=30.0)
        self._seed_clip(ctx, session_id, object_key=key)
        _add_video(ctx, session_id, "C1", probe={"fps": 120.0})
        resolved = clip_frames_resolver(ctx)(session_id, 1, "C1")
        assert resolved is not None
        frames, fps = resolved
        assert len(frames) == 6
        assert fps == 120.0  # the camera source's probed rate, not the re-encode's

    def test_container_fps_is_the_fallback(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        key = f"clips/{session_id}/1-C1.mp4"
        _store_video(ctx, tmp_path, key, frames=4, fps=30.0)
        self._seed_clip(ctx, session_id, object_key=key)  # no Video row at all
        resolved = clip_frames_resolver(ctx)(session_id, 1, "C1")
        assert resolved is not None
        assert resolved[1] == 30.0

    def test_no_usable_rate_anywhere_is_a_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_clip(ctx, session_id, object_key="clips/norate.mp4")
        monkeypatch.setattr(sa, "_decode_frames", lambda _ctx, _key: ([b"frame"], 0.0))
        assert clip_frames_resolver(ctx)(session_id, 1, "C1") is None

    def test_zero_frame_decode_is_a_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _EmptyCapture:
            def __init__(self, _path: str) -> None: ...

            def isOpened(self) -> bool:
                return True

            def get(self, _prop: int) -> float:
                return 30.0

            def read(self) -> tuple[bool, None]:
                return False, None

            def release(self) -> None: ...

        monkeypatch.setattr(sa.cv2, "VideoCapture", _EmptyCapture)
        self._seed_clip(ctx, session_id, object_key="clips/empty.mp4")
        ctx.store.put("clips/empty.mp4", b"header only")
        assert clip_frames_resolver(ctx)(session_id, 1, "C1") is None


class TestRunPoseStage:
    def test_extracts_from_cut_clips_with_the_fallback_recorded(
        self,
        ctx: WorkerContext,
        session_id: uuid.UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_POSE_MODEL_ASSET, raising=False)
        _add_event(ctx, session_id, 1)
        key = f"clips/{session_id}/1-C1.mp4"
        _store_video(ctx, tmp_path, key, frames=6, fps=30.0)
        with ctx.session_factory() as db:
            db.add(
                Clip(
                    session_id=session_id,
                    ball_no=1,
                    camera_id="C1",
                    start_ms=10_000,
                    end_ms=12_000,
                    status=ClipStatus.CUT,
                    object_key=key,
                )
            )
            db.commit()
        payload = run_pose_stage(ctx, session_id)
        assert payload["provider"] == "fake-pose:1"
        assert payload["provider_fallback"] is not None  # honest: no real model configured
        assert payload["extracted"] == 1  # C1 has a clip
        assert payload["missing"] == [[1, "C3"]]  # the second default camera has none
        assert payload["low_availability"] == []


# ------------------------------------------------------- detection provider


class _StubYolo:
    def __init__(self, weights: str) -> None:
        self.weights = weights


class _UltralyticsStub(types.ModuleType):
    YOLO: type[_StubYolo]
    __version__: str


def _install_ultralytics_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _UltralyticsStub("ultralytics")
    stub.YOLO = _StubYolo
    stub.__version__ = "8.4.90-stub"
    monkeypatch.setitem(sys.modules, "ultralytics", stub)


def _register_production_model(
    ctx: WorkerContext,
    *,
    report_key: str | None = "models/ball-detector/runs/r1/eval-report.json",
    dangling_run: bool = False,
) -> None:
    with ctx.session_factory() as db:
        dataset = Dataset(version="v1", frozen=True)
        db.add(dataset)
        db.flush()
        run_id = uuid.uuid4()
        if not dangling_run:
            db.add(
                ModelRun(
                    id=run_id,
                    model_name=DETECTOR_MODEL_NAME,
                    dataset_id=dataset.id,
                    status=TrainingStatus.SUCCEEDED,
                    trainer_version="t1",
                    report_key=report_key,
                )
            )
        db.add(
            ModelVersion(
                model_name=DETECTOR_MODEL_NAME,
                version="1.0",
                run_id=run_id,
                stage=ModelStage.PRODUCTION,
            )
        )
        db.commit()


class TestProductionDetector:
    def test_empty_registry_reports_the_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and "no production" in reason

    def test_dangling_run_reports_the_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        _register_production_model(ctx, dangling_run=True)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and "no eval-report artifact" in reason

    def test_run_without_report_key_reports_the_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        _register_production_model(ctx, report_key=None)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and "no eval-report artifact" in reason

    def test_no_evidence_footage_reports_the_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        _register_production_model(ctx)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and "no evidence footage" in reason

    @pytest.mark.parametrize(
        ("report_bytes", "match"),
        [
            (None, "missing from the object store"),
            (b"{not json", "not valid JSON"),
            (b"[1, 2]", "names no weights artifact"),
            (b'{"artifacts": "x"}', "names no weights artifact"),
            (b'{"artifacts": {"weights_key": 5}}', "names no weights artifact"),
        ],
    )
    def test_unusable_eval_report_reports_the_gap(
        self,
        ctx: WorkerContext,
        session_id: uuid.UUID,
        tmp_path: Path,
        report_bytes: bytes | None,
        match: str,
    ) -> None:
        _register_production_model(ctx)
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0})
        if report_bytes is not None:
            ctx.store.put("models/ball-detector/runs/r1/eval-report.json", report_bytes)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and match in reason

    def _seed_full_registry(
        self, ctx: WorkerContext, session_id: uuid.UUID, *, with_weights: bool, with_source: bool
    ) -> None:
        _register_production_model(ctx)
        source_key = f"videos/{session_id}/C1.mp4"
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0}, object_key=source_key)
        report = {"artifacts": {"weights_key": "models/ball-detector/runs/r1/best.pt"}}
        ctx.store.put("models/ball-detector/runs/r1/eval-report.json", json.dumps(report).encode())
        if with_weights:
            ctx.store.put("models/ball-detector/runs/r1/best.pt", b"weights-bytes")
        if with_source:
            ctx.store.put(source_key, b"source-bytes")

    def test_missing_weights_reports_the_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        self._seed_full_registry(ctx, session_id, with_weights=False, with_source=True)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and "weights" in reason and "missing" in reason

    def test_missing_source_video_reports_the_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        self._seed_full_registry(ctx, session_id, with_weights=True, with_source=False)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and "source video" in reason

    def test_ultralytics_genuinely_missing_reports_the_gap(
        self, ctx: WorkerContext, session_id: uuid.UUID, tmp_path: Path
    ) -> None:
        self._seed_full_registry(ctx, session_id, with_weights=True, with_source=True)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert provider is None
        assert reason is not None and "ultralytics" in reason

    def test_full_chain_builds_the_real_provider(
        self,
        ctx: WorkerContext,
        session_id: uuid.UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_full_registry(ctx, session_id, with_weights=True, with_source=True)
        _install_ultralytics_stub(monkeypatch)
        provider, reason = production_detector(ctx, session_id, tmp_path)
        assert reason is None
        assert provider is not None
        assert provider.version == "yolo-best-ultralytics-8.4.90-stub"
        assert (tmp_path / "best.pt").read_bytes() == b"weights-bytes"  # really downloaded


class TestVideoFrameSource:
    def test_reads_one_frame_per_grid_slot(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        _write_video(video, frames=30, fps=30.0)
        frames = VideoFrameSource(video).frames(start_ms=0.0, end_ms=300.0, fps=30.0)
        assert len(frames) == 10  # int(300 / 33.3) + 1

    def test_window_past_footage_end_returns_what_exists(self, tmp_path: Path) -> None:
        video = tmp_path / "v.mp4"
        _write_video(video, frames=5, fps=30.0)
        frames = VideoFrameSource(video).frames(start_ms=0.0, end_ms=1000.0, fps=30.0)
        assert 0 < len(frames) <= 5  # honest shortfall; the yolo adapter fails it loudly

    def test_undecodable_source_fails_loud(self, tmp_path: Path) -> None:
        video = tmp_path / "junk.mp4"
        video.write_bytes(b"junk")
        with pytest.raises(StageInputError, match="cannot be decoded"):
            VideoFrameSource(video).frames(start_ms=0.0, end_ms=100.0, fps=30.0)


# ------------------------------------------------------- detect_track stage


class TestRunDetectTrackStage:
    def test_tracks_with_the_fake_fallback_recorded(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        _add_event(ctx, session_id, 1)
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0, "width": 1920, "height": 1080})
        payload = run_detect_track_stage(ctx, session_id)
        assert payload["detector"] == "fake-detect-1"
        assert payload["detector_fallback"] is not None
        assert "no production" in payload["detector_fallback"]
        assert payload["tracked"] == 1
        assert payload["pixel_only_cameras"][0][0] == "C1"  # no calibration: loud
        assert payload["skipped_cameras"] == []
        assert payload["failed"] == []
        with ctx.session_factory() as db:
            rows = db.scalars(select(BallTrack).where(BallTrack.session_id == session_id)).all()
        assert [(row.ball_no, row.camera_id) for row in rows] == [(1, "C1")]

    def test_production_provider_is_used_when_available(
        self, ctx: WorkerContext, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add_event(ctx, session_id, 1)
        _add_video(ctx, session_id, "C1", probe={"fps": 30.0, "width": 1920, "height": 1080})
        provider = FakeDetectionProvider(seed=99)

        def resolved(
            _ctx: WorkerContext, _session_id: uuid.UUID, _workdir: Path
        ) -> tuple[FakeDetectionProvider, None]:
            return provider, None

        monkeypatch.setattr(sa, "production_detector", resolved)
        payload = run_detect_track_stage(ctx, session_id)
        assert payload["detector"] == provider.version
        assert payload["detector_fallback"] is None
        assert payload["tracked"] == 1


# ------------------------------------------------------------ metrics stage


def _seed_track_payload(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> None:
    """A minimal valid US-F3 payload + row so fusion has a track modality."""
    start_ms = float(ball_no * 10_000)
    points = [
        {
            "frame_no": index,
            "ts_ms": start_ms + 400.0 + 30.0 * index,
            "px_x": 100.0 + 10.0 * index,
            "px_y": 400.0 + 20.0 * index,
            "score": 0.9,
            "bridged": False,
        }
        for index in range(9)
    ]
    segments = [
        {"kind": "pre_bounce", "start_ms": start_ms, "end_ms": start_ms + 500.0, "confidence": 0.9},
        {
            "kind": "post_bounce",
            "start_ms": start_ms + 500.0,
            "end_ms": start_ms + 2000.0,
            "confidence": 0.9,
        },
    ]
    payload = {
        "points": points,
        "segments": segments,
        "flags": {"identity_risk": False, "long_gap": False},
    }
    key = f"sessions/{session_id}/balls/{ball_no}/track-C1.json"
    ctx.store.put(key, json.dumps(payload).encode())
    with ctx.session_factory() as db:
        db.add(
            BallTrack(
                session_id=session_id,
                ball_no=ball_no,
                camera_id="C1",
                tracker_version="t1",
                points_key=key,
                coverage=1.0,
                segments=segments,
                flags={},
                confidence=0.9,
            )
        )
        db.commit()


class TestRunMetricsStage:
    def test_fuses_without_a_detector_honestly(
        self, ctx: WorkerContext, session_id: uuid.UUID
    ) -> None:
        _add_event(ctx, session_id, 1)
        _add_event(ctx, session_id, 2)
        _seed_track_payload(ctx, session_id, 1)
        payload = run_metrics_stage(ctx, session_id)
        assert payload["detector"] is None
        assert payload["detector_fallback"] is not None  # the registry gap, recorded
        assert payload["fused_count"] == 1
        fused = payload["fused"][0]
        assert fused["ball_no"] == 1
        assert fused["bat_path"] is None  # no detector: null-with-reason, never fake
        assert payload["skipped"] == [[2, "no ball track row"]]

    def test_production_detector_feeds_the_bat_modality(
        self, ctx: WorkerContext, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add_event(ctx, session_id, 1)
        _seed_track_payload(ctx, session_id, 1)
        provider = FakeDetectionProvider()

        def resolved(
            _ctx: WorkerContext, _session_id: uuid.UUID, _workdir: Path
        ) -> tuple[FakeDetectionProvider, None]:
            return provider, None

        monkeypatch.setattr(sa, "production_detector", resolved)
        payload = run_metrics_stage(ctx, session_id)
        assert payload["detector"] == provider.version
        assert payload["detector_fallback"] is None
        assert payload["fused_count"] == 1


# ---------------------------------------------------- bowling metric stages


def _bowling_session(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Mira", birthdate=datetime.date(2014, 3, 2))
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 10),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
            expected_cameras=["C5"],
        )
        db.add_all([player, session])
        db.commit()
        return session.id


BOWLING_STAGES = (
    run_bowling_action_stage,
    run_bowling_flight_stage,
    run_classify_variations_stage,
)


class TestBowlingStageGate:
    @pytest.mark.parametrize("stage", BOWLING_STAGES)
    def test_missing_session_raises_lookup_error(self, ctx: WorkerContext, stage: Any) -> None:
        with pytest.raises(LookupError, match="not found"):
            stage(ctx, uuid.uuid4())

    @pytest.mark.parametrize("stage", BOWLING_STAGES)
    def test_non_bowling_sessions_are_honest_gated_no_ops(
        self, ctx: WorkerContext, session_id: uuid.UUID, stage: Any
    ) -> None:
        """US-I2-I6 x US-J1: a batting session's trace SAYS why nothing was
        written — the gated no-op payload — and no ball_metrics row appears."""
        assert stage(ctx, session_id) == {"skipped": "not a bowling session"}
        with ctx.session_factory() as db:
            assert db.scalar(select(BallMetrics)) is None


class TestRunBowlingActionStage:
    def test_builds_the_pose_seam_and_writes_the_pinned_metrics(
        self, ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """US-I2/I3 wiring (Phase-6 findings 0/45): the adapter hands the
        production provider + C5 clip frames to the action job; detected,
        undetected and footage-less balls all land honestly in the payload."""
        session_id = _bowling_session(ctx)
        for ball_no in (1, 2, 3):
            _add_event(ctx, session_id, ball_no)

        def fake_resolver_factory(_ctx: WorkerContext) -> Any:
            def resolve(
                _session_id: uuid.UUID, ball_no: int, camera_id: str
            ) -> tuple[list[int], float] | None:
                assert camera_id == "C5"  # the bowling release view (US-I1)
                if ball_no == 1:
                    return (list(range(N_FRAMES)), SWING_FPS)  # full swing: detected
                if ball_no == 2:
                    return ([0, 1, 2], SWING_FPS)  # arm never overhead: undetected
                return None  # ball 3: no footage

            return resolve

        monkeypatch.setattr(sa, "clip_frames_resolver", fake_resolver_factory)
        monkeypatch.setattr(
            sa,
            "resolve_pose_provider",
            lambda: (SwingPoseProvider(), "fake-bowler-pose:1", "no pose model asset (test)"),
        )
        payload = run_bowling_action_stage(ctx, session_id)
        assert payload["provider"] == "fake-bowler-pose:1"
        assert payload["provider_fallback"] == "no pose model asset (test)"
        assert payload["analyzed"] == 2
        assert [ball_no for ball_no, _reason in payload["undetected"]] == [2]
        assert payload["skipped"] == [[3, "no footage for the bowling camera"]]
        with ctx.session_factory() as db:
            rows = db.scalars(
                select(BallMetrics).where(BallMetrics.phase == MetricPhase.PRE_RELEASE)
            ).all()
            assert {row.ball_no for row in rows} == {1, 2}
            for row in rows:
                assert set(row.metrics) == set(BOWLING_ACTION_KEYS)
        assert rows[0].metrics["release_frame"]["value"] is not None  # ball 1 detected


class TestRunBowlingFlightStage:
    def test_counts_measured_metrics_and_scores_the_target(self, ctx: WorkerContext) -> None:
        """US-I4/I5 wiring: flight metrics land under phase='flight'; the
        payload counts measured-vs-honest-null without inventing numbers."""
        session_id = _bowling_session(ctx)
        _add_event(ctx, session_id, 1)
        with ctx.session_factory() as db:
            db.add(
                BowlingTarget(
                    session_id=session_id,
                    line=Line.OFF,
                    length=Length.GOOD,
                    description="good length outside off",
                    created_by="coach",
                )
            )
            db.add(
                BounceEstimate(
                    session_id=session_id,
                    ball_no=1,
                    pitch_x=6.0,
                    pitch_y=0.2,
                    confidence=0.9,
                    tracker_version="test",
                )
            )
            db.commit()
        payload = run_bowling_flight_stage(ctx, session_id)
        assert payload == {
            "balls": 1,
            "turn_measured": 0,  # no track: null-with-reason, honestly uncounted
            "apex_measured": 0,
            "dip_measured": 0,
            "targets_scored": 1,
        }
        with ctx.session_factory() as db:
            row = db.scalars(
                select(BallMetrics).where(BallMetrics.phase == MetricPhase.FLIGHT)
            ).one()
            assert row.metrics["target_hit"]["value"] is True
            assert row.metrics["turn_cm"]["value"] is None


class TestRunClassifyVariationsStage:
    def test_reports_the_honest_classification_summary(self, ctx: WorkerContext) -> None:
        """US-I6 wiring: labeled balls classify from stored features; a ball
        without a measured trajectory is skipped loudly, never force-labeled."""
        session_id = _bowling_session(ctx)
        with ctx.session_factory() as db:
            db.add(
                BallMetrics(
                    session_id=session_id,
                    ball_no=1,
                    phase=MetricPhase.FLIGHT,
                    metrics={"turn_cm": {"value": 12.0, "unit": "cm", "confidence": 0.9}},
                )
            )
            db.add(
                DeliveryLabel(
                    session_id=session_id,
                    ball_no=1,
                    variation_intent=BowlingVariation.LEG_BREAK,
                    labeler="coach",
                )
            )
            db.add(
                DeliveryLabel(
                    session_id=session_id,
                    ball_no=2,
                    variation_intent=BowlingVariation.GOOGLY,
                    labeler="coach",
                )
            )
            db.commit()
        payload = run_classify_variations_stage(ctx, session_id)
        assert payload["classified"] + payload["unclear"] == 1  # ball 1 got a verdict
        assert payload["cleared"] == 0
        assert payload["skipped"] == [
            [2, "ball 2: no stored turn_cm metric (trajectory not measured)"]
        ]
        assert isinstance(payload["gate_met"], bool)
        assert payload["classifier_version"]  # provenance always recorded
