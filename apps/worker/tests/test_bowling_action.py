"""US-I2/I3 bowling-action job: pinned metric names, merge-by-key, honest nulls (unit, SQLite)."""

import math
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cricai_data.ballrecord import session_ball_records
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import BowlerSource, MetricPhase, SessionType
from cricai_data.models import BallEvent, BallMetrics, Player, Session
from cricai_data.storage import FsObjectStore
from cricai_vision.pose import (
    LANDMARK_NAMES,
    N_LANDMARKS,
    FakePoseProvider,
    Landmark,
    PoseFrame,
    PoseTrack,
)
from cricai_vision.release import REASON_ARM_NEVER_OVERHEAD
from cricai_vision.triangulate import Track3D, Track3DPoint, TrackQuality3D
from cricai_worker import bowling_action
from cricai_worker.bowling_action import (
    BOWLING_ACTION_KEYS,
    SKIP_NO_FOOTAGE,
    BowlingActionInputs,
    analyze_session_bowling_action,
)
from cricai_worker.context import WorkerContext
from sqlalchemy import event, select

INDEX_TO_NAME: dict[int, str] = {index: name for name, index in LANDMARK_NAMES.items()}

FPS = 120.0
N_FRAMES = 48

#: Right-arm bowler at release: upright trunk, braced vertical front-left leg,
#: feet planted at image y=1000, head 25 px toward the target (+x).
BOWLER_POSE: dict[str, tuple[float, float]] = {
    "nose": (905.0, 300.0),
    "left_shoulder": (910.0, 400.0),
    "right_shoulder": (890.0, 400.0),
    "left_hip": (880.0, 700.0),
    "right_hip": (920.0, 700.0),
    "left_knee": (880.0, 850.0),
    "right_knee": (930.0, 860.0),
    "left_ankle": (880.0, 1000.0),
    "right_ankle": (950.0, 990.0),
}

#: The swing's analytic apex frame: phi = -160 + i * 6.8 crosses 0 at 23.53.
APEX_FRAME = 24


def make_frame(frame_no: int, points: dict[str, tuple[float, float]]) -> PoseFrame:
    landmarks = []
    for index in range(N_LANDMARKS):
        name = INDEX_TO_NAME.get(index)
        xy = points.get(name, (500.0, 500.0)) if name is not None else (500.0, 500.0)
        landmarks.append(Landmark(image_xy=xy, world_xyz=(0.0, 0.0, 0.0), visibility=0.95))
    return PoseFrame(frame_no=frame_no, landmarks=tuple(landmarks))


class SwingPoseProvider:
    """Deterministic bowler swing (the FakePoseProvider pattern, US-I2):
    the right wrist arcs over the shoulder with its apex at frame 24."""

    model_name = "fake-bowler-pose"
    model_version = "1"

    def extract(self, frames: Any, *, fps: float) -> PoseTrack:
        pose_frames = []
        for i in range(len(frames)):
            phi = math.radians(-160.0 + i * 6.8)
            wrist = (900.0 + 60.0 * math.sin(phi), 400.0 - 60.0 * math.cos(phi))
            pose_frames.append(make_frame(i, {**BOWLER_POSE, "right_wrist": wrist}))
        return PoseTrack(
            model_name=self.model_name,
            model_version=self.model_version,
            fps=fps,
            frames=tuple(pose_frames),
            subject_confidence=0.97,
        )


def frames_resolver(
    session_id: uuid.UUID, ball_no: int, camera_id: str
) -> tuple[list[int], float] | None:
    return (list(range(N_FRAMES)), FPS)


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = make_engine(f"sqlite:///{tmp_path / 'bowl.sqlite'}")
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def seed_session(
    ctx: WorkerContext, *, session_type: SessionType = SessionType.BOWLING
) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=date(2026, 7, 10),
            session_type=session_type,
            bowler_source=BowlerSource.HUMAN,
        )
        db.add(session)
        db.commit()
        return session.id


def add_event(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    release_ms: int = 1200,
    valid: bool = True,
) -> None:
    with ctx.session_factory() as db:
        db.add(
            BallEvent(
                session_id=session_id,
                ball_no=ball_no,
                start_ms=1000,
                release_ms=release_ms,
                contact_ms=None,
                end_ms=1400,
                confidence=0.8,
                valid=valid,
            )
        )
        db.commit()


def metrics_row(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> BallMetrics:
    with ctx.session_factory() as db:
        return db.execute(
            select(BallMetrics).where(
                BallMetrics.session_id == session_id,
                BallMetrics.ball_no == ball_no,
                BallMetrics.phase == MetricPhase.PRE_RELEASE,
            )
        ).scalar_one()


def run(
    ctx: WorkerContext, session_id: uuid.UUID, inputs: BowlingActionInputs | None = None
) -> Any:
    return analyze_session_bowling_action(
        ctx,
        session_id,
        provider=SwingPoseProvider(),
        frames_resolver=frames_resolver,
        inputs=inputs,
    )


def make_track3d(z_m: float, center_ms: float) -> Track3D:
    points = tuple(
        Track3DPoint(
            frame_no=i, ts_ms=center_ms + (i - 5) * 8.0, x=0.0, y=0.0, z=z_m, reprojection_px=0.0
        )
        for i in range(11)
    )
    quality = TrackQuality3D(
        rms_reprojection_px=0.0,
        matched_fraction=1.0,
        n_frames_union=11,
        n_points_3d=11,
        n_dropped_unmatched=0,
        n_dropped_skewed=0,
        n_dropped_bridged=0,
        n_dropped_invalid=0,
        low_overlap=False,
        segments=(),
    )
    return Track3D(points=points, quality=quality)


def test_persists_exactly_the_pinned_metric_names(ctx: WorkerContext) -> None:
    """The BallRecord v1.1 assembler names + contract #6 primitives, verbatim."""
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    summary = run(ctx, session_id, BowlingActionInputs(px_per_cm=10.0))
    metrics = metrics_row(ctx, session_id, 1).metrics
    assert set(metrics) == set(BOWLING_ACTION_KEYS)
    assert metrics["release_frame"]["value"] == APEX_FRAME
    assert metrics["release_ms"]["value"] == pytest.approx(1000.0 + APEX_FRAME * 1000.0 / FPS)
    assert metrics["release_frame_offset"]["value"] == 0
    assert isinstance(metrics["release_frame_offset"]["value"], int)
    hand_xy = metrics["hand_xy"]["value"]
    assert isinstance(hand_xy, list) and len(hand_xy) == 2
    assert metrics["release_height_cm"]["value"] == pytest.approx((1000.0 - hand_xy[1]) / 10.0)
    assert metrics["release_height_cm"]["source"] == "pose_scale"
    assert metrics["brace_state"]["value"] == "braced"
    assert metrics["falling_away_deg"]["value"] == pytest.approx(0.0)
    assert metrics["head_offset_at_release_px"]["value"] == pytest.approx(25.0)
    assert metrics["head_offset_at_release_cm"]["value"] == pytest.approx(2.5)
    ball = summary.analyzed[0]
    assert (ball.ball_no, ball.release_frame, ball.release_frame_offset) == (1, APEX_FRAME, 0)
    assert ball.brace_state == "braced"
    assert ball.detection_reason is None
    assert summary.analyzed_count == 1


def test_release_frame_offset_scores_the_drift_from_the_event(ctx: WorkerContext) -> None:
    """US-I3: offset = detected frame - nominal frame from the event timestamps."""
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, release_ms=1175)  # nominal frame 21, detected 24
    run(ctx, session_id)
    metrics = metrics_row(ctx, session_id, 1).metrics
    assert metrics["release_frame_offset"]["value"] == 3


def test_merge_preserves_foreign_pre_release_keys(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    foreign = {"value": 30.0, "unit": "cm", "confidence": 0.9}
    with ctx.session_factory() as db:
        db.add(
            BallMetrics(
                session_id=session_id,
                ball_no=1,
                phase=MetricPhase.PRE_RELEASE,
                metrics={"stance_width_cm": foreign},
            )
        )
        db.commit()
    run(ctx, session_id)
    metrics = metrics_row(ctx, session_id, 1).metrics
    assert metrics["stance_width_cm"] == foreign
    assert set(metrics) == {"stance_width_cm", *BOWLING_ACTION_KEYS}


def test_rerun_is_idempotent(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    run(ctx, session_id, BowlingActionInputs(px_per_cm=10.0))
    first = dict(metrics_row(ctx, session_id, 1).metrics)
    run(ctx, session_id, BowlingActionInputs(px_per_cm=10.0))
    assert metrics_row(ctx, session_id, 1).metrics == first


def test_undetected_release_writes_every_key_null_with_reason(ctx: WorkerContext) -> None:
    """A batting-stance track has no overhead swing: all keys null, loudly."""
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    summary = analyze_session_bowling_action(
        ctx, session_id, provider=FakePoseProvider(), frames_resolver=frames_resolver
    )
    metrics = metrics_row(ctx, session_id, 1).metrics
    assert set(metrics) == set(BOWLING_ACTION_KEYS)
    for payload in metrics.values():
        assert payload["value"] is None
        assert payload["reason"] == REASON_ARM_NEVER_OVERHEAD
        assert payload["confidence"] == 0.0
    ball = summary.analyzed[0]
    assert ball.release_frame is None
    assert ball.detection_reason == REASON_ARM_NEVER_OVERHEAD


def test_balls_without_footage_are_skipped_loudly(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    add_event(ctx, session_id, 2)

    def gappy(session_id: uuid.UUID, ball_no: int, camera_id: str) -> Any:
        return None if ball_no == 2 else frames_resolver(session_id, ball_no, camera_id)

    summary = analyze_session_bowling_action(
        ctx, session_id, provider=SwingPoseProvider(), frames_resolver=gappy
    )
    assert summary.skipped == ((2, SKIP_NO_FOOTAGE),)
    assert [ball.ball_no for ball in summary.analyzed] == [1]


def test_invalid_events_are_ignored(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, valid=False)
    summary = run(ctx, session_id)
    assert summary.analyzed == () and summary.skipped == ()


def test_stereo_seam_wins_for_release_height(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    inputs = BowlingActionInputs(
        px_per_cm=10.0, track3d_resolver=lambda _sid, _ball_no: make_track3d(2.05, 1200.0)
    )
    run(ctx, session_id, inputs)
    payload = metrics_row(ctx, session_id, 1).metrics["release_height_cm"]
    assert payload["value"] == pytest.approx(205.0)
    assert payload["source"] == "stereo"


def test_ball_record_assembler_reads_the_persisted_names(ctx: WorkerContext) -> None:
    """End-to-end contract: BallRecord v1.1 bowling fields fill from this job."""
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    run(ctx, session_id, BowlingActionInputs(px_per_cm=10.0))
    with ctx.session_factory() as db:
        (record,) = session_ball_records(db, session_id)
    assert record["mode"] == "bowling"
    assert record["brace_state"] == "braced"
    assert record["release_frame_offset"] == 0
    assert record["falling_away_deg"] == pytest.approx(0.0)
    assert record["release_height_cm"] == pytest.approx(65.99, abs=0.05)
    assert record["source"]["release_height_cm"] == "pose_scale"
    assert record["source"]["brace_state"] == "auto"
    assert record["variation_intent"] is None  # no delivery label: null-with-reason
    assert "variation_intent" in record["reasons"]


def test_non_bowling_sessions_are_rejected(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx, session_type=SessionType.BATTING)
    with pytest.raises(ValueError, match="not bowling"):
        run(ctx, session_id)


def test_unknown_session_is_loud(ctx: WorkerContext) -> None:
    with pytest.raises(ValueError, match="session not found"):
        run(ctx, uuid.uuid4())


def test_inputs_validation_is_loud() -> None:
    with pytest.raises(ValueError, match="px_per_cm"):
        BowlingActionInputs(px_per_cm=0.0)


# --------------------------------------------------- concurrent-merge safety


def test_lost_first_insert_race_merges_onto_the_winners_row(tmp_path: Path) -> None:
    """uq_metrics_ball_phase first-insert race on the pre-release row (the
    Phase-6 finding-16 pattern, mirrored from bowling_flight): the loser of a
    concurrent first insert must adopt the winner's committed row and merge
    onto it — never abort the run, never clobber the winner's keys."""
    path = tmp_path / "race.sqlite"
    engine = make_engine(f"sqlite:///{path}")
    create_all(engine)
    factory = make_session_factory(engine)
    other_factory = make_session_factory(make_engine(f"sqlite:///{path}"))
    with factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        session = Session(
            player=player,
            session_date=date(2026, 7, 10),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
        )
        db.add_all([player, session])
        db.commit()
        session_id = session.id

    competing = {"stance_width_cm": {"value": 30.0, "unit": "cm", "confidence": 0.9}}

    def _competing_first_writer(*_args: Any) -> None:
        with other_factory() as other:
            other.add(
                BallMetrics(
                    session_id=session_id,
                    ball_no=1,
                    phase=MetricPhase.PRE_RELEASE,
                    metrics=competing,
                )
            )
            other.commit()

    entries = {"release_frame": {"value": 24, "unit": "frame", "confidence": 0.9}}
    with factory() as db:
        event.listen(db, "before_flush", _competing_first_writer, once=True)
        bowling_action._merge_pre_release_metrics(db, session_id, 1, entries)
        db.commit()

    with factory() as db:
        row = db.execute(
            select(BallMetrics).where(
                BallMetrics.session_id == session_id,
                BallMetrics.ball_no == 1,
                BallMetrics.phase == MetricPhase.PRE_RELEASE,
            )
        ).scalar_one()  # exactly one row: the race never duplicates
        assert row.metrics["stance_width_cm"] == competing["stance_width_cm"]
        assert row.metrics["release_frame"]["value"] == 24
