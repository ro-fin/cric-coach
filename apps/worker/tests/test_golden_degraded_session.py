"""T5 #3 golden: the degraded session — dead camera + missing audio, honest end to end.

A batting session EXPECTS C1+C3 but only C1 footage exists (C3 is the dead
camera) and the C1 recording carries no audio track (its probe records no
``audio_onsets_ms``). The FULL production DAG (``run_pipeline``, no registry
injection) runs over real temp PostgreSQL and real ffmpeg, and this golden pins
the designed degraded-but-honest semantics (US-J1):

- the run COMPLETES with ``status == "succeeded"`` — a missing expected camera
  is data, not failure — while the ``probe`` stage marks the session
  ``degraded`` and names the missing camera (US-J1 AC);
- the ``clips`` stage records one loud :attr:`~cricai_data.enums.ClipStatus.GAP`
  row per ball for the dead camera with the reason in ``error`` — never a
  silent absence (US-D2 AC);
- the missing audio is honest at every level: the events stage reports
  ``audio_onsets: None`` and no fused ball ever names ``audio`` as a modality
  (US-F5 — a missing feed narrows provenance, it never fabricates a vote);
- ZERO fabricated metrics: no pose/track/clip artifact exists for the dead
  camera, and every absent metric value is null-WITH-REASON (US-E2/E3/F5 —
  ``bat_path`` names why no detector evidence exists); model fallbacks are
  recorded in the trace, never silent (US-E1/F3);
- the shipped report leads with the US-L4 honesty banner (thin data — single
  camera, no exposure samples, no calibration) and ships NO correction, drill,
  goal or numeric claim: nothing coached is invented from degraded data (US-G3).

Requires ffmpeg + PostgreSQL (the ``integration`` service contract); golden —
release-gating, deterministic (seeded synthetic session, fixed dates).
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
from cricai_coaching.quality import HONESTY_BANNER_THRESHOLD
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    ClipStatus,
    SessionType,
    StageStatus,
    VideoStatus,
)
from cricai_data.models import (
    BallMetrics,
    BallTrack,
    Clip,
    PipelineStage,
    Player,
    PoseTrack,
    Report,
    Video,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_data.synthetic import BlockSpec, generate_session
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import STAGES, RunPolicy, run_pipeline
from sqlalchemy import select

pytestmark = [pytest.mark.golden, pytest.mark.integration]

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"

FPS = 30.0
LEAD_FRAMES = 45  # 1.5 s of stillness: > the segmenter's 1 s min gap
BURST_FRAMES = 30  # 1 s of motion per ball: inside min/max event duration

#: The camera that recorded footage (audio-less) and the dead expected camera.
LIVE_CAMERA = "C1"
DEAD_CAMERA = "C3"


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


def _seed_degraded_session(ctx: WorkerContext, tmp_path: Path, n_balls: int) -> uuid_module.UUID:
    """A session expecting C1+C3 where only C1 uploaded — and C1 has no audio.

    The C1 probe payload deliberately carries NO ``audio_onsets_ms`` key: that
    is exactly what a camera with a dead/absent microphone produces, so the
    audio gap is data the pipeline must degrade around honestly (US-J1/F5).
    """
    local = tmp_path / "c1.mp4"
    frames = _write_session_video(local, n_balls)
    key = "videos/golden/degraded-C1.mp4"
    ctx.store.put(key, local.read_bytes())
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            expected_cameras=[LIVE_CAMERA, DEAD_CAMERA],
        )
        db.add_all([player, session])
        db.flush()
        db.add(
            Video(
                session_id=session.id,
                camera_id=LIVE_CAMERA,
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
                    # NO "audio_onsets_ms": the C1 recording has no audio.
                },
            )
        )
        db.commit()
        return session.id


def _assert_gap_rows_loud(ctx: WorkerContext, session_id: uuid_module.UUID, n_balls: int) -> None:
    """US-D2 AC: the dead camera left one loud GAP row per ball, nothing else."""
    ball_range = set(range(1, n_balls + 1))
    with ctx.session_factory() as db:
        clips = db.scalars(select(Clip).where(Clip.session_id == session_id)).all()
    gaps = [clip for clip in clips if clip.status is ClipStatus.GAP]
    assert {(clip.ball_no, clip.camera_id) for clip in gaps} == {
        (ball_no, DEAD_CAMERA) for ball_no in ball_range
    }
    for clip in gaps:
        assert clip.object_key is None  # a GAP never points at footage
        assert clip.error == f"no video uploaded for camera {DEAD_CAMERA}"
    cut = [clip for clip in clips if clip.status is ClipStatus.CUT]
    assert {(clip.ball_no, clip.camera_id) for clip in cut} == {
        (ball_no, LIVE_CAMERA) for ball_no in ball_range
    }
    for clip in cut:
        assert clip.object_key is not None
        assert ctx.store.exists(clip.object_key)


def _assert_nothing_fabricated_for_dead_camera(
    ctx: WorkerContext, session_id: uuid_module.UUID, n_balls: int
) -> None:
    """No pose or track artifact exists for C3 — absence stays absence (US-E1/F3)."""
    ball_range = set(range(1, n_balls + 1))
    with ctx.session_factory() as db:
        poses = db.scalars(select(PoseTrack).where(PoseTrack.session_id == session_id)).all()
        tracks = db.scalars(select(BallTrack).where(BallTrack.session_id == session_id)).all()
    assert {(row.ball_no, row.camera_id) for row in poses} == {
        (ball_no, LIVE_CAMERA) for ball_no in ball_range
    }
    assert all(row.model_name == "fake-pose" for row in poses)  # honest provenance
    assert {(row.ball_no, row.camera_id) for row in tracks} == {
        (ball_no, LIVE_CAMERA) for ball_no in ball_range
    }


def _assert_metrics_null_with_reason(ctx: WorkerContext, session_id: uuid_module.UUID) -> None:
    """US-E3/F5: every absent metric is null-WITH-REASON — zero silent zeros."""
    with ctx.session_factory() as db:
        rows = db.scalars(select(BallMetrics).where(BallMetrics.session_id == session_id)).all()
    assert rows  # the metrics stage really wrote per-ball rows
    for row in rows:
        for name, payload in row.metrics.items():
            if payload["value"] is None:
                reason = payload.get("reason")
                assert isinstance(reason, str) and reason, (row.ball_no, name, payload)
        # No production detector is registered, so bat_path is honestly absent
        # with the documented reason — never a fabricated class (US-F5).
        bat_path = row.metrics["bat_path"]
        assert bat_path["value"] is None
        assert bat_path["reason"] == "no detection provider configured"


def _assert_report_honest(ctx: WorkerContext, session_id: uuid_module.UUID) -> None:
    """The report leads with the US-L4 honesty banner and invents nothing (US-G3)."""
    with ctx.session_factory() as db:
        report = db.scalar(select(Report).where(Report.session_id == session_id))
    assert report is not None  # degraded still ships a report (US-J1)
    body = report.body
    banner = body["honesty_banner"]
    assert isinstance(banner, str)
    assert banner.startswith("Data quality was low this session")
    assert "weakest:" in banner  # the banner names WHY the numbers are shaky
    # Thin data ships no coached content and no numeric claim — nothing to
    # fabricate a correction from (US-G3 honesty path).
    assert body["main_correction"] is None
    assert body["drill"] is None
    assert body["goal"] is None
    assert body["secondary"] == []
    assert body["claims"] == []
    assert report.quality is not None
    assert report.quality["composite"] < HONESTY_BANNER_THRESHOLD
    assert report.quality["banner"] == banner  # the banner IS the quality banner


def test_golden_degraded_session_completes_honestly(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5 #3: dead camera + missing audio -> completes, honest, zero fabrication."""
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    monkeypatch.delenv("CRICAI_POSE_MODEL_ASSET", raising=False)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    ctx = WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )
    n_balls = len(generate_session(4242, (BlockSpec(n_balls=3),)).balls)
    session_id = _seed_degraded_session(ctx, tmp_path, n_balls)

    outcome = run_pipeline(ctx, session_id, policy=RunPolicy(max_attempts=1))
    fate = {stage.stage: stage for stage in outcome.stages}

    # Designed semantics (US-J1): the RUN succeeds — a missing expected camera
    # degrades the session, it does not fail the pipeline. Every stage ran.
    assert outcome.status == "succeeded"
    for stage in STAGES:
        assert fate[stage].status is StageStatus.SUCCEEDED, (stage, fate[stage].error)

    # ... and the honesty lives in the trace: probe marks the session degraded
    # and names the dead camera; events records the audio gap (US-J1/D1).
    with ctx.session_factory() as db:
        rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == outcome.run_id)).all()
    payloads = {
        row.stage: (row.output or {}).get("payload", {})
        for row in rows
        if row.status is StageStatus.SUCCEEDED
    }
    assert payloads["probe"]["degraded"] is True
    assert payloads["probe"]["missing_cameras"] == [DEAD_CAMERA]
    assert payloads["probe"]["cameras_with_evidence"] == [LIVE_CAMERA]
    assert payloads["events"]["audio_onsets"] is None  # C1 audio missing, honestly
    assert payloads["events"]["detected"] == n_balls  # exact count, from real footage
    assert payloads["clips"]["gaps"] == n_balls  # one loud gap per ball (US-D2)
    assert payloads["clips"]["cut"] == n_balls
    # The pose stage reports every (ball, C3) footage gap loudly (US-E1 AC).
    expected_pose_gaps = [[ball_no, DEAD_CAMERA] for ball_no in range(1, n_balls + 1)]
    assert payloads["pose"]["missing"] == expected_pose_gaps
    # Model gaps degrade to documented fallbacks WITH the reason recorded.
    assert payloads["pose"]["provider_fallback"] is not None
    assert payloads["detect_track"]["detector_fallback"] is not None
    assert payloads["metrics"]["detector"] is None
    assert payloads["metrics"]["fused_count"] == n_balls
    for ball in payloads["metrics"]["fused"]:
        assert ball["bat_path"] is None  # no detector -> no bat-path class
        assert "audio" not in ball["modalities"]  # missing audio never votes (US-F5)

    _assert_gap_rows_loud(ctx, session_id, n_balls)
    _assert_nothing_fabricated_for_dead_camera(ctx, session_id, n_balls)
    _assert_metrics_null_with_reason(ctx, session_id)
    _assert_report_honest(ctx, session_id)
