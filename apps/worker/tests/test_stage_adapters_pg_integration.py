"""IT (US-J1/L1, Phase-6 adapters): the FULL DAG over real media on real PostgreSQL.

A synthetic session (``cricai_data.synthetic``) drives a tiny cv2-generated
multi-ball video: one moving-block burst per synthetic ball on the reference
camera. The pipeline then runs with the PRODUCTION wiring (no registry
injection): the events adapter computes motion energy from the stored bytes,
real ffmpeg cuts the per-ball clips, the pose adapter decodes those clips
(fake provider, fallback recorded — no MediaPipe asset in CI), the tracker
runs on the fake detection fallback (no production model registered — reason
recorded), fusion runs detector-less, and the agent tail ships a report.
Requires ffmpeg (the ``integration`` marker's service contract).

The BOWLING variant (US-I2-I7, Phase-6 findings 0/4/29/45) drives the same
synthetic media through the C5 bowling camera: the three bowling stages
execute for real, the bowling ball_metrics keys land under their pinned
names, and the shipped report's accuracy scorecard counts the deliveries —
the batting run instead proves those stages are honest gated no-ops.
"""

from __future__ import annotations

import datetime
import uuid as uuid_module
from pathlib import Path

import cv2
import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from cricai_coaching.bowling_report import RELEASE_SCATTER_NOTE
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    BowlingVariation,
    ClipStatus,
    Handedness,
    Length,
    Line,
    MetricPhase,
    SessionType,
    StageStatus,
    VideoStatus,
)
from cricai_data.models import (
    BallEvent,
    BallMetrics,
    BallTrack,
    BounceMark,
    BowlingTarget,
    Clip,
    DeliveryLabel,
    PipelineStage,
    Player,
    PoseTrack,
    Report,
    Video,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_data.synthetic import BlockSpec, generate_session
from cricai_vision.release import REASON_ARM_NEVER_OVERHEAD
from cricai_worker.bowling_action import BOWLING_ACTION_KEYS
from cricai_worker.bowling_flight import BOWLING_FLIGHT_KEYS
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import PipelineOutcome, RunPolicy, run_pipeline
from cricai_worker.stage_adapters import SKIPPED_NOT_BOWLING
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.golden]

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"

FPS = 30.0
LEAD_FRAMES = 45  # 1.5 s of stillness: > the segmenter's 1 s min gap
BURST_FRAMES = 30  # 1 s of motion per ball: inside min/max event duration


def _write_session_video(path: Path, n_balls: int) -> int:
    """A 64x48 mp4 with one moving-block burst per ball; returns frame count."""
    total = LEAD_FRAMES + n_balls * (BURST_FRAMES + LEAD_FRAMES)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (64, 48))
    assert writer.isOpened()
    for index in range(total):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        cycle = (index - LEAD_FRAMES) % (BURST_FRAMES + LEAD_FRAMES)
        if index >= LEAD_FRAMES and cycle < BURST_FRAMES:
            x = (cycle * 3) % 50
            frame[10:30, x : x + 10] = 255
        writer.write(frame)
    writer.release()
    return total


def _seed_media_session(ctx: WorkerContext, tmp_path: Path, n_balls: int) -> uuid_module.UUID:
    """One C1-only session whose stored video carries ``n_balls`` motion bursts."""
    local = tmp_path / "c1.mp4"
    frames = _write_session_video(local, n_balls)
    key = "videos/it/C1.mp4"
    ctx.store.put(key, local.read_bytes())
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
        db.flush()
        db.add(
            Video(
                session_id=session.id,
                camera_id="C1",
                object_key=key,
                filename="c1.mp4",
                checksum_sha256="a" * 64,
                size_bytes=local.stat().st_size,
                status=VideoStatus.PROBED,
                probe={
                    "fps": FPS,
                    "width": 64,
                    "height": 48,
                    "resolution": "64x48",
                    "codec": "mp4v",
                    "duration_s": frames / FPS,
                },
            )
        )
        db.commit()
        return session.id


def _assert_media_rows(ctx: WorkerContext, session_id: uuid_module.UUID, n_balls: int) -> None:
    """Every media stage left real, consistent rows + store artifacts behind."""
    ball_range = set(range(1, n_balls + 1))
    with ctx.session_factory() as db:
        events = db.scalars(
            select(BallEvent).where(BallEvent.session_id == session_id).order_by(BallEvent.ball_no)
        ).all()
        assert [event.ball_no for event in events] == sorted(ball_range)

        clips = db.scalars(select(Clip).where(Clip.session_id == session_id)).all()
        cut = [clip for clip in clips if clip.status is ClipStatus.CUT]
        assert len(cut) == n_balls  # real ffmpeg cut every ball on C1
        for clip in cut:
            assert clip.object_key is not None
            assert ctx.store.exists(clip.object_key)

        poses = db.scalars(select(PoseTrack).where(PoseTrack.session_id == session_id)).all()
        assert {(row.ball_no, row.camera_id) for row in poses} == {(n, "C1") for n in ball_range}
        assert all(row.model_name == "fake-pose" for row in poses)  # honest provenance

        tracks = db.scalars(select(BallTrack).where(BallTrack.session_id == session_id)).all()
        assert {(row.ball_no, row.camera_id) for row in tracks} == {(n, "C1") for n in ball_range}
        assert all(ctx.store.exists(row.points_key) for row in tracks)

        metrics = db.scalars(select(BallMetrics).where(BallMetrics.session_id == session_id)).all()
        assert {row.ball_no for row in metrics} == ball_range
        for row in metrics:
            assert "contact_quality" in row.metrics
            # US-F5 honesty: no detector configured -> null-with-reason, never fake.
            assert row.metrics["bat_path"]["value"] is None

        report = db.scalar(select(Report).where(Report.session_id == session_id))
        assert report is not None  # the agent tail shipped over the real media run


def _assert_honest_trace(ctx: WorkerContext, outcome: PipelineOutcome, n_balls: int) -> None:
    """The stage trace records the real derivations and every model fallback."""
    with ctx.session_factory() as db:
        rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == outcome.run_id)).all()
    payloads = {
        row.stage: (row.output or {}).get("payload", {})
        for row in rows
        if row.status is StageStatus.SUCCEEDED
    }
    assert payloads["events"]["motion_energy_source"] == "video"  # computed, not seeded
    assert payloads["events"]["detected"] == n_balls
    assert payloads["pose"]["provider"] == "fake-pose:1"
    assert payloads["pose"]["provider_fallback"] is not None
    assert payloads["detect_track"]["detector"] == "fake-detect-1"
    assert "no production" in payloads["detect_track"]["detector_fallback"]
    assert payloads["metrics"]["detector"] is None
    assert payloads["metrics"]["fused_count"] == n_balls
    # A batting session's bowling stages are honest gated no-ops: the trace
    # says WHY nothing bowling-related was written (US-I2-I6 x US-J1).
    for stage in ("bowling_action", "bowling_flight", "classify_variations"):
        assert payloads[stage] == SKIPPED_NOT_BOWLING


def test_full_dag_runs_the_real_media_stages_over_a_synthetic_session(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    monkeypatch.delenv("CRICAI_POSE_MODEL_ASSET", raising=False)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    ctx = WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )
    n_balls = len(generate_session(4242, (BlockSpec(n_balls=3),)).balls)
    session_id = _seed_media_session(ctx, tmp_path, n_balls)

    outcome = run_pipeline(ctx, session_id, policy=RunPolicy(max_attempts=1))
    fate = {s.stage: s for s in outcome.stages}

    # Every media stage EXECUTED for real — no signature failures, no skips.
    media = ("probe", "calibrate_check", "events", "clips", "pose", "detect_track", "metrics")
    for stage in media:
        assert fate[stage].status is StageStatus.SUCCEEDED, (stage, fate[stage].error)
        assert fate[stage].resumed is False
    assert outcome.status == "succeeded"

    _assert_media_rows(ctx, session_id, n_balls)
    _assert_honest_trace(ctx, outcome, n_balls)


# --------------------------------------------------------- BOWLING full DAG


def _seed_bowling_media_session(
    ctx: WorkerContext, tmp_path: Path, n_balls: int
) -> uuid_module.UUID:
    """A C5-only BOWLING session: synthetic media plus the coach-declared
    ground truth the bowling stages consume — one session-wide target, a
    manual bounce mark per ball (manual beats machine, US-F4) and one
    delivery-intent label. The player bats LEFT-handed on purpose: bowling
    zone analytics and target scoring share the fixed canonical right-hand
    frame, so their handedness must not matter (US-I4)."""
    local = tmp_path / "c5.mp4"
    frames = _write_session_video(local, n_balls)
    key = "videos/it/C5.mp4"
    ctx.store.put(key, local.read_bytes())
    with ctx.session_factory() as db:
        player = Player(
            name="Mira", birthdate=datetime.date(2014, 3, 2), handedness=Handedness.LEFT
        )
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 9),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
            expected_cameras=["C5"],
        )
        db.add_all([player, session])
        db.flush()
        db.add(
            Video(
                session_id=session.id,
                camera_id="C5",
                object_key=key,
                filename="c5.mp4",
                checksum_sha256="b" * 64,
                size_bytes=local.stat().st_size,
                status=VideoStatus.PROBED,
                probe={
                    "fps": FPS,
                    "width": 64,
                    "height": 48,
                    "resolution": "64x48",
                    "codec": "mp4v",
                    "duration_s": frames / FPS,
                },
            )
        )
        db.add(
            BowlingTarget(
                session_id=session.id,
                line=Line.OFF,
                length=Length.GOOD,
                description="good length outside off",
                created_by="coach",
            )
        )
        for ball_no in range(1, n_balls + 1):
            # (6.0, +0.2) is OFF/GOOD in the canonical right-hand frame.
            db.add(
                BounceMark(
                    session_id=session.id,
                    ball_no=ball_no,
                    camera_id="C5",
                    frame_no=30 * ball_no,
                    px_x=320.0,
                    px_y=400.0,
                    pitch_x=6.0,
                    pitch_y=0.2,
                )
            )
        db.add(
            DeliveryLabel(
                session_id=session.id,
                ball_no=1,
                variation_intent=BowlingVariation.LEG_BREAK,
                labeler="coach",
            )
        )
        db.commit()
        return session.id


def _assert_bowling_metrics_landed(
    ctx: WorkerContext, session_id: uuid_module.UUID, n_balls: int
) -> None:
    """Every ball carries the pinned bowling metric names in both phases."""
    ball_range = set(range(1, n_balls + 1))
    with ctx.session_factory() as db:
        pre_release = db.scalars(
            select(BallMetrics).where(
                BallMetrics.session_id == session_id,
                BallMetrics.phase == MetricPhase.PRE_RELEASE,
            )
        ).all()
        assert {row.ball_no for row in pre_release} == ball_range
        for row in pre_release:
            assert set(row.metrics) == set(BOWLING_ACTION_KEYS)  # exact pinned names
        flight = db.scalars(
            select(BallMetrics).where(
                BallMetrics.session_id == session_id,
                BallMetrics.phase == MetricPhase.FLIGHT,
            )
        ).all()
        assert {row.ball_no for row in flight} == ball_range
        for row in flight:
            assert set(row.metrics) == set(BOWLING_FLIGHT_KEYS)
            # The manual mark at (6.0, +0.2) hits the declared OFF/GOOD target
            # in the canonical frame — for a LEFT-handed kid too (US-I4).
            assert row.metrics["target_hit"]["value"] is True
            assert row.metrics["target_hit"]["source"].startswith("manual+")


def test_full_dag_runs_the_bowling_stages_over_a_synthetic_bowling_session(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase-6 findings 0/4/29/45: a BOWLING session's pipeline runs the three
    bowling stages for real — bowling ball_metrics keys land under their
    pinned names and the shipped report's scorecard counts the deliveries.

    Phase-7 T5 #4 extension (US-I3/I7): the report body's ``release_scatter``
    block renders — full pinned shape, points recomputed from the stored
    pre-release rows, honest nulls where nothing was measured."""
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    monkeypatch.delenv("CRICAI_POSE_MODEL_ASSET", raising=False)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    ctx = WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )
    n_balls = len(generate_session(4242, (BlockSpec(n_balls=3),)).balls)
    session_id = _seed_bowling_media_session(ctx, tmp_path, n_balls)

    outcome = run_pipeline(ctx, session_id, policy=RunPolicy(max_attempts=1))
    fate = {s.stage: s for s in outcome.stages}

    assert outcome.status == "succeeded"
    for stage in ("bowling_action", "bowling_flight", "classify_variations"):
        assert fate[stage].status is StageStatus.SUCCEEDED, (stage, fate[stage].error)
        assert fate[stage].resumed is False  # coach-mutable inputs: never resumed

    _assert_bowling_metrics_landed(ctx, session_id, n_balls)

    with ctx.session_factory() as db:
        rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == outcome.run_id)).all()
        payloads = {
            row.stage: (row.output or {}).get("payload", {})
            for row in rows
            if row.status is StageStatus.SUCCEEDED
        }
        report = db.scalar(select(Report).where(Report.session_id == session_id))
    # The action stage really built the pose seam (fake provider, loudly).
    assert payloads["bowling_action"]["provider"] == "fake-pose:1"
    assert payloads["bowling_action"]["provider_fallback"] is not None
    assert payloads["bowling_action"]["analyzed"] == n_balls
    assert payloads["bowling_flight"]["balls"] == n_balls
    assert payloads["bowling_flight"]["targets_scored"] == n_balls
    # The labeled ball has no measured trajectory here: skipped loudly, never
    # force-classified (US-I6 honesty).
    assert [entry[0] for entry in payloads["classify_variations"]["skipped"]] == [1]
    assert "turn_cm" in payloads["classify_variations"]["skipped"][0][1]

    # US-I7/K5: the shipped report's scorecard counts the real deliveries.
    assert report is not None
    scorecard = report.body["bowling"]["accuracy_scorecard"]
    assert scorecard["counted"] == n_balls
    assert scorecard["total"] == n_balls
    assert scorecard["overall"] == {"hits": n_balls, "n": n_balls, "pct": 100.0}

    # T5 #4 (US-I3/I7): the release scatter renders — the report body ships the
    # full pinned scatter block, cross-checked below against the per-ball
    # release-height rows this session's pipeline run actually stored.
    scatter = report.body["bowling"]["release_scatter"]
    assert set(scatter) == {
        "n",
        "mean_cm",
        "sigma_cm",
        "min_cm",
        "max_cm",
        "points",
        "by_variation",
        "note",
    }
    assert scatter["note"] == RELEASE_SCATTER_NOTE
    with ctx.session_factory() as db:
        stored = {
            row.ball_no: row.metrics["release_height_cm"]
            for row in db.scalars(
                select(BallMetrics).where(
                    BallMetrics.session_id == session_id,
                    BallMetrics.phase == MetricPhase.PRE_RELEASE,
                )
            )
        }
    assert set(stored) == set(range(1, n_balls + 1))
    assert scatter["n"] == len(scatter["points"])
    # In this rig every stored height is HONESTLY null: the deterministic fake
    # pose provider never lifts the bowling wrist above the shoulder, and the
    # production wiring carries no stereo or calibration-scale seam — so the
    # scatter ships with zero points and null stats, never a fabricated number
    # (US-I2 no-silent-zeros; the ReportView fixture test covers the populated
    # render of this same block shape).
    for ball_no in sorted(stored):
        assert stored[ball_no]["value"] is None
        assert stored[ball_no]["reason"] == REASON_ARM_NEVER_OVERHEAD
    assert scatter["points"] == []
    assert scatter["by_variation"] == []
    assert (scatter["mean_cm"], scatter["sigma_cm"]) == (None, None)
    assert (scatter["min_cm"], scatter["max_cm"]) == (None, None)
