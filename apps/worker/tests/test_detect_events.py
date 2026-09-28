"""US-D1 worker job + CLI tests: EVENT REPLACE RULE semantics against the real schema.

Unit tests run on in-memory SQLite (full line+branch coverage of the job);
integration tests re-prove the delete-then-reinsert flow and the CLI against
temp-Postgres, where the (session_id, ball_no) unique constraint is enforced.
The DEPENDENT-ROW GUARD tests prove a re-run can never delete an auto event
whose ball number downstream rows still point at (US-D4 relink guarantee);
they parametrize over the canonical ``EVENT_DEPENDENT_TABLES`` labels so a
table added to the list without a seeder here fails loudly (KeyError).
"""

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from cricai_data.db import create_all, make_engine, make_session_factory, session_scope
from cricai_data.enums import (
    BowlerSource,
    BowlingVariation,
    Contact,
    EventSource,
    Footwork,
    Length,
    Line,
    MetricPhase,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import (
    EVENT_DEPENDENT_TABLES,
    BallEvent,
    BallMetrics,
    BallTag,
    BallTrack,
    BounceEstimate,
    BounceMark,
    Clip,
    CoachNote,
    DeliveryLabel,
    EventCorrection,
    FrameSample,
    Player,
    PoseTrack,
    ReferenceBall,
    Session,
)
from cricai_data.storage import FsObjectStore
from cricai_vision.events import DETECTOR_VERSION, EventDetectionError
from cricai_worker.context import ENV_DATABASE_URL, ENV_STORAGE_ROOT, WorkerContext
from cricai_worker.detect_events import DetectionBlockedError, run_detection
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "detect_events.py"

FPS = 100.0  # 1 frame == 10 ms

#: 25 frames (250 ms) above the DEFAULT config threshold, peak at offset 10.
BUMP = [0.6] * 10 + [1.0] + [0.6] * 14


def _energy(starts: list[int], n_frames: int) -> list[float]:
    energy = [0.0] * n_frames
    for start in starts:
        energy[start : start + len(BUMP)] = BUMP
    return energy


#: Three deliveries 4 s apart -> windows 1000-1250, 5000-5250, 9000-9250 ms.
THREE_BALLS = _energy([100, 500, 900], 1300)


def _make_ctx(engine: Engine, tmp_path: Path) -> WorkerContext:
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return _make_ctx(engine, tmp_path)


def _seed_session(ctx: WorkerContext) -> uuid.UUID:
    with session_scope(ctx.session_factory) as db:
        session = Session(
            player=Player(name="Test Player", birthdate=date(2012, 1, 1)),
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        return session.id


def _seed_event(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    source: EventSource,
    valid: bool,
) -> uuid.UUID:
    with session_scope(ctx.session_factory) as db:
        event = BallEvent(
            session_id=session_id,
            ball_no=ball_no,
            start_ms=ball_no * 10_000,
            release_ms=ball_no * 10_000 + 100,
            contact_ms=None,
            end_ms=ball_no * 10_000 + 500,
            confidence=0.5,
            source=source,
            detector_version="stale-detector",
            valid=valid,
        )
        db.add(event)
        db.flush()
        return event.id


def _events(ctx: WorkerContext, session_id: uuid.UUID) -> list[BallEvent]:
    with ctx.session_factory() as db:
        return list(
            db.scalars(
                select(BallEvent)
                .where(BallEvent.session_id == session_id)
                .order_by(BallEvent.ball_no)
            )
        )


# ------------------------------------------- dependent-row seeders (guard tests)


def _seed_tag(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        BallTag(
            session_id=session_id,
            ball_no=ball_no,
            line=Line.OFF,
            length=Length.GOOD,
            shot=Shot.DRIVE,
            footwork=Footwork.FRONT,
            contact=Contact.MIDDLE,
            outcome=Outcome.CONTROLLED_GROUND_SHOT,
            control=True,
            created_by="parent",
        )
    )


def _seed_bounce_mark(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        BounceMark(
            session_id=session_id,
            ball_no=ball_no,
            camera_id="C3",
            frame_no=42,
            px_x=600.0,
            px_y=172.5,
            pitch_x=6.0,
            pitch_y=0.2,
        )
    )


def _seed_clip(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(Clip(session_id=session_id, ball_no=ball_no, camera_id="C1", start_ms=0, end_ms=3000))


def _seed_pose_track(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        PoseTrack(
            session_id=session_id,
            ball_no=ball_no,
            camera_id="C1",
            model_name="fake-pose",
            model_version="1",
            landmarks_key=f"sessions/{session_id}/balls/{ball_no}/pose-C1.json",
            frame_count=120,
            availability=0.99,
            subject_confidence=0.97,
        )
    )


def _seed_ball_metrics(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        BallMetrics(
            session_id=session_id,
            ball_no=ball_no,
            phase=MetricPhase.PRE_RELEASE,
            metrics={},
        )
    )


def _seed_reference_ball(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        ReferenceBall(
            session_id=session_id,
            ball_no=ball_no,
            label="model cover drive",
            marked_by="coach",
        )
    )


def _seed_ball_track(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        BallTrack(
            session_id=session_id,
            ball_no=ball_no,
            camera_id="C1",
            tracker_version="kalman-1.0.0",
            points_key=f"sessions/{session_id}/balls/{ball_no}/track-C1.json",
            coverage=0.95,
            confidence=0.9,
        )
    )


def _seed_bounce_estimate(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        BounceEstimate(
            session_id=session_id,
            ball_no=ball_no,
            pitch_x=6.0,
            pitch_y=0.2,
            confidence=0.8,
            tracker_version="kalman-1.0.0",
        )
    )


def _seed_frame_sample(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        FrameSample(
            session_id=session_id,
            ball_no=ball_no,
            camera_id="C1",
            frame_no=ball_no * 10,
            ts_ms=ball_no * 1000,
            object_key=f"sessions/{session_id}/balls/{ball_no}/frame-C1.jpg",
            stratum={},
            sampler_version="frame-sampler-1.0.0",
        )
    )


def _seed_delivery_label(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        DeliveryLabel(
            session_id=session_id,
            ball_no=ball_no,
            variation_intent=BowlingVariation.LEG_BREAK,
            labeler="coach",
        )
    )


def _seed_coach_note(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    player_id = db.scalar(select(Session.player_id).where(Session.id == session_id))
    db.add(
        CoachNote(
            player_id=player_id,
            session_id=session_id,
            ball_no=ball_no,
            body="watch the seam position on this one",
            author="coach",
        )
    )


def _seed_correction(db: OrmSession, event_id: uuid.UUID) -> None:
    db.add(
        EventCorrection(event_id=event_id, action="adjust", before=None, after=None, actor="coach")
    )


#: Keyed by the canonical ``EVENT_DEPENDENT_TABLES`` labels. The guard test
#: parametrizes over the canonical list itself, so a table added there without
#: a seeder here fails loudly (KeyError) instead of silently going untested.
DEPENDENT_SEEDERS: dict[str, Callable[[OrmSession, uuid.UUID, int], None]] = {
    "ball tags": _seed_tag,
    "bounce marks": _seed_bounce_mark,
    "clips": _seed_clip,
    "pose tracks": _seed_pose_track,
    "ball metrics": _seed_ball_metrics,
    "reference balls": _seed_reference_ball,
    "ball tracks": _seed_ball_track,
    "bounce estimates": _seed_bounce_estimate,
    "frame samples": _seed_frame_sample,
    "delivery labels": _seed_delivery_label,
    "coach notes": _seed_coach_note,
}


def test_writes_auto_events(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    summary = run_detection(ctx, session_id, THREE_BALLS, FPS)
    assert summary.session_id == session_id
    assert summary.detected == 3
    assert summary.replaced == 0
    assert summary.preserved == 0
    assert summary.first_ball_no == 1
    assert summary.detector_version == DETECTOR_VERSION
    rows = _events(ctx, session_id)
    assert [row.ball_no for row in rows] == [1, 2, 3]
    assert (rows[0].start_ms, rows[0].release_ms, rows[0].end_ms) == (1000, 1100, 1250)
    for row in rows:
        assert row.source == EventSource.AUTO
        assert row.valid is True
        assert row.detector_version == DETECTOR_VERSION
        assert row.contact_ms is None  # no audio: left balls still exist
        assert 0.0 <= row.confidence <= 1.0


def test_contact_written_from_audio_onsets(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    run_detection(ctx, session_id, THREE_BALLS, FPS, audio_onsets_ms=[1150, 999_999])
    rows = _events(ctx, session_id)
    assert rows[0].contact_ms == 1150
    assert rows[1].contact_ms is None
    assert rows[2].contact_ms is None


def test_rerun_replaces_not_duplicates(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    run_detection(ctx, session_id, THREE_BALLS, FPS)
    first_ids = {row.id for row in _events(ctx, session_id)}
    summary = run_detection(ctx, session_id, THREE_BALLS, FPS)
    rows = _events(ctx, session_id)
    assert summary.replaced == 3
    assert summary.preserved == 0
    assert summary.first_ball_no == 1
    assert [row.ball_no for row in rows] == [1, 2, 3]  # same numbering, no duplicates
    assert {row.id for row in rows}.isdisjoint(first_ids)  # rows were replaced


def test_preserves_human_and_rejected_rows_numbering_above_them(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    stale_auto = _seed_event(ctx, session_id, 1, EventSource.AUTO, valid=True)
    rejected = _seed_event(ctx, session_id, 2, EventSource.AUTO, valid=False)
    corrected = _seed_event(ctx, session_id, 3, EventSource.CORRECTED, valid=True)
    manual = _seed_event(ctx, session_id, 7, EventSource.MANUAL, valid=True)
    summary = run_detection(ctx, session_id, THREE_BALLS, FPS)
    rows = _events(ctx, session_id)
    ids = {row.id for row in rows}
    assert stale_auto not in ids  # the only replaceable row is gone
    assert {rejected, corrected, manual} <= ids  # never touched or resurrected
    assert summary.replaced == 1
    assert summary.preserved == 3
    assert summary.first_ball_no == 8  # above max preserved ball_no (7)
    assert [row.ball_no for row in rows] == [2, 3, 7, 8, 9, 10]
    rejected_row = next(row for row in rows if row.id == rejected)
    assert rejected_row.valid is False  # still rejected
    new_rows = [row for row in rows if row.ball_no >= 8]
    assert all(row.source == EventSource.AUTO and row.valid for row in new_rows)


def test_quiet_stream_still_replaces_stale_auto_rows(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_event(ctx, session_id, 1, EventSource.AUTO, valid=True)
    summary = run_detection(ctx, session_id, [0.0] * 100, FPS)
    assert summary.detected == 0
    assert summary.replaced == 1
    assert summary.first_ball_no is None
    assert _events(ctx, session_id) == []


def test_unknown_session_raises_and_writes_nothing(ctx: WorkerContext) -> None:
    missing = uuid.uuid4()
    with pytest.raises(LookupError, match=str(missing)):
        run_detection(ctx, missing, THREE_BALLS, FPS)
    with ctx.session_factory() as db:
        assert list(db.scalars(select(BallEvent))) == []


def test_invalid_input_fails_before_any_db_write(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    kept = _seed_event(ctx, session_id, 1, EventSource.AUTO, valid=True)
    with pytest.raises(EventDetectionError, match="fps"):
        run_detection(ctx, session_id, THREE_BALLS, 0.0)
    assert [row.id for row in _events(ctx, session_id)] == [kept]  # nothing replaced


# --- DEPENDENT-ROW GUARD: a re-run never deletes an event downstream rows point at ---


@pytest.mark.parametrize("table_label", sorted(label for _, label in EVENT_DEPENDENT_TABLES))
def test_rerun_with_dependent_rows_aborts_without_modifying(
    ctx: WorkerContext, table_label: str
) -> None:
    session_id = _seed_session(ctx)
    run_detection(ctx, session_id, THREE_BALLS, FPS)
    before = {row.ball_no: row.id for row in _events(ctx, session_id)}
    with session_scope(ctx.session_factory) as db:
        DEPENDENT_SEEDERS[table_label](db, session_id, 2)

    with pytest.raises(DetectionBlockedError) as excinfo:
        run_detection(ctx, session_id, THREE_BALLS, FPS)

    error = excinfo.value
    assert error.session_id == session_id
    assert error.blockers == {2: (table_label,)}  # structured: blocking ball -> tables
    message = str(error)
    assert "ball 2" in message
    assert table_label in message
    assert "nothing was modified" in message
    # Honest remediation: no removal API exists for machine-derived dependents,
    # so the message must not advise a resolution path that does not exist.
    assert "no removal API" in message
    assert "operator intervention" in message
    # Aborted before any write: same rows, same ids, same numbering.
    assert {row.ball_no: row.id for row in _events(ctx, session_id)} == before


def test_rerun_with_correction_history_aborts(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    run_detection(ctx, session_id, THREE_BALLS, FPS)
    first_id = _events(ctx, session_id)[0].id
    with session_scope(ctx.session_factory) as db:
        _seed_correction(db, first_id)

    with pytest.raises(DetectionBlockedError) as excinfo:
        run_detection(ctx, session_id, THREE_BALLS, FPS)

    assert excinfo.value.blockers == {1: ("event corrections",)}
    assert next(row.id for row in _events(ctx, session_id)) == first_id  # untouched


def test_blocked_error_names_every_blocking_ball_and_table(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    run_detection(ctx, session_id, THREE_BALLS, FPS)
    first_id = _events(ctx, session_id)[0].id
    with session_scope(ctx.session_factory) as db:
        _seed_clip(db, session_id, 1)
        _seed_correction(db, first_id)  # ball 1 blocked twice over
        _seed_reference_ball(db, session_id, 3)  # ball 2 stays clean

    with pytest.raises(DetectionBlockedError) as excinfo:
        run_detection(ctx, session_id, THREE_BALLS, FPS)

    assert excinfo.value.blockers == {
        1: ("clips", "event corrections"),
        3: ("reference balls",),
    }
    message = str(excinfo.value)
    assert "ball 1 has clips, event corrections" in message
    assert "ball 3 has reference balls" in message


def test_dependents_on_preserved_balls_do_not_block_rerun(ctx: WorkerContext) -> None:
    """Preserved events keep their numbers, so their dependents stay associated."""
    session_id = _seed_session(ctx)
    run_detection(ctx, session_id, THREE_BALLS, FPS)
    with session_scope(ctx.session_factory) as db:
        first = db.scalars(
            select(BallEvent).where(BallEvent.session_id == session_id, BallEvent.ball_no == 1)
        ).one()
        first.valid = False  # coach rejected ball 1: preserved, number pinned
        _seed_clip(db, session_id, 1)  # its clip must never block a re-run

    summary = run_detection(ctx, session_id, THREE_BALLS, FPS)

    assert summary.replaced == 2  # old balls 2 and 3 (dependency-free) replaced
    assert summary.preserved == 1
    assert summary.first_ball_no == 2  # numbering resumes above the preserved ball
    rows = _events(ctx, session_id)
    assert [row.ball_no for row in rows] == [1, 2, 3, 4]
    assert rows[0].valid is False  # rejected ball kept, still owns its clip


# --- temp-Postgres integration: the real unique constraint + the CLI ---


def _pg_ctx(pg_url: str, tmp_path: Path) -> tuple[Engine, WorkerContext]:
    engine = make_engine(pg_url)
    create_all(engine)
    return engine, _make_ctx(engine, tmp_path)


@pytest.mark.integration
def test_pg_rerun_idempotent_with_preserved_rows(pg_url: str, tmp_path: Path) -> None:
    engine, ctx = _pg_ctx(pg_url, tmp_path)
    session_id = _seed_session(ctx)
    _seed_event(ctx, session_id, 5, EventSource.MANUAL, valid=True)
    first = run_detection(ctx, session_id, THREE_BALLS, FPS)
    # Re-inserting the same ball_no slots must survive uq (session_id, ball_no).
    second = run_detection(ctx, session_id, THREE_BALLS, FPS)
    assert first.first_ball_no == second.first_ball_no == 6
    assert [row.ball_no for row in _events(ctx, session_id)] == [5, 6, 7, 8]
    engine.dispose()


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


@pytest.mark.integration
def test_cli_end_to_end_idempotent(pg_url: str, tmp_path: Path) -> None:
    engine, ctx = _pg_ctx(pg_url, tmp_path)
    session_id = _seed_session(ctx)
    energy_file = tmp_path / "energy.json"
    energy_file.write_text(json.dumps(THREE_BALLS))
    onsets_file = tmp_path / "onsets.csv"
    onsets_file.write_text("1150\n999999\n")
    env = {ENV_DATABASE_URL: pg_url, ENV_STORAGE_ROOT: str(tmp_path / "store")}
    args = [
        "--session-id",
        str(session_id),
        "--energy-file",
        str(energy_file),
        "--fps",
        "100",
        "--onsets-file",
        str(onsets_file),
    ]
    first = _run_cli(args, env)
    assert first.returncode == 0, first.stderr
    assert "3 auto events" in first.stdout
    second = _run_cli(args, env)  # US-D1 AC: re-run replaces, not duplicates
    assert second.returncode == 0, second.stderr
    rows = _events(ctx, session_id)
    assert [row.ball_no for row in rows] == [1, 2, 3]
    assert rows[0].contact_ms == 1150
    engine.dispose()


@pytest.mark.integration
def test_cli_unknown_session_exits_2(pg_url: str, tmp_path: Path) -> None:
    engine, _ = _pg_ctx(pg_url, tmp_path)
    engine.dispose()
    energy_file = tmp_path / "energy.json"
    energy_file.write_text(json.dumps(THREE_BALLS))
    result = _run_cli(
        ["--session-id", str(uuid.uuid4()), "--energy-file", str(energy_file), "--fps", "100"],
        {ENV_DATABASE_URL: pg_url, ENV_STORAGE_ROOT: str(tmp_path / "store")},
    )
    assert result.returncode == 2
    assert "not found" in result.stderr


def test_cli_rejects_bad_session_id(tmp_path: Path) -> None:
    energy_file = tmp_path / "energy.json"
    energy_file.write_text("[0.0]")
    result = _run_cli(
        ["--session-id", "not-a-uuid", "--energy-file", str(energy_file), "--fps", "100"], {}
    )
    assert result.returncode == 2
    assert "not a UUID" in result.stderr


def test_cli_rejects_bad_fps_before_touching_db(tmp_path: Path) -> None:
    energy_file = tmp_path / "energy.json"
    energy_file.write_text("[0.0, 1.0]")
    result = _run_cli(
        ["--session-id", str(uuid.uuid4()), "--energy-file", str(energy_file), "--fps", "0"], {}
    )
    assert result.returncode == 2
    assert "fps" in result.stderr


def test_cli_rejects_empty_energy_file(tmp_path: Path) -> None:
    energy_file = tmp_path / "empty.csv"
    energy_file.write_text("")
    result = _run_cli(
        ["--session-id", str(uuid.uuid4()), "--energy-file", str(energy_file), "--fps", "100"], {}
    )
    assert result.returncode == 2
    assert "no samples" in result.stderr


def test_cli_rejects_missing_onsets_file(tmp_path: Path) -> None:
    energy_file = tmp_path / "energy.json"
    energy_file.write_text("[0.0, 1.0]")
    result = _run_cli(
        [
            "--session-id",
            str(uuid.uuid4()),
            "--energy-file",
            str(energy_file),
            "--fps",
            "100",
            "--onsets-file",
            str(tmp_path / "nope.csv"),
        ],
        {},
    )
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def test_cli_rejects_non_numeric_energy(tmp_path: Path) -> None:
    energy_file = tmp_path / "energy.json"
    energy_file.write_text('["a", "b"]')
    result = _run_cli(
        ["--session-id", str(uuid.uuid4()), "--energy-file", str(energy_file), "--fps", "100"], {}
    )
    assert result.returncode == 2
    assert "expected numbers" in result.stderr
