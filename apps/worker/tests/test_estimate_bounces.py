"""US-F4 bounce-estimation job: upsert, manual-mark safety, calibration mirror."""

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
    Handedness,
    Length,
    Line,
    SessionType,
)
from cricai_data.models import (
    BallEvent,
    BallTrack,
    BounceEstimate,
    BounceMark,
    Calibration,
    CameraConfig,
    Player,
    Session,
)
from cricai_data.storage import FsObjectStore
from cricai_vision import trajectory
from cricai_vision.extrinsics import PlaneCalibration, to_params
from cricai_vision.intrinsics import BoardSpec, IntrinsicsResult
from cricai_vision.intrinsics import to_params as intrinsic_to_params
from cricai_vision.zones import ZoneConfig
from cricai_worker.context import WorkerContext
from cricai_worker.estimate_bounces import (
    BounceEstimationSummary,
    CameraMapping,
    MappingResolver,
    camera_mapping,
    estimate_session_bounces,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

#: Pixel->pitch homography scaling the apex px (500, 400) to (5.0, 0.2) m.
SCALE_CAL = PlaneCalibration(
    matrix=((0.01, 0.0, 0.0), (0.0, 0.0005, 0.0), (0.0, 0.0, 1.0)),
    rms_px=0.0,
    rms_m=0.0,
    n_landmarks=6,
)

#: Alternative homography (double x scale): apex maps to (10.0, 0.2) m.
DOUBLE_CAL = PlaneCalibration(
    matrix=((0.02, 0.0, 0.0), (0.0, 0.0005, 0.0), (0.0, 0.0, 1.0)),
    rms_px=0.0,
    rms_m=0.0,
    n_landmarks=6,
)

#: Third row maps every pixel to the horizon (w == 0): always unmappable.
HORIZON_CAL = PlaneCalibration(
    matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.0)),
    rms_px=0.0,
    rms_m=0.0,
    n_landmarks=6,
)


def _scale_resolver(db: OrmSession, session: Session, camera_id: str) -> CameraMapping | None:
    return CameraMapping(homography=SCALE_CAL)


def _fixed_resolver(mapping: CameraMapping) -> MappingResolver:
    def resolver(db: OrmSession, session: Session, camera_id: str) -> CameraMapping | None:
        return mapping

    return resolver


def _points(apex_px_x: float = 500.0, apex_px_y: float = 400.0) -> list[dict[str, Any]]:
    """Synthetic flight: px_y peaks (image bottom) at ts 300 ms, px_x sweeps."""
    return [
        {
            "frame_no": 10 + k,
            "ts_ms": 300.0 + 30.0 * k,
            "px_x": apex_px_x + 4.0 * k,
            "px_y": apex_px_y - 8.0 * abs(k),
            "score": 0.9,
            "bridged": False,
        }
        for k in range(-4, 5)
    ]


def _payload(
    *, apex_px: tuple[float, float] = (500.0, 400.0), full_toss: bool = False
) -> dict[str, Any]:
    segments = (
        []
        if full_toss
        else [
            {"kind": "pre_bounce", "start_ms": 0.0, "end_ms": 300.0, "confidence": 0.9},
            {"kind": "post_bounce", "start_ms": 300.0, "end_ms": 800.0, "confidence": 0.8},
        ]
    )
    return {
        "points": _points(*apex_px),
        "segments": segments,
        "flags": {"identity_risk": False, "long_gap": False},
    }


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
    handedness: Handedness = Handedness.RIGHT,
    session_type: SessionType = SessionType.BATTING,
) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20), handedness=handedness)
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 8),
            session_type=session_type,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.commit()
        return session.id


def _track_key(session_id: uuid.UUID, ball_no: int, camera_id: str) -> str:
    return f"sessions/{session_id}/balls/{ball_no}/track-{camera_id}.json"


def _ensure_event(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    exists = db.scalar(
        select(BallEvent).where(BallEvent.session_id == session_id, BallEvent.ball_no == ball_no)
    )
    if exists is None:
        db.add(
            BallEvent(
                session_id=session_id,
                ball_no=ball_no,
                start_ms=0,
                release_ms=100,
                end_ms=2000,
                confidence=0.9,
            )
        )


def _reject_event(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> None:
    """Flip the event invalid, the way POST /events/{id}/reject does (US-D4)."""
    with ctx.session_factory() as db:
        event = db.scalar(
            select(BallEvent).where(
                BallEvent.session_id == session_id, BallEvent.ball_no == ball_no
            )
        )
        assert event is not None
        event.valid = False
        db.commit()


def _seed_track(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    camera_id: str = "C3",
    payload: dict[str, Any] | None = None,
    raw_bytes: bytes | None = None,
    store_payload: bool = True,
    tracker_version: str = "trk-1",
) -> str:
    key = _track_key(session_id, ball_no, camera_id)
    if store_payload:
        data = raw_bytes if raw_bytes is not None else json.dumps(payload or _payload()).encode()
        ctx.store.put(key, data)
    with ctx.session_factory() as db:
        _ensure_event(db, session_id, ball_no)
        db.add(
            BallTrack(
                session_id=session_id,
                ball_no=ball_no,
                camera_id=camera_id,
                tracker_version=tracker_version,
                points_key=key,
                coverage=0.9,
                segments=[],
                flags={},
                confidence=0.8,
            )
        )
        db.commit()
    return key


def _estimates(ctx: WorkerContext, session_id: uuid.UUID) -> list[BounceEstimate]:
    with ctx.session_factory() as db:
        return list(
            db.scalars(
                select(BounceEstimate)
                .where(BounceEstimate.session_id == session_id)
                .order_by(BounceEstimate.ball_no)
            )
        )


# ---------------------------------------------------------------------------
# Estimation, zone classes, idempotent upsert
# ---------------------------------------------------------------------------


def test_estimates_tracked_balls_with_zone_classes(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    _seed_track(ctx, session_id, 2)
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert summary == BounceEstimationSummary(
        session_id=str(session_id), estimated=2, removed=0, skipped=()
    )
    rows = _estimates(ctx, session_id)
    assert [row.ball_no for row in rows] == [1, 2]
    for row in rows:
        assert row.pitch_x == pytest.approx(5.0)
        assert row.pitch_y == pytest.approx(0.2)
        assert (row.line, row.length) == (Line.OFF, Length.GOOD)
        assert row.confidence == pytest.approx(0.8)  # min(pre 0.9, post 0.8)
        assert row.tracker_version == "trk-1+bounce-est-1"


def test_left_hander_mirrors_line_channel(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, handedness=Handedness.LEFT)
    _seed_track(ctx, session_id, 1)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    (row,) = _estimates(ctx, session_id)
    assert row.line is Line.LEG  # +0.2 m mirrors to the leg side for a LH batter


def test_bowling_session_zones_ignore_the_bowlers_batting_handedness(tmp_path: Path) -> None:
    """Phase-6 finding 8 (US-I4/I5): a BOWLING session's cached zones use the
    fixed canonical right-hand frame — the SAME frame target scoring pins —
    never the bowler's own batting handedness. A left-handed kid's pitch map
    must not mirror against their accuracy scorecard."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, handedness=Handedness.LEFT, session_type=SessionType.BOWLING)
    _seed_track(ctx, session_id, 1)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    (row,) = _estimates(ctx, session_id)
    assert (row.line, row.length) == (Line.OFF, Length.GOOD)  # canonical frame, not LEG


def test_bowling_zone_cache_agrees_with_target_scoring(tmp_path: Path) -> None:
    """Cross-surface consistency (finding 8): for a left-handed player's
    bowling session, the cached line/length classify the SAME channel that
    ``trajectory.score_delivery`` scores the delivery against — the pitch map
    and the accuracy scorecard can never contradict each other."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx, handedness=Handedness.LEFT, session_type=SessionType.BOWLING)
    _seed_track(ctx, session_id, 1)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    (row,) = _estimates(ctx, session_id)
    score = trajectory.score_delivery(
        ZoneConfig(),
        ball_no=1,
        target=trajectory.TargetZone(key="t1", line=row.line, length=row.length),
        bounce=trajectory.BouncePoint(row.pitch_x, row.pitch_y, row.confidence),
    )
    assert score.hit is True  # the cached zone IS the channel scoring sees


def test_custom_zone_config_matches_router_vocabulary(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    widened = ZoneConfig(
        length_bands_m={
            Length.YORKER: (0.0, 2.0),
            Length.FULL: (2.0, 10.0),  # swallows the default GOOD band
            Length.GOOD: (10.0, 12.0),
            Length.SHORT: (12.0, float("inf")),
        }
    )
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver, zone_config=widened)
    (row,) = _estimates(ctx, session_id)
    assert row.length is Length.FULL  # 5.0 m under the widened bands


def test_off_pitch_vertex_keeps_null_zone_classes(tmp_path: Path) -> None:
    """Zone classification failure never fails the ball: NULL classes (US-C5)."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    wild = PlaneCalibration(
        matrix=((0.1, 0.0, 0.0), (0.0, 0.0005, 0.0), (0.0, 0.0, 1.0)),  # 50 m: off the pitch
        rms_px=0.0,
        rms_m=0.0,
        n_landmarks=6,
    )
    summary = estimate_session_bounces(
        ctx, session_id, mapping_resolver=_fixed_resolver(CameraMapping(homography=wild))
    )
    assert summary.estimated == 1
    (row,) = _estimates(ctx, session_id)
    assert row.pitch_x == pytest.approx(50.0)
    assert row.line is None and row.length is None


def test_rerun_upserts_the_same_row(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    key = _seed_track(ctx, session_id, 1)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    (first,) = _estimates(ctx, session_id)
    ctx.store.put(key, json.dumps(_payload(apex_px=(600.0, 400.0))).encode())
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert summary.estimated == 1
    (row,) = _estimates(ctx, session_id)
    assert row.id == first.id  # upsert on (session, ball): never a duplicate row
    assert row.pitch_x == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# Manual bounce_marks are NEVER touched (pinned Phase-4 precedence contract)
# ---------------------------------------------------------------------------


def _mark_snapshot(ctx: WorkerContext, session_id: uuid.UUID) -> list[tuple[Any, ...]]:
    with ctx.session_factory() as db:
        return [
            (
                mark.id,
                mark.ball_no,
                mark.camera_id,
                mark.frame_no,
                mark.px_x,
                mark.px_y,
                mark.pitch_x,
                mark.pitch_y,
                mark.line,
                mark.length,
                mark.flagged_for_review,
                mark.created_at,
            )
            for mark in db.scalars(
                select(BounceMark)
                .where(BounceMark.session_id == session_id)
                .order_by(BounceMark.ball_no, BounceMark.camera_id)
            )
        ]


@pytest.mark.safety
def test_job_never_writes_updates_or_deletes_bounce_marks(tmp_path: Path) -> None:
    """US-F4 AC: manual overrides are preserved untouched across reprocessing."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    _seed_track(ctx, session_id, 2, payload=_payload(full_toss=True))  # removal path
    with ctx.session_factory() as db:
        for ball_no, camera_id in ((1, "C3"), (1, "C4"), (2, "C3")):
            db.add(
                BounceMark(
                    session_id=session_id,
                    ball_no=ball_no,
                    camera_id=camera_id,
                    frame_no=100 + ball_no,
                    px_x=512.0,
                    px_y=300.0,
                    pitch_x=6.5,
                    pitch_y=0.25,
                    line=Line.OFF,
                    length=Length.GOOD,
                    flagged_for_review=ball_no == 1,
                )
            )
        db.commit()
    before = _mark_snapshot(ctx, session_id)
    assert len(before) == 3
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert _mark_snapshot(ctx, session_id) == before
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)  # reprocess
    assert _mark_snapshot(ctx, session_id) == before  # manual rows byte-identical


# ---------------------------------------------------------------------------
# Honest skips: no bounce, missing inputs, stale-row handling
# ---------------------------------------------------------------------------


def test_full_toss_yields_no_row_and_removes_a_stale_one(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    key = _seed_track(ctx, session_id, 1)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert len(_estimates(ctx, session_id)) == 1
    ctx.store.put(key, json.dumps(_payload(full_toss=True)).encode())  # re-tracked: full toss
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert summary.estimated == 0
    assert summary.removed == 1  # the stale auto row lied; it is gone
    assert summary.skipped[0][0] == 1
    assert "full toss or tracking failure" in summary.skipped[0][1]
    assert _estimates(ctx, session_id) == []


def test_full_toss_without_a_stale_row_just_skips(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1, payload=_payload(full_toss=True))
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert (summary.estimated, summary.removed) == (0, 0)
    assert _estimates(ctx, session_id) == []


def test_unavailable_inputs_preserve_the_existing_row(tmp_path: Path) -> None:
    """Transient infrastructure gaps never destroy previously computed rows."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    key = _seed_track(ctx, session_id, 1)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    ctx.store.delete(key)
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert (summary.estimated, summary.removed) == (0, 0)
    assert "missing from the object store" in summary.skipped[0][1]
    assert len(_estimates(ctx, session_id)) == 1  # preserved


def test_no_bounce_camera_never_deletes_while_another_camera_is_unavailable(tmp_path: Path) -> None:
    """A per-camera no-bounce is not ball truth: when the camera that produced
    the stored row (C4) hits a transient input gap, C3's honest no-bounce must
    not delete the previously computed estimate."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1, camera_id="C3", payload=_payload(full_toss=True))
    key = _seed_track(ctx, session_id, 1, camera_id="C4")
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert len(_estimates(ctx, session_id)) == 1  # stored from C4
    ctx.store.delete(key)  # transient object-store gap on the source camera
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert (summary.estimated, summary.removed) == (0, 0)
    reason = summary.skipped[0][1]
    assert "C3: no pre_bounce/post_bounce boundary" in reason
    assert "C4: track payload missing from the object store" in reason
    assert len(_estimates(ctx, session_id)) == 1  # preserved


def test_unmappable_vertex_preserves_the_existing_row(tmp_path: Path) -> None:
    """A degenerate-but-loadable homography is a calibration artifact, never a
    no-bounce truth: the previously computed row must survive the bad era."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    summary = estimate_session_bounces(
        ctx, session_id, mapping_resolver=_fixed_resolver(CameraMapping(homography=HORIZON_CAL))
    )
    assert (summary.estimated, summary.removed) == (0, 0)
    assert "unmappable" in summary.skipped[0][1]
    assert len(_estimates(ctx, session_id)) == 1  # preserved


def test_rejected_event_ball_is_ignored_and_its_phantom_row_removed(tmp_path: Path) -> None:
    """Rejected events are not balls (US-D4): their tracks are never estimated
    and a previously written estimate row is cleaned up, not re-created."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    _seed_track(ctx, session_id, 2)
    estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert [row.ball_no for row in _estimates(ctx, session_id)] == [1, 2]
    _reject_event(ctx, session_id, 2)  # coach rejects the false event
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert summary == BounceEstimationSummary(
        session_id=str(session_id), estimated=1, removed=1, skipped=()
    )
    assert [row.ball_no for row in _estimates(ctx, session_id)] == [1]


def test_malformed_payloads_and_missing_calibration_skip_loudly(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1, raw_bytes=b"not json")
    _seed_track(ctx, session_id, 2, raw_bytes=json.dumps({"points": "nope"}).encode())
    _seed_track(ctx, session_id, 3, camera_id="C5")

    def only_c3(db: OrmSession, session: Session, camera_id: str) -> CameraMapping | None:
        return CameraMapping(homography=SCALE_CAL) if camera_id == "C3" else None

    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=only_c3)
    assert summary.estimated == 0
    reasons = dict(summary.skipped)
    assert "malformed track payload" in reasons[1]
    assert "malformed track payload" in reasons[2]
    assert reasons[3] == "C5: no usable extrinsic calibration"


def test_camera_fallback_uses_the_next_camera(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1, camera_id="C3", raw_bytes=b"broken")
    _seed_track(
        ctx,
        session_id,
        1,
        camera_id="C4",
        payload=_payload(apex_px=(700.0, 400.0)),
        tracker_version="trk-c4",
    )
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert summary.estimated == 1
    (row,) = _estimates(ctx, session_id)
    assert row.pitch_x == pytest.approx(7.0)  # C4's apex: the fallback camera won
    assert row.tracker_version == "trk-c4+bounce-est-1"


def test_both_cameras_no_bounce_reports_the_representative_reason(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1, camera_id="C3", payload=_payload(full_toss=True))
    _seed_track(ctx, session_id, 1, camera_id="C4", payload=_payload(full_toss=True))
    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=_scale_resolver)
    assert summary.skipped[0][1].startswith("C3:")  # lowest camera is the representative


def test_all_cameras_unavailable_joins_every_reason(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1, camera_id="C3")
    _seed_track(ctx, session_id, 1, camera_id="C4", store_payload=False)

    def only_c4(db: OrmSession, session: Session, camera_id: str) -> CameraMapping | None:
        return CameraMapping(homography=SCALE_CAL) if camera_id == "C4" else None

    summary = estimate_session_bounces(ctx, session_id, mapping_resolver=only_c4)
    reason = summary.skipped[0][1]
    assert "C3: no usable extrinsic calibration" in reason
    assert "C4: track payload missing from the object store" in reason


def test_crash_mid_run_keeps_prior_balls_committed(tmp_path: Path) -> None:
    """Per-ball commits: a crash never strands completed balls (crash-resume)."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    _seed_track(ctx, session_id, 2)
    calls = {"n": 0}

    def crashing(db: OrmSession, session: Session, camera_id: str) -> CameraMapping | None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("calibration store exploded")
        return CameraMapping(homography=SCALE_CAL)

    with pytest.raises(RuntimeError, match="calibration store exploded"):
        estimate_session_bounces(ctx, session_id, mapping_resolver=crashing)
    rows = _estimates(ctx, session_id)
    assert [row.ball_no for row in rows] == [1]  # ball 1 landed durably


def test_unknown_session_raises(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="session not found"):
        estimate_session_bounces(ctx, uuid.uuid4(), mapping_resolver=_scale_resolver)


# ---------------------------------------------------------------------------
# Default calibration resolver (mirrors the manual click path, no API import)
# ---------------------------------------------------------------------------


def _seed_camera(db: OrmSession, camera_id: str, *, era_no: int = 1, active: bool = True) -> None:
    db.add(
        CameraConfig(
            camera_id=camera_id,
            era_no=era_no,
            active=active,
            position_label="side",
            xyz_offset_m={"x": 0.0, "y": 0.0, "z": 0.0},
            height_m=1.5,
            fps=120,
            resolution="1920x1080",
        )
    )


def _seed_extrinsic(
    db: OrmSession,
    camera_id: str,
    cal: PlaneCalibration,
    *,
    era_no: int = 1,
    valid: bool = True,
    created_at: datetime.datetime | None = None,
) -> Calibration:
    record = Calibration(
        camera_id=camera_id,
        era_no=era_no,
        kind=CalibrationKind.EXTRINSIC,
        params=to_params(cal),
        valid=valid,
    )
    if created_at is not None:
        record.created_at = created_at
    db.add(record)
    return record


def _intrinsic_params(dist: tuple[float, ...]) -> dict[str, Any]:
    return intrinsic_to_params(
        IntrinsicsResult(
            camera_matrix=((1400.0, 0.0, 960.0), (0.0, 1400.0, 540.0), (0.0, 0.0, 1.0)),
            dist_coeffs=dist,
            reprojection_error_px=0.3,
            board_spec=BoardSpec(),
            n_views=8,
            captured_on="2026-07-01",
        )
    )


def test_default_resolver_runs_the_job_end_to_end(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    with ctx.session_factory() as db:
        _seed_camera(db, "C3")
        _seed_extrinsic(db, "C3", SCALE_CAL)
        db.commit()
    summary = estimate_session_bounces(ctx, session_id)  # default resolver
    assert summary.estimated == 1
    (row,) = _estimates(ctx, session_id)
    assert row.pitch_x == pytest.approx(5.0)


def test_resolver_prefers_the_session_attached_calibration(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        session = db.get(Session, session_id)
        assert session is not None
        _seed_camera(db, "C3")
        _seed_extrinsic(db, "C3", SCALE_CAL)  # current era says 5.0 m
        attached = _seed_extrinsic(db, "C3", DOUBLE_CAL)  # US-C3: the linked era wins
        db.flush()
        session.calibration_id = attached.id
        db.commit()
        mapping = camera_mapping(db, session, "C3")
        assert mapping is not None
        assert mapping.homography == DOUBLE_CAL
        assert mapping.intrinsic_params is None


@pytest.mark.parametrize(
    "spoil",
    ["invalid", "wrong_camera", "wrong_kind", "dangling"],
)
def test_resolver_falls_back_when_the_attached_record_is_unusable(
    tmp_path: Path, spoil: str
) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        session = db.get(Session, session_id)
        assert session is not None
        _seed_camera(db, "C3")
        _seed_extrinsic(db, "C3", SCALE_CAL)
        if spoil == "invalid":
            attached = _seed_extrinsic(db, "C3", DOUBLE_CAL, valid=False)
        elif spoil == "wrong_camera":
            attached = _seed_extrinsic(db, "C4", DOUBLE_CAL)
        elif spoil == "wrong_kind":
            attached = Calibration(
                camera_id="C3",
                era_no=1,
                kind=CalibrationKind.INTRINSIC,
                params=_intrinsic_params((0.0,) * 5),
            )
            db.add(attached)
        else:  # dangling id: the record is gone
            attached = None
        db.flush()
        session.calibration_id = attached.id if attached is not None else uuid.uuid4()
        db.commit()
        mapping = camera_mapping(db, session, "C3")
        assert mapping is not None
        assert mapping.homography == SCALE_CAL  # fell back to the current era


def test_resolver_uses_the_newest_valid_extrinsic_of_the_active_era(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    old = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    new = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
    with ctx.session_factory() as db:
        session = db.get(Session, session_id)
        assert session is not None
        _seed_camera(db, "C3", era_no=1, active=True)
        _seed_camera(db, "C3", era_no=2, active=True)
        _seed_extrinsic(db, "C3", SCALE_CAL, era_no=1, created_at=new)  # older era: ignored
        _seed_extrinsic(db, "C3", SCALE_CAL, era_no=2, created_at=old)
        _seed_extrinsic(db, "C3", DOUBLE_CAL, era_no=2, created_at=new)  # newest of era 2
        _seed_extrinsic(db, "C3", SCALE_CAL, era_no=2, created_at=new, valid=False)
        db.commit()
        mapping = camera_mapping(db, session, "C3")
        assert mapping is not None
        assert mapping.homography == DOUBLE_CAL


@pytest.mark.parametrize("gap", ["no_camera", "no_extrinsic", "unusable_params"])
def test_resolver_returns_none_when_no_usable_extrinsic_exists(tmp_path: Path, gap: str) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        session = db.get(Session, session_id)
        assert session is not None
        if gap != "no_camera":
            _seed_camera(db, "C3")
        if gap == "no_extrinsic":
            _seed_extrinsic(db, "C3", SCALE_CAL, valid=False)
        if gap == "unusable_params":
            record = _seed_extrinsic(db, "C3", SCALE_CAL)
            record.params = {"version": 99}
        db.commit()
        assert camera_mapping(db, session, "C3") is None


def test_resolver_attaches_the_same_era_intrinsic(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    params = _intrinsic_params((0.0,) * 5)
    with ctx.session_factory() as db:
        session = db.get(Session, session_id)
        assert session is not None
        _seed_camera(db, "C3")
        _seed_extrinsic(db, "C3", SCALE_CAL)
        db.add(Calibration(camera_id="C3", era_no=1, kind=CalibrationKind.INTRINSIC, params=params))
        db.commit()
        mapping = camera_mapping(db, session, "C3")
        assert mapping is not None
        assert mapping.intrinsic_params == params


# ---------------------------------------------------------------------------
# Intrinsic undistortion of track pixels (mirrors the manual click path)
# ---------------------------------------------------------------------------


def test_zero_distortion_intrinsic_is_identity(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    estimate_session_bounces(
        ctx,
        session_id,
        mapping_resolver=_fixed_resolver(
            CameraMapping(homography=SCALE_CAL, intrinsic_params=_intrinsic_params((0.0,) * 5))
        ),
    )
    (row,) = _estimates(ctx, session_id)
    assert row.pitch_x == pytest.approx(5.0, abs=1e-6)
    assert row.pitch_y == pytest.approx(0.2, abs=1e-6)


def test_nonzero_distortion_moves_the_mapped_bounce(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    estimate_session_bounces(
        ctx,
        session_id,
        mapping_resolver=_fixed_resolver(
            CameraMapping(
                homography=SCALE_CAL,
                intrinsic_params=_intrinsic_params((-0.2, 0.0, 0.0, 0.0, 0.0)),
            )
        ),
    )
    (row,) = _estimates(ctx, session_id)
    assert row.pitch_x != pytest.approx(5.0, abs=1e-6)  # undistortion was applied


def test_unusable_intrinsic_skips_the_camera_loudly(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_track(ctx, session_id, 1)
    summary = estimate_session_bounces(
        ctx,
        session_id,
        mapping_resolver=_fixed_resolver(
            CameraMapping(homography=SCALE_CAL, intrinsic_params={"version": 99})
        ),
    )
    assert summary.estimated == 0
    assert "unusable intrinsic calibration" in summary.skipped[0][1]
