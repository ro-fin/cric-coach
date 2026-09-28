"""US-I4/US-I5 bowling-flight job: exact metric names, precedence, honesty."""

import ast
import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from cricai_coaching.content_lint import BANNED_SPIN_CLAIMS, find_banned_phrases
from cricai_data.ballrecord import BOWLING_FIELDS
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    Handedness,
    Length,
    Line,
    MetricPhase,
    SessionType,
)
from cricai_data.models import (
    BallEvent,
    BallMetrics,
    BallTrack,
    BounceEstimate,
    BounceMark,
    BowlingTarget,
    Player,
    Session,
    SessionBlock,
)
from cricai_data.storage import FsObjectStore
from cricai_vision.trajectory import TRAJECTORY_VERSION
from cricai_worker import bowling_flight
from cricai_worker.bowling_flight import (
    BOWLING_FLIGHT_KEYS,
    FlightInputs,
    FlightSummary,
    analyze_session_flight,
)
from cricai_worker.context import WorkerContext
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

UTC = datetime.UTC


def _context(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed_session(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(
            name="Mira", birthdate=datetime.date(2014, 3, 2), handedness=Handedness.RIGHT
        )
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 10),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
        )
        db.add_all([player, session])
        db.commit()
        return session.id


def _add_event(
    db: OrmSession, session_id: uuid.UUID, ball_no: int, *, start_ms: int = 0, valid: bool = True
) -> None:
    db.add(
        BallEvent(
            session_id=session_id,
            ball_no=ball_no,
            start_ms=start_ms,
            release_ms=start_ms + 100,
            end_ms=start_ms + 2000,
            confidence=0.9,
            valid=valid,
        )
    )


def _add_target(
    db: OrmSession,
    session_id: uuid.UUID,
    *,
    line: Line = Line.OFF,
    length: Length = Length.GOOD,
    block_id: uuid.UUID | None = None,
    created_at: datetime.datetime | None = None,
) -> None:
    target = BowlingTarget(
        session_id=session_id,
        block_id=block_id,
        line=line,
        length=length,
        description="leg-break to a good length on off stump",
        created_by="coach",
    )
    if created_at is not None:
        target.created_at = created_at
    db.add(target)


def _add_estimate(
    db: OrmSession,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    pitch_x: float = 6.0,
    pitch_y: float = 0.2,
    confidence: float = 0.9,
) -> None:
    db.add(
        BounceEstimate(
            session_id=session_id,
            ball_no=ball_no,
            pitch_x=pitch_x,
            pitch_y=pitch_y,
            confidence=confidence,
            tracker_version="test",
        )
    )


def _payload(*, pitch_mapped: bool = True, full_toss: bool = False) -> dict[str, Any]:
    """A synthetic bowling-end delivery track (bowler end x = 20.12 side).

    14 pre-bounce points: pitch_x 14.5 -> 8.0 (0.5 m steps), image arc
    px_y = 293 + 3*(i-2)^2 (apex at i=2, interior); 10 post-bounce points:
    pitch_x 7.5 -> 3.0, rebounding in the image, deviating +0.075 m of pitch_y
    per meter of travel (a leg-break to the right-hander: 15 cm at 2 m).
    """
    points: list[dict[str, Any]] = []
    for i in range(14):
        point: dict[str, Any] = {
            "frame_no": i,
            "ts_ms": 20.0 * i,
            "px_x": 100.0 + 10.0 * i,
            "px_y": 293.0 + 3.0 * (i - 2) ** 2,
            "score": 0.9,
            "bridged": False,
        }
        if pitch_mapped:
            point["pitch_x"] = 14.5 - 0.5 * i
            point["pitch_y"] = 0.3
        points.append(point)
    bounce_py = 293.0 + 3.0 * 11**2
    for j in range(10):
        x = 7.5 - 0.5 * j
        point = {
            "frame_no": 14 + j,
            "ts_ms": 20.0 * (14 + j),
            "px_x": 100.0 + 10.0 * (14 + j),
            "px_y": bounce_py - 40.0 * (j + 1),
            "score": 0.9,
            "bridged": False,
        }
        if pitch_mapped:
            point["pitch_x"] = x
            point["pitch_y"] = 0.3 + 0.075 * (8.0 - x)
        points.append(point)
    segments = (
        [{"kind": "pre_bounce", "start_ms": 0.0, "end_ms": 460.0, "confidence": 0.9}]
        if full_toss
        else [
            {"kind": "pre_bounce", "start_ms": 0.0, "end_ms": 260.0, "confidence": 0.9},
            {"kind": "post_bounce", "start_ms": 280.0, "end_ms": 460.0, "confidence": 0.8},
        ]
    )
    return {
        "points": points,
        "segments": segments,
        "flags": {"identity_risk": False, "long_gap": False},
    }


def _add_track(
    ctx: WorkerContext,
    db: OrmSession,
    session_id: uuid.UUID,
    ball_no: int,
    camera_id: str,
    payload: dict[str, Any] | None,
    *,
    raw: bytes | None = None,
    store: bool = True,
) -> None:
    key = f"sessions/{session_id}/balls/{ball_no}/track-{camera_id}.json"
    if store:
        body = raw if raw is not None else json.dumps(payload).encode()
        ctx.store.put(key, body)
    db.add(
        BallTrack(
            session_id=session_id,
            ball_no=ball_no,
            camera_id=camera_id,
            tracker_version="cv-kalman-1",
            points_key=key,
            coverage=1.0,
            segments=[],
            flags={},
            confidence=0.9,
        )
    )


def _metrics(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> dict[str, Any]:
    with ctx.session_factory() as db:
        row = db.execute(
            select(BallMetrics).where(
                BallMetrics.session_id == session_id,
                BallMetrics.ball_no == ball_no,
                BallMetrics.phase == MetricPhase.FLIGHT,
            )
        ).scalar_one()
        return dict(row.metrics)


def _scale_resolver(scale: float) -> Any:
    def resolver(_db: OrmSession, _session: Session, _camera_id: str) -> float | None:
        return scale

    return resolver


def test_writes_the_four_exact_metric_names_and_honest_values(tmp_path: Path) -> None:
    """US-I4/US-I5: exact keys, turn measured, apex null without a scale."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_target(db, session_id)
        _add_estimate(db, session_id, 1)
        _add_track(ctx, db, session_id, 1, "C5", _payload())
        db.commit()
    summary = analyze_session_flight(ctx, session_id)
    metrics = _metrics(ctx, session_id, 1)
    assert set(metrics) == set(BOWLING_FLIGHT_KEYS)
    assert metrics["turn_cm"]["value"] == pytest.approx(15.0, abs=1e-6)
    assert metrics["turn_cm"]["unit"] == "cm"
    assert metrics["turn_cm"]["source"] == f"C5+{TRAJECTORY_VERSION}"
    assert metrics["apex_m"]["value"] is None  # honest: no calibrated vertical scale
    assert "no calibrated vertical scale" in metrics["apex_m"]["reason"]
    assert metrics["dip_flag"]["value"] is False  # an arc-like descent, no extra drop
    assert metrics["target_hit"]["value"] is True
    assert metrics["target_hit"]["confidence"] == 0.9
    assert metrics["target_hit"]["source"] == f"auto+{TRAJECTORY_VERSION}"
    assert isinstance(summary, FlightSummary)
    ball = summary.balls[0]
    assert (ball.ball_no, ball.dip_flag, ball.target_hit) == (1, False, True)
    assert ball.turn_cm == pytest.approx(15.0, abs=1e-6)
    assert ball.apex_m is None


def test_calibrated_scale_measures_the_apex(tmp_path: Path) -> None:
    """US-I5: with a calibrated vertical scale the apex height appears."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_track(ctx, db, session_id, 1, "C5", _payload())
        db.commit()
    inputs = FlightInputs(scale_resolver=_scale_resolver(0.001))
    analyze_session_flight(ctx, session_id, inputs=inputs)
    metrics = _metrics(ctx, session_id, 1)
    # Apex pixel 293 (i=2), bounce pixel 656: 363 px at 0.001 m/px.
    assert metrics["apex_m"]["value"] == pytest.approx(0.363)
    assert metrics["apex_m"]["unit"] == "m"
    assert metrics["apex_m"]["source"] == f"C5+{TRAJECTORY_VERSION}"


def test_metric_names_match_the_ballrecord_contract() -> None:
    """The v1.1 assembler reads these exact names (contract drift guard)."""
    assert set(BOWLING_FLIGHT_KEYS) <= set(BOWLING_FIELDS)


def test_merge_preserves_other_flight_keys_and_reruns_identically(tmp_path: Path) -> None:
    """The US-E4 batting flight keys survive; a re-run rewrites equal values."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_target(db, session_id)
        _add_estimate(db, session_id, 1)
        _add_track(ctx, db, session_id, 1, "C5", _payload())
        db.add(
            BallMetrics(
                session_id=session_id,
                ball_no=1,
                phase=MetricPhase.FLIGHT,
                metrics={"speed_kph": {"value": 78.0, "unit": "kph", "confidence": 1.0}},
            )
        )
        db.commit()
    analyze_session_flight(ctx, session_id)
    first = _metrics(ctx, session_id, 1)
    assert first["speed_kph"]["value"] == 78.0  # merged, never replaced
    assert set(first) == {"speed_kph", *BOWLING_FLIGHT_KEYS}
    analyze_session_flight(ctx, session_id)
    assert _metrics(ctx, session_id, 1) == first  # idempotent


def test_manual_bounce_mark_beats_the_auto_estimate(tmp_path: Path) -> None:
    """Manual beats machine (pinned US-F4 precedence), confidence 1.0."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_target(db, session_id)
        _add_estimate(db, session_id, 1, pitch_x=3.0)  # the auto row says FULL: a miss
        db.add(
            BounceMark(
                session_id=session_id,
                ball_no=1,
                camera_id="C5",
                frame_no=14,
                px_x=240.0,
                px_y=656.0,
                pitch_x=6.0,
                pitch_y=0.2,
            )
        )
        db.commit()
    analyze_session_flight(ctx, session_id)
    metrics = _metrics(ctx, session_id, 1)
    assert metrics["target_hit"]["value"] is True  # the manual mark wins
    assert metrics["target_hit"]["confidence"] == 1.0
    assert metrics["target_hit"]["source"] == f"manual+{TRAJECTORY_VERSION}"


def test_block_target_beats_session_wide_and_newest_wins(tmp_path: Path) -> None:
    """US-I4 target resolution: block scope first, newest declaration wins."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    early = datetime.datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
    later = datetime.datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    with ctx.session_factory() as db:
        block = SessionBlock(
            session_id=session_id,
            block_no=1,
            start_s=0.0,
            end_s=60.0,
            bowler_source=BowlerSource.HUMAN,
            intent=BlockIntent.SPIN_SPECIFIC,
        )
        open_block = SessionBlock(
            session_id=session_id,
            block_no=2,
            start_s=200.0,
            end_s=None,
            bowler_source=BowlerSource.HUMAN,
            intent=BlockIntent.SPIN_SPECIFIC,
        )
        db.add_all([block, open_block])
        db.flush()
        # Session-wide: an old MIDDLE/FULL target superseded by OFF/GOOD.
        _add_target(db, session_id, line=Line.MIDDLE, length=Length.FULL, created_at=early)
        _add_target(db, session_id, line=Line.OFF, length=Length.GOOD, created_at=later)
        # Block 1 declares LEG/SHORT: ball 1 (in the block) misses at (6.0, 0.2).
        _add_target(
            db, session_id, line=Line.LEG, length=Length.SHORT, block_id=block.id, created_at=early
        )
        _add_event(db, session_id, 1, start_ms=30_000)  # inside block 1
        _add_event(db, session_id, 2, start_ms=90_000)  # between blocks: session-wide
        _add_event(db, session_id, 3, start_ms=250_000)  # open block, no scoped target
        for ball_no in (1, 2, 3):
            _add_estimate(db, session_id, ball_no)  # all bounce at (6.0, 0.2): OFF/GOOD
        db.commit()
    analyze_session_flight(ctx, session_id)
    assert _metrics(ctx, session_id, 1)["target_hit"]["value"] is False  # scored vs LEG/SHORT
    assert _metrics(ctx, session_id, 2)["target_hit"]["value"] is True  # newest session-wide
    assert _metrics(ctx, session_id, 3)["target_hit"]["value"] is True  # falls back session-wide


def test_missing_inputs_are_null_with_reasons_never_fabricated(tmp_path: Path) -> None:
    """No track / no bounce / no target: nulls carry the honest reason."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)  # nothing else exists for ball 1
        _add_event(db, session_id, 2)
        _add_target(db, session_id)  # ball 2 has a target but no bounce
        db.commit()
    summary = analyze_session_flight(ctx, session_id)
    ball1 = _metrics(ctx, session_id, 1)
    assert ball1["turn_cm"]["value"] is None
    assert ball1["turn_cm"]["reason"] == "no ball track for this delivery"
    assert ball1["apex_m"]["reason"] == "no ball track for this delivery"
    assert ball1["dip_flag"]["reason"] == "no ball track for this delivery"
    ball2 = _metrics(ctx, session_id, 2)
    assert ball2["target_hit"]["value"] is None
    assert "no bounce estimate" in ball2["target_hit"]["reason"]
    # Ball 1 additionally has no declared target (created after its... no —
    # targets are session-wide here, so ball 1 scores against it too).
    assert "no bounce estimate" in ball1["target_hit"]["reason"]
    assert all(ball.target_hit is None for ball in summary.balls)


def test_no_declared_target_scores_null(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_estimate(db, session_id, 1)
        db.commit()
    analyze_session_flight(ctx, session_id)
    metrics = _metrics(ctx, session_id, 1)
    assert metrics["target_hit"]["value"] is None
    assert "no declared target" in metrics["target_hit"]["reason"]


def test_off_pitch_bounce_is_unscored_with_reason(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_target(db, session_id)
        _add_estimate(db, session_id, 1, pitch_x=-1.0)
        db.commit()
    analyze_session_flight(ctx, session_id)
    metrics = _metrics(ctx, session_id, 1)
    assert metrics["target_hit"]["value"] is None
    assert "outside the classifiable pitch area" in metrics["target_hit"]["reason"]


def test_pixel_only_camera_degrades_to_the_mapped_one(tmp_path: Path) -> None:
    """Per-metric fallback: C5 is pixel-only (null turn), C6 measures it."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_track(ctx, db, session_id, 1, "C5", _payload(pitch_mapped=False))
        _add_track(ctx, db, session_id, 1, "C6", _payload())
        db.commit()
    analyze_session_flight(ctx, session_id)
    metrics = _metrics(ctx, session_id, 1)
    assert metrics["turn_cm"]["value"] == pytest.approx(15.0, abs=1e-6)
    assert metrics["turn_cm"]["source"] == f"C6+{TRAJECTORY_VERSION}"
    # dip is scale-free: the FIRST camera already measured it and is kept.
    assert metrics["dip_flag"]["value"] is False
    assert metrics["dip_flag"]["source"] == f"C5+{TRAJECTORY_VERSION}"


def test_first_camera_wins_when_both_measure(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_track(ctx, db, session_id, 1, "C5", _payload())
        _add_track(ctx, db, session_id, 1, "C6", _payload())
        db.commit()
    analyze_session_flight(ctx, session_id)
    assert _metrics(ctx, session_id, 1)["turn_cm"]["source"] == f"C5+{TRAJECTORY_VERSION}"


def test_unreadable_payloads_degrade_per_camera(tmp_path: Path) -> None:
    """Missing object, malformed JSON, malformed pitch data: honest reasons."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    bad_pitch = _payload()
    bad_pitch["points"][0]["pitch_x"] = "sideways"
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_track(ctx, db, session_id, 1, "C5", None, store=False)  # no stored object
        _add_event(db, session_id, 2)
        _add_track(ctx, db, session_id, 2, "C5", None, raw=b"{not json")
        _add_event(db, session_id, 3)
        _add_track(ctx, db, session_id, 3, "C5", bad_pitch)
        db.commit()
    analyze_session_flight(ctx, session_id)
    assert (
        _metrics(ctx, session_id, 1)["turn_cm"]["reason"]
        == "C5: track payload missing from the object store"
    )
    assert "C5: malformed track payload" in _metrics(ctx, session_id, 2)["turn_cm"]["reason"]
    assert "C5: malformed track payload" in _metrics(ctx, session_id, 3)["turn_cm"]["reason"]


def test_full_toss_reports_the_estimator_reason(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_track(ctx, db, session_id, 1, "C5", _payload(full_toss=True))
        db.commit()
    analyze_session_flight(ctx, session_id)
    reason = _metrics(ctx, session_id, 1)["turn_cm"]["reason"]
    assert reason.startswith("C5: ")
    assert "full toss or tracking failure" in reason


def test_rejected_events_are_not_balls(tmp_path: Path) -> None:
    """US-D4: an invalidated event is never analyzed or written."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1, valid=False)
        _add_estimate(db, session_id, 1)
        db.commit()
    summary = analyze_session_flight(ctx, session_id)
    assert summary.balls == ()
    with ctx.session_factory() as db:
        assert (
            db.execute(
                select(BallMetrics).where(BallMetrics.session_id == session_id)
            ).scalar_one_or_none()
            is None
        )


def test_unknown_session_is_loud(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="session not found"):
        analyze_session_flight(ctx, uuid.uuid4())


def test_left_hand_target_frame_mirrors_the_lateral_axis(tmp_path: Path) -> None:
    """US-I4: targets declared to a left-hander mirror the line channels."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        _add_event(db, session_id, 1)
        _add_target(db, session_id)
        _add_estimate(db, session_id, 1, pitch_y=-0.2)  # off side for a left-hander
        db.commit()
    analyze_session_flight(ctx, session_id, inputs=FlightInputs(batter_handedness=Handedness.LEFT))
    assert _metrics(ctx, session_id, 1)["target_hit"]["value"] is True


@pytest.mark.safety
def test_no_banned_claims_in_any_module_string() -> None:
    """SAF: bowling-facing strings stay geometric (no made-up ball-physics).

    Uses the canonical :data:`BANNED_SPIN_CLAIMS` vocabulary via
    :func:`find_banned_phrases` (finding [34]) so this scan can never drift
    narrower than the one the report/rules surfaces enforce — an inline regex
    here once missed ``spin axis``.
    """
    source = Path(bowling_flight.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            violations = find_banned_phrases(node.value, BANNED_SPIN_CLAIMS)
            assert not violations, f"banned claim {violations[0].phrase!r} at line {node.lineno}"


# --------------------------------------------------- concurrent-merge safety


def test_lost_first_insert_race_merges_onto_the_winners_row(tmp_path: Path) -> None:
    """uq_metrics_ball_phase first-insert race (Phase-6 finding 16).

    Two writers can both see no ``(session, ball, flight)`` row and both
    insert; the loser's flush hits the unique constraint. The merge must then
    adopt the winner's committed row and merge onto it — never abort the run
    and never clobber the winner's keys. Simulated deterministically: a
    ``before_flush`` hook commits the competing row through a second engine
    right before the merge's own INSERT flushes.
    """
    path = tmp_path / "race.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    create_all(engine)
    factory = make_session_factory(engine)
    other_factory = make_session_factory(create_engine(f"sqlite:///{path}"))
    with factory() as db:
        player = Player(name="Mira", birthdate=datetime.date(2014, 3, 2))
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 10),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
        )
        db.add_all([player, session])
        db.commit()
        session_id = session.id

    competing = {"speed_kph": {"value": 78.0, "unit": "kph", "confidence": 1.0}}

    def _competing_first_writer(*_args: Any) -> None:
        with other_factory() as other:
            other.add(
                BallMetrics(
                    session_id=session_id,
                    ball_no=1,
                    phase=MetricPhase.FLIGHT,
                    metrics=competing,
                )
            )
            other.commit()

    entries = {"turn_cm": {"value": 12.0, "unit": "cm", "confidence": 0.9}}
    with factory() as db:
        event.listen(db, "before_flush", _competing_first_writer, once=True)
        bowling_flight._merge_flight_metrics(db, session_id, 1, entries)
        db.commit()

    with factory() as db:
        row = db.execute(
            select(BallMetrics).where(
                BallMetrics.session_id == session_id,
                BallMetrics.ball_no == 1,
                BallMetrics.phase == MetricPhase.FLIGHT,
            )
        ).scalar_one()  # exactly one row: the race never duplicates
        assert row.metrics["speed_kph"] == competing["speed_kph"]  # winner's key survived
        assert row.metrics["turn_cm"]["value"] == 12.0  # the loser merged on top
