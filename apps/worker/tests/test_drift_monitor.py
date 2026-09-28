"""US-L4 weekly drift monitor job: DB wiring, alert persistence, idempotency,
and the Phase-6 canary re-derivation (the frozen canary session re-derived
through the events call-adapter + the real bounce/zone pipeline every run)."""

import datetime
import math
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from cricai_coaching.drift import WEEKLY_SAMPLE_SIZE, canary_expectations
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    AlertAudience,
    BowlerSource,
    CalibrationKind,
    ClipStatus,
    Contact,
    EventSource,
    Footwork,
    Length,
    Line,
    Outcome,
    SessionType,
    Shot,
    VideoStatus,
)
from cricai_data.models import (
    Alert,
    AuditLog,
    BallEvent,
    BallTag,
    BallTrack,
    BounceEstimate,
    Calibration,
    CameraConfig,
    Clip,
    Player,
    PoseTrack,
    Report,
    Session,
    Video,
)
from cricai_data.storage import FsObjectStore
from cricai_vision.extrinsics import PlaneCalibration, to_params
from cricai_worker import drift_monitor as dm
from cricai_worker.context import WorkerContext
from cricai_worker.drift_monitor import (
    CANARY_SESSION_ID,
    DriftMonitorSummary,
    bounce_estimate_fields,
    derive_canary_observed,
    ensure_canary_session,
    load_quality_inputs,
    run_weekly_drift_monitor,
)
from cricai_worker.estimate_bounces import camera_mapping
from cricai_worker.rollup_reports import rollup_all_players
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cricai_testing.apptest import PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app

WEEK = datetime.date(2026, 7, 6)  # a Monday
PRIOR_WEEK = WEEK - datetime.timedelta(days=7)  # the week a WEEK run compares


def _boom_deriver(_ctx: WorkerContext) -> Mapping[int, Mapping[str, object]]:
    raise RuntimeError("canary pipeline exploded")


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "storage")
    )


def _session(db: OrmSession, session_date: datetime.date = WEEK) -> Session:
    player = Player(name="Test Player", birthdate=datetime.date(2015, 1, 1))
    db.add(player)
    db.flush()
    session = Session(
        player_id=player.id,
        session_date=session_date,
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add(session)
    db.flush()
    return session


def _event(db: OrmSession, session: Session, ball_no: int, *, valid: bool = True) -> None:
    db.add(
        BallEvent(
            session_id=session.id,
            ball_no=ball_no,
            start_ms=ball_no * 1000,
            release_ms=ball_no * 1000 + 100,
            end_ms=ball_no * 1000 + 900,
            confidence=0.9,
            valid=valid,
        )
    )


def _tag(
    db: OrmSession,
    session: Session,
    ball_no: int,
    *,
    line: Line = Line.OFF,
    length: Length = Length.GOOD,
    eligible: bool = True,
) -> None:
    db.add(
        BallTag(
            session_id=session.id,
            ball_no=ball_no,
            line=line,
            length=length,
            shot=Shot.DEFEND,
            footwork=Footwork.FRONT,
            contact=Contact.MIDDLE,
            outcome=Outcome.CONTROLLED_GROUND_SHOT,
            control=True,
            ground_truth_eligible=eligible,
            created_by="coach",
        )
    )


def _estimate(
    db: OrmSession,
    session: Session,
    ball_no: int,
    *,
    line: Line | None = Line.OFF,
    length: Length | None = Length.GOOD,
) -> None:
    db.add(
        BounceEstimate(
            session_id=session.id,
            ball_no=ball_no,
            pitch_x=0.0,
            pitch_y=5.0,
            line=line,
            length=length,
            confidence=0.8,
            tracker_version="t1",
        )
    )


def _full_artifacts(db: OrmSession, session: Session, ball_no: int) -> None:
    """Clips on two aligned cameras + full pose/track for one ball."""
    for camera_id in ("C1", "C2"):
        db.add(
            Clip(
                session_id=session.id,
                ball_no=ball_no,
                camera_id=camera_id,
                start_ms=ball_no * 1000,
                end_ms=ball_no * 1000 + 900,
                status=ClipStatus.CUT,
            )
        )
        db.add(
            PoseTrack(
                session_id=session.id,
                ball_no=ball_no,
                camera_id=camera_id,
                model_name="pose",
                model_version="1",
                landmarks_key=f"pose-{session.id}-{ball_no}-{camera_id}",
                frame_count=100,
                availability=1.0,
                subject_confidence=0.9,
            )
        )
        db.add(
            BallTrack(
                session_id=session.id,
                ball_no=ball_no,
                camera_id=camera_id,
                tracker_version="t1",
                points_key=f"track-{session.id}-{ball_no}-{camera_id}",
                coverage=1.0,
                segments=[],
                confidence=0.9,
            )
        )


def _fresh_calibration(db: OrmSession, session: Session) -> None:
    calibration = Calibration(
        camera_id="C3",
        kind=CalibrationKind.EXTRINSIC,
        params={},
        created_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
    )
    db.add(calibration)
    db.flush()
    session.calibration_id = calibration.id


def _alerts(db: OrmSession, code: str | None = None) -> list[Alert]:
    query = select(Alert)
    if code is not None:
        query = query.where(Alert.code == code)
    return list(db.scalars(query))


def _seed_good_session(
    db: OrmSession, *, n_balls: int = 12, session_date: datetime.date = WEEK
) -> Session:
    """A high-quality session whose auto line/length agree with manual tags."""
    session = _session(db, session_date=session_date)
    _fresh_calibration(db, session)
    for ball_no in range(1, n_balls + 1):
        _event(db, session, ball_no)
        _tag(db, session, ball_no)
        _estimate(db, session, ball_no)
        _full_artifacts(db, session, ball_no)
    return session


class TestBounceEstimateFields:
    def test_maps_zone_classes_and_keeps_none(self, ctx: WorkerContext) -> None:
        db = ctx.session_factory()
        session = _session(db)
        _estimate(db, session, 1, line=Line.LEG, length=Length.FULL)
        _estimate(db, session, 2, line=None, length=None)
        fields = bounce_estimate_fields(db, session.id)
        assert fields == {
            1: {"line": "leg", "length": "full"},
            2: {"line": None, "length": None},
        }
        db.close()


class TestLoadQualityInputs:
    def test_reads_all_artifact_rows(self, ctx: WorkerContext) -> None:
        db = ctx.session_factory()
        session = _seed_good_session(db, n_balls=2)
        _event(db, session, 3, valid=False)  # rejected events are not balls
        db.add(
            Video(
                session_id=session.id,
                camera_id="C1",
                object_key=f"v-{session.id}",
                filename="c1.mp4",
                checksum_sha256="0" * 64,
                size_bytes=1,
                probe={"mean_luma_samples": [120.0, 10, "glare", None]},
            )
        )
        db.add(
            Video(
                session_id=session.id,
                camera_id="C2",
                object_key=f"v2-{session.id}",
                filename="c2.mp4",
                checksum_sha256="1" * 64,
                size_bytes=1,
                probe=None,  # unprobed video: no exposure evidence
            )
        )
        db.flush()
        inputs = load_quality_inputs(db, session)
        assert inputs.ball_nos == (1, 2)
        assert inputs.event_offsets_ms == {
            1: {"C1": 1000.0, "C2": 1000.0},
            2: {"C1": 2000.0, "C2": 2000.0},
        }
        assert inputs.luma_samples == {"C1": [120.0, 10.0]}  # non-numerics dropped
        assert inputs.pose_availability[1] == {"C1": 1.0, "C2": 1.0}
        assert inputs.track_coverage[2] == {"C1": 1.0, "C2": 1.0}
        assert inputs.calibration_age_days == 5.0
        assert inputs.calibration_suspect is False
        db.close()

    def test_pending_clips_and_probe_without_samples_are_ignored(self, ctx: WorkerContext) -> None:
        db = ctx.session_factory()
        session = _session(db)
        _event(db, session, 1)
        db.add(
            Clip(
                session_id=session.id,
                ball_no=1,
                camera_id="C1",
                start_ms=0,
                end_ms=900,
                status=ClipStatus.PENDING,
            )
        )
        db.add(
            Video(
                session_id=session.id,
                camera_id="C1",
                object_key=f"v-{session.id}",
                filename="c1.mp4",
                checksum_sha256="2" * 64,
                size_bytes=1,
                probe={"fps": 120},  # probed, but no luma samples recorded
            )
        )
        db.flush()
        inputs = load_quality_inputs(db, session)
        assert inputs.event_offsets_ms == {}
        assert inputs.luma_samples == {}
        db.close()

    def test_calibration_age_edge_cases(self, ctx: WorkerContext) -> None:
        db = ctx.session_factory()
        no_calibration = _session(db)
        assert load_quality_inputs(db, no_calibration).calibration_age_days is None

        dangling = _session(db)
        dangling.calibration_id = uuid.uuid4()  # row vanished (no FK enforcement)
        assert load_quality_inputs(db, dangling).calibration_age_days is None

        future = _session(db, session_date=datetime.date(2026, 6, 1))
        _fresh_calibration(db, future)  # calibrated after the session: clamp to 0
        db.flush()
        assert load_quality_inputs(db, future).calibration_age_days == 0.0
        db.close()

    def test_suspect_flag_passes_through(self, ctx: WorkerContext) -> None:
        db = ctx.session_factory()
        session = _session(db)
        session.calibration_suspect = True
        db.flush()
        assert load_quality_inputs(db, session).calibration_suspect is True
        db.close()


class TestRunWeeklyDriftMonitor:
    def test_healthy_week_writes_nothing(self, ctx: WorkerContext) -> None:
        """Prior week's 12 sampled balls agree, this week's session is high
        quality and fully tagged, the canary matches: no alert at all."""
        with ctx.session_factory() as db:
            _seed_good_session(db, n_balls=12, session_date=PRIOR_WEEK)  # tagged sample
            _seed_good_session(db, n_balls=12)  # the week just ended
            db.commit()
        summary = run_weekly_drift_monitor(ctx, WEEK, canary_observed=canary_expectations())
        assert summary.sessions == 1  # only the week just ended is scored/sampled
        assert summary.compared_week_start == PRIOR_WEEK.isoformat()
        assert summary.paired_balls == 12  # the prior week's tagged sample
        assert summary.sampled == ()  # every ball already manually tagged
        with ctx.session_factory() as db:
            assert _alerts(db) == []
        assert summary.alerts_written == 0
        assert summary.alerts_skipped == 0

    def test_disagreement_in_the_compared_week_fires_developer_drift_alert(
        self, ctx: WorkerContext
    ) -> None:
        with ctx.session_factory() as db:
            session = _session(db, session_date=PRIOR_WEEK)  # the tagged sample week
            for ball_no in range(1, 13):
                _event(db, session, ball_no)
                _tag(db, session, ball_no, line=Line.OFF)
                # auto line disagrees on every ball; length agrees
                _estimate(db, session, ball_no, line=Line.LEG)
            db.commit()
        run_weekly_drift_monitor(ctx, WEEK)
        with ctx.session_factory() as db:
            drift = _alerts(db, "drift_agreement")
            assert len(drift) == 1
            alert = drift[0]
            assert alert.audience is AlertAudience.DEVELOPER
            assert alert.severity == "warning"
            assert alert.detail["field"] == "line"
            assert alert.detail["agreement_pct"] == 0.0
            assert alert.detail["week_start"] == WEEK.isoformat()
            assert alert.detail["compared_week_start"] == PRIOR_WEEK.isoformat()
            assert alert.detail["dedupe_key"] == f"drift_agreement:{WEEK.isoformat()}:line"

    def test_two_drifting_fields_write_distinct_same_code_alerts(self, ctx: WorkerContext) -> None:
        """Two fields drifting in one run insert distinct ``drift_agreement`` rows.

        Persisting the second exercises the dedupe scan's skip path: an existing
        same-code row whose ``dedupe_key`` differs must not block the insert.
        """
        with ctx.session_factory() as db:
            session = _session(db, session_date=PRIOR_WEEK)
            for ball_no in range(1, 13):
                _event(db, session, ball_no)
                _tag(db, session, ball_no, line=Line.OFF, length=Length.GOOD)
                # auto line AND length disagree on every ball
                _estimate(db, session, ball_no, line=Line.LEG, length=Length.SHORT)
            db.commit()
        run_weekly_drift_monitor(ctx, WEEK)
        with ctx.session_factory() as db:
            drift = _alerts(db, "drift_agreement")
            assert {a.detail["field"] for a in drift} == {"line", "length"}
            assert {a.detail["dedupe_key"] for a in drift} == {
                f"drift_agreement:{WEEK.isoformat()}:line",
                f"drift_agreement:{WEEK.isoformat()}:length",
            }

    def test_short_compared_sample_fires_shortfall_alert(self, ctx: WorkerContext) -> None:
        with ctx.session_factory() as db:
            _seed_good_session(db, n_balls=3, session_date=PRIOR_WEEK)
            db.commit()
        run_weekly_drift_monitor(ctx, WEEK)
        with ctx.session_factory() as db:
            shortfall = _alerts(db, "drift_sample_short")
            assert len(shortfall) == 1
            assert shortfall[0].detail["paired"] == 3
            assert shortfall[0].detail["week_start"] == WEEK.isoformat()
            assert shortfall[0].detail["compared_week_start"] == PRIOR_WEEK.isoformat()

    def test_low_quality_session_writes_parent_honesty_alert(self, ctx: WorkerContext) -> None:
        with ctx.session_factory() as db:
            session = _session(db)
            for ball_no in range(1, 13):
                _event(db, session, ball_no)
                _tag(db, session, ball_no)
                _estimate(db, session, ball_no)
            # no clips/pose/track/calibration: quality collapses
            session_id = session.id
            db.commit()
        run_weekly_drift_monitor(ctx, WEEK)
        with ctx.session_factory() as db:
            honesty = _alerts(db, "data_quality_low")
            assert len(honesty) == 1
            alert = honesty[0]
            assert alert.audience is AlertAudience.PARENT
            assert alert.session_id == session_id
            assert alert.detail["banner"] is not None
            assert alert.detail["composite"] < 0.7
            assert alert.detail["session_id"] == str(session_id)

    def test_weekly_sample_alert_lists_untagged_balls(self, ctx: WorkerContext) -> None:
        with ctx.session_factory() as db:
            session = _seed_good_session(db, n_balls=12)
            for ball_no in range(13, 43):  # 30 untagged balls, some without estimates
                _event(db, session, ball_no)
                if ball_no % 2 == 0:
                    _estimate(db, session, ball_no, length=Length.SHORT)
            session_id = str(session.id)
            db.commit()
        summary = run_weekly_drift_monitor(ctx, WEEK)
        assert len(summary.sampled) == WEEKLY_SAMPLE_SIZE
        assert all(sid == session_id and 13 <= ball_no <= 42 for sid, ball_no in summary.sampled)
        with ctx.session_factory() as db:
            sample_alerts = _alerts(db, "ground_truth_sample")
            assert len(sample_alerts) == 1
            balls: list[dict[str, Any]] = sample_alerts[0].detail["balls"]
            assert len(balls) == WEEKLY_SAMPLE_SIZE
            strata = {ball["stratum"] for ball in balls}
            assert len(strata) > 1  # stratified across zones, not one clump

    def test_sessions_outside_the_week_are_ignored(self, ctx: WorkerContext) -> None:
        with ctx.session_factory() as db:
            _session(db, session_date=WEEK - datetime.timedelta(days=1))
            _session(db, session_date=WEEK + datetime.timedelta(days=7))
            db.commit()
        summary = run_weekly_drift_monitor(ctx, WEEK, canary_observed=canary_expectations())
        assert summary.sessions == 0
        assert summary.paired_balls == 0
        with ctx.session_factory() as db:
            # an empty week still reports the broken sampling protocol
            assert [a.code for a in _alerts(db)] == ["drift_sample_short"]

    def test_loop_closes_next_run_compares_the_tagged_sample(self, ctx: WorkerContext) -> None:
        """The runbook loop: week N posts the sample, the coach tags it, week
        N+1's run compares exactly those tags and fires on disagreement."""
        with ctx.session_factory() as db:
            session = _session(db)  # a week-N session
            for ball_no in range(1, 13):
                _event(db, session, ball_no)
                _estimate(db, session, ball_no, line=Line.LEG)  # auto says leg
            session_id = session.id
            db.commit()
        week_n = run_weekly_drift_monitor(ctx, WEEK, canary_observed=canary_expectations())
        assert len(week_n.sampled) == WEEKLY_SAMPLE_SIZE

        # The coach tags the sampled balls during week N+1 (sees off, not leg).
        with ctx.session_factory() as db:
            tagged_session = db.get(Session, session_id)
            assert tagged_session is not None
            for sampled_session_id, ball_no in week_n.sampled:
                assert sampled_session_id == str(session_id)
                _tag(db, tagged_session, ball_no, line=Line.OFF)
            db.commit()

        next_week = WEEK + datetime.timedelta(days=7)
        week_n1 = run_weekly_drift_monitor(ctx, next_week, canary_observed=canary_expectations())
        assert week_n1.compared_week_start == WEEK.isoformat()
        assert week_n1.paired_balls == WEEKLY_SAMPLE_SIZE
        with ctx.session_factory() as db:
            drift = _alerts(db, "drift_agreement")
            assert len(drift) == 1
            alert = drift[0]
            assert alert.detail["field"] == "line"
            assert alert.detail["agreement_pct"] == 0.0
            assert alert.detail["compared"] == WEEKLY_SAMPLE_SIZE
            assert alert.detail["week_start"] == next_week.isoformat()
            assert alert.detail["compared_week_start"] == WEEK.isoformat()
            assert alert.detail["dedupe_key"] == f"drift_agreement:{next_week.isoformat()}:line"
            # Week N+1's compared sample met protocol: only week N's bootstrap
            # run (empty prior week) reported a shortfall.
            shortfalls = _alerts(db, "drift_sample_short")
            assert [a.detail["dedupe_key"] for a in shortfalls] == [
                f"drift_sample_short:{WEEK.isoformat()}"
            ]

    def test_broken_derivation_fires_missing_alarm_with_the_error(self, ctx: WorkerContext) -> None:
        """Silence is a failure, never a pass: a GENUINELY broken canary
        re-derivation persists ``drift_canary_missing`` carrying the error —
        idempotently — and never crashes the rest of the weekly monitor."""
        summary = run_weekly_drift_monitor(ctx, WEEK, canary_deriver=_boom_deriver)
        assert summary.week_start == WEEK.isoformat()  # the monitor still completed
        with ctx.session_factory() as db:
            missing = _alerts(db, "drift_canary_missing")
            assert len(missing) == 1
            alert = missing[0]
            assert alert.severity == "critical"
            assert alert.audience is AlertAudience.DEVELOPER
            assert alert.detail["dedupe_key"] == f"drift_canary_missing:{WEEK.isoformat()}"
            assert alert.detail["derivation_error"] == "RuntimeError: canary pipeline exploded"
        # Re-run of the same week: no new row.
        run_weekly_drift_monitor(ctx, WEEK, canary_deriver=_boom_deriver)
        with ctx.session_factory() as db:
            assert len(_alerts(db, "drift_canary_missing")) == 1

    def test_empty_derivation_fires_missing_alarm_without_error_detail(
        self, ctx: WorkerContext
    ) -> None:
        """A derivation that runs but produces nothing comparable is missing too."""
        run_weekly_drift_monitor(ctx, WEEK, canary_deriver=lambda _ctx: {})
        with ctx.session_factory() as db:
            missing = _alerts(db, "drift_canary_missing")
            assert len(missing) == 1
            assert "derivation_error" not in missing[0].detail

    def test_canary_observations_are_checked_when_supplied(self, ctx: WorkerContext) -> None:
        observed = canary_expectations()
        observed[1] = {**observed[1], "line": "x", "length": "x", "shot": "x"}
        observed[2] = {**observed[2], "line": "x", "length": "x", "shot": "x"}
        run_weekly_drift_monitor(ctx, WEEK, canary_observed=observed)
        with ctx.session_factory() as db:
            canary = _alerts(db, "drift_canary")
            assert len(canary) == 1
            assert canary[0].severity == "critical"
            assert canary[0].audience is AlertAudience.DEVELOPER

    def test_healthy_canary_writes_nothing(self, ctx: WorkerContext) -> None:
        run_weekly_drift_monitor(ctx, WEEK, canary_observed=canary_expectations())
        with ctx.session_factory() as db:
            assert _alerts(db, "drift_canary") == []
            assert _alerts(db, "drift_canary_missing") == []

    def test_rerun_is_idempotent(self, ctx: WorkerContext) -> None:
        with ctx.session_factory() as db:
            compared = _session(db, session_date=PRIOR_WEEK)  # last week's tagged sample
            for ball_no in range(1, 8):
                _event(db, compared, ball_no)
                _tag(db, compared, ball_no, line=Line.OFF)
                _estimate(db, compared, ball_no, line=Line.LEG)
            session = _session(db)  # the week just ended: low quality, untagged
            for ball_no in range(1, 4):
                _event(db, session, ball_no)  # untagged: weekly sample candidates
            db.commit()
        first = run_weekly_drift_monitor(ctx, WEEK)
        second = run_weekly_drift_monitor(ctx, WEEK)
        assert first.alerts_written > 0
        assert second.alerts_written == 0
        assert second.alerts_skipped == first.alerts_written
        with ctx.session_factory() as db:
            all_alerts = _alerts(db)
            assert len(all_alerts) == first.alerts_written
            codes = sorted(a.code for a in all_alerts)
            # The default canary re-derivation is healthy: NO canary alert —
            # the Phase-6 retirement of the standing drift_canary_missing.
            assert codes == [
                "data_quality_low",
                "drift_agreement",
                "drift_sample_short",
                "ground_truth_sample",
            ]

    def test_summary_shape(self, ctx: WorkerContext) -> None:
        summary = run_weekly_drift_monitor(ctx, WEEK)
        assert summary == DriftMonitorSummary(
            week_start=WEEK.isoformat(),
            compared_week_start=PRIOR_WEEK.isoformat(),
            sessions=0,  # the canary session (year 2000) never enters the week
            paired_balls=0,
            alerts_written=1,  # empty week: shortfall only — the canary is healthy
            alerts_skipped=0,
            sampled=(),
        )

    def test_custom_loaders_are_injectable(self, ctx: WorkerContext) -> None:
        """Richer auto sources plug in without touching the job (US-L4 seam)."""
        with ctx.session_factory() as db:
            session = _session(db, session_date=PRIOR_WEEK)
            for ball_no in range(1, 13):
                _event(db, session, ball_no)
                _tag(db, session, ball_no)
            db.commit()

        def all_fields(db: OrmSession, session_id: uuid.UUID) -> dict[int, dict[str, object]]:
            rows = db.scalars(select(BallTag).where(BallTag.session_id == session_id))
            return {
                tag.ball_no: {
                    "line": tag.line.value,
                    "length": tag.length.value,
                    "shot": "pull",  # a wrong auto shot classifier
                    "footwork": tag.footwork.value,
                    "contact": tag.contact.value,
                    "outcome": tag.outcome.value,
                    "control": tag.control,
                }
                for tag in rows
            }

        run_weekly_drift_monitor(ctx, WEEK, auto_fields=all_fields)
        with ctx.session_factory() as db:
            drift = _alerts(db, "drift_agreement")
            assert [a.detail["field"] for a in drift] == ["shot"]


class TestCanaryRederivation:
    """Phase-6 debt retirement: the frozen canary re-derived through the
    production pipeline path every weekly run (US-L4)."""

    def test_band_center_stays_inside_every_band_shape(self) -> None:
        assert dm._band_center(2.0, 5.0) == 3.5  # finite: midpoint
        assert dm._band_center(-math.inf, -0.1143) == -0.6143  # open low edge
        assert dm._band_center(8.0, math.inf) == 8.5  # open high edge

    def test_derivation_reproduces_the_frozen_expectations(self, ctx: WorkerContext) -> None:
        """The real chain — events adapter segmentation, bounce estimator,
        homography, zone classifier, auto-fields loader — reproduces the
        frozen canary's line/length exactly (agreement 1.0 > the 0.98 bound)."""
        observed = derive_canary_observed(ctx)
        expected = canary_expectations()
        assert set(observed) == set(expected)  # all 30 balls re-derived
        for ball_no, fields in expected.items():
            assert observed[ball_no]["line"] == fields["line"], ball_no
            assert observed[ball_no]["length"] == fields["length"], ball_no

    def test_weekly_rederivation_is_idempotent_and_audited(self, ctx: WorkerContext) -> None:
        """Run N+1 clears run N's machine rows through the audited re-derive
        cascade and converges on identical observations."""
        first = derive_canary_observed(ctx)
        second = derive_canary_observed(ctx)
        assert first == second
        with ctx.session_factory() as db:
            audits = list(db.scalars(select(AuditLog).where(AuditLog.actor == dm.CANARY_ACTOR)))
            assert len(audits) == 2  # one cascade per derivation, on the record
            estimates = list(
                db.scalars(
                    select(BounceEstimate).where(BounceEstimate.session_id == CANARY_SESSION_ID)
                )
            )
            assert len(estimates) == len(canary_expectations())

    def test_canary_session_is_pinned_and_reused(self, ctx: WorkerContext) -> None:
        assert ensure_canary_session(ctx) == CANARY_SESSION_ID
        with ctx.session_factory() as db:
            session = db.get(Session, CANARY_SESSION_ID)
            assert session is not None
            calibration_id = session.calibration_id
            assert calibration_id is not None
        assert ensure_canary_session(ctx) == CANARY_SESSION_ID  # converges, no dupes
        with ctx.session_factory() as db:
            session = db.get(Session, CANARY_SESSION_ID)
            assert session is not None
            assert session.calibration_id == calibration_id
            videos = list(db.scalars(select(Video).where(Video.session_id == CANARY_SESSION_ID)))
            assert len(videos) == 1  # the frozen probe input was re-pinned, not duplicated

    def test_balls_outside_the_frozen_set_get_no_synthetic_track(self, ctx: WorkerContext) -> None:
        """A segmenter drift that numbers extra balls must surface as canary
        evidence gaps, never crash the seeding."""
        ensure_canary_session(ctx)
        derive_canary_observed(ctx)
        with ctx.session_factory() as db:
            db.add(
                BallEvent(
                    session_id=CANARY_SESSION_ID,
                    ball_no=99,
                    start_ms=200_000,
                    release_ms=200_100,
                    end_ms=201_000,
                    confidence=0.9,
                    source=EventSource.MANUAL,
                    valid=True,
                )
            )
            db.commit()
        assert dm._seed_canary_tracks(ctx, CANARY_SESSION_ID) == len(canary_expectations())

    def test_skewed_derivation_trips_the_canary_alarm(
        self, ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MV honesty: a pipeline that re-derives the frozen input into the
        wrong zones must fire the critical drift_canary alarm end to end."""
        healthy = dm.canary_zone_targets()
        skewed = dict.fromkeys(healthy, (1.0, 0.9))  # everything yorker / outside-off

        monkeypatch.setattr(dm, "canary_zone_targets", lambda: skewed)
        run_weekly_drift_monitor(ctx, WEEK)
        with ctx.session_factory() as db:
            canary = _alerts(db, "drift_canary")
            assert len(canary) == 1
            assert canary[0].severity == "critical"
            assert canary[0].detail["agreement_pct"] < 0.98
            assert _alerts(db, "drift_canary_missing") == []

    def test_explicit_observations_bypass_the_deriver(self, ctx: WorkerContext) -> None:
        run_weekly_drift_monitor(
            ctx, WEEK, canary_observed=canary_expectations(), canary_deriver=_boom_deriver
        )
        with ctx.session_factory() as db:
            assert _alerts(db, "drift_canary") == []
            assert _alerts(db, "drift_canary_missing") == []


#: A realistic fitted C1 homography, distinct from the frozen canary matrix.
_REAL_C1_CALIBRATION = PlaneCalibration(
    matrix=((0.02, 0.0, 0.0), (0.0, 0.01, -3.0), (0.0, 0.0, 1.0)),
    rms_px=0.4,
    rms_m=0.01,
    n_landmarks=8,
)


def _seed_real_c1_rig(db: OrmSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A real deployment: C1 registered (era 1, active) with a fitted extrinsic
    calibration older than the canary, plus one real uncalibrated session."""
    db.add(
        CameraConfig(
            camera_id="C1",
            era_no=1,
            active=True,
            position_label="side-on",
            xyz_offset_m={"x": 0.0, "y": 0.0, "z": 0.0},
            height_m=1.2,
            fps=30,
            resolution="1920x1080",
        )
    )
    real_calibration = Calibration(
        camera_id="C1",
        era_no=1,
        kind=CalibrationKind.EXTRINSIC,
        params=to_params(_REAL_C1_CALIBRATION),
        valid=True,
        created_at=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
    )
    db.add(real_calibration)
    session = _session(db, session_date=datetime.date(2026, 7, 8))
    db.commit()
    return real_calibration.id, session.id


def _racing_factory(engine: Engine, rival: WorkerContext) -> sessionmaker[OrmSession]:
    """A session factory whose FIRST write is beaten by ``rival``'s full
    ``ensure_canary_session`` — the check-then-insert race window on the
    deterministic canary ids (scheduled run vs manual smoke-test)."""

    class RacingSession(OrmSession):
        _armed = True

        def flush(self, objects: Any = None) -> None:
            cls = type(self)
            if cls._armed and self.new:
                cls._armed = False
                ensure_canary_session(rival)  # the rival wins the insert race
            super().flush(objects)

    return sessionmaker(bind=engine, class_=RacingSession, expire_on_commit=False)


class TestCanaryContainment:
    """The synthetic canary must never leak off its own session: hidden from
    kid-visible surfaces via guest containment (US-L3), skipped by the rollup
    sweep (US-G5), and structurally unreachable from every real-camera
    calibration lookup (US-C3/US-L4)."""

    @pytest.fixture
    def api_ctx(self, tmp_path: Path) -> tuple[WorkerContext, TestClient]:
        """WorkerContext and the real API app sharing one SQLite engine."""
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        create_all(engine)
        ctx = WorkerContext(
            session_factory=make_session_factory(engine),
            store=FsObjectStore(tmp_path / "storage"),
        )
        return ctx, TestClient(make_test_app(tmp_path, engine))

    def test_canary_player_is_guest_and_repinned(self, ctx: WorkerContext) -> None:
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            player = db.get(Player, dm.CANARY_PLAYER_ID)
            assert player is not None
            assert player.is_guest is True
            player.is_guest = False  # DB drift must never un-contain the canary
            db.commit()
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            player = db.get(Player, dm.CANARY_PLAYER_ID)
            assert player is not None
            assert player.is_guest is True

    def test_player_role_surfaces_hide_the_canary(
        self, api_ctx: tuple[WorkerContext, TestClient]
    ) -> None:
        """US-L3: the kid's roster and session list never show the canary."""
        ctx, client = api_ctx
        ensure_canary_session(ctx)
        roster = client.get("/players", headers=auth(PLAYER_TOKEN))
        assert roster.status_code == 200
        assert roster.json() == []
        sessions = client.get("/sessions", headers=auth(PLAYER_TOKEN))
        assert sessions.status_code == 200
        assert sessions.json()["total"] == 0
        # Parent/coach keep guest visibility (US-L3 by design): the canary
        # shows there exactly like any guest, flagged as one.
        parent_roster = client.get("/players", headers=auth(PARENT_TOKEN))
        assert [(p["name"], p["is_guest"]) for p in parent_roster.json()] == [
            ("Drift Canary", True)
        ]

    def test_rollup_sweep_skips_the_canary(self, ctx: WorkerContext) -> None:
        """US-G5: rollup_all_players writes ZERO reports for the canary."""
        with ctx.session_factory() as db:
            real = Player(name="Real Kid", birthdate=datetime.date(2016, 1, 1))
            db.add(real)
            db.commit()
            real_id = real.id
        ensure_canary_session(ctx)
        sweep = rollup_all_players(ctx, as_of=datetime.date(2026, 7, 10))
        assert [summary.player_id for summary in sweep.summaries] == [str(real_id)]
        with ctx.session_factory() as db:
            canary_reports = list(
                db.scalars(select(Report).where(Report.player_id == dm.CANARY_PLAYER_ID))
            )
            assert canary_reports == []

    def test_reuse_latest_for_real_camera_never_resolves_the_canary(
        self, api_ctx: tuple[WorkerContext, TestClient]
    ) -> None:
        """US-C3: the canary calibration is newer, but era-scoped reuse for a
        real camera must keep resolving the real fitted record."""
        ctx, client = api_ctx
        with ctx.session_factory() as db:
            real_calibration_id, real_session_id = _seed_real_c1_rig(db)
        ensure_canary_session(ctx)  # the canary calibration row is now the newest
        response = client.put(
            f"/sessions/{real_session_id}/calibration",
            json={"reuse_latest_for_camera": "C1"},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 200
        assert response.json()["id"] == str(real_calibration_id)

    def test_worker_era_fallback_never_resolves_the_canary(self, ctx: WorkerContext) -> None:
        """US-F4: a real session's era-fallback mapping must never pick the
        canary homography, even when the canary row is the newest."""
        with ctx.session_factory() as db:
            _, real_session_id = _seed_real_c1_rig(db)
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            real_session = db.get(Session, real_session_id)
            assert real_session is not None
            mapping = camera_mapping(db, real_session, "C1")
            assert mapping is not None
            assert mapping.homography.matrix == _REAL_C1_CALIBRATION.matrix

    def test_canary_calibration_is_repinned_every_run(self, ctx: WorkerContext) -> None:
        """US-L4: DB drift (invalidation, edits) never re-targets the canary —
        the frozen calibration is restored on the next run."""
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            session = db.get(Session, CANARY_SESSION_ID)
            assert session is not None
            calibration_id = session.calibration_id
            assert calibration_id is not None
            record = db.get(Calibration, calibration_id)
            assert record is not None
            record.valid = False  # e.g. the US-C4 invalidate runbook hit the row
            record.camera_id = "C1"
            record.era_no = 7
            record.params = {"drifted": True}
            db.commit()
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            record = db.get(Calibration, calibration_id)
            assert record is not None
            assert record.valid is True
            assert record.camera_id == dm.CANARY_CAMERA
            assert record.era_no == 1
            assert record.kind is CalibrationKind.EXTRINSIC
            assert record.params == to_params(dm._CANARY_CALIBRATION)

    def test_dangling_calibration_link_is_repaired(self, ctx: WorkerContext) -> None:
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            session = db.get(Session, CANARY_SESSION_ID)
            assert session is not None
            old_id = session.calibration_id
            record = db.get(Calibration, old_id)
            assert record is not None
            db.delete(record)  # the row vanished; the link dangles
            db.commit()
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            session = db.get(Session, CANARY_SESSION_ID)
            assert session is not None
            assert session.calibration_id is not None
            assert session.calibration_id != old_id
            record = db.get(Calibration, session.calibration_id)
            assert record is not None
            assert record.valid is True
            assert record.camera_id == dm.CANARY_CAMERA

    def test_foreign_video_rows_are_pruned_each_run(self, ctx: WorkerContext) -> None:
        """The canary's evidence is exactly one synthetic video: a stray row
        under a real camera id could hijack the reference-camera choice."""
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            db.add(
                Video(
                    session_id=CANARY_SESSION_ID,
                    camera_id="C2",
                    object_key="stray/upload.mp4",
                    filename="stray.mp4",
                    checksum_sha256="d" * 64,
                    size_bytes=1,
                    status=VideoStatus.PROBED,
                )
            )
            db.commit()
        ensure_canary_session(ctx)
        with ctx.session_factory() as db:
            videos = list(db.scalars(select(Video).where(Video.session_id == CANARY_SESSION_ID)))
            assert [video.camera_id for video in videos] == [dm.CANARY_CAMERA]

    def test_first_materialization_race_converges(self, tmp_path: Path) -> None:
        """Two runs racing the first materialization on a fresh DB collide on
        the deterministic ids; the loser must converge on the winner's rows —
        never crash into a false drift_canary_missing page."""
        engine = create_engine(f"sqlite:///{tmp_path / 'race.sqlite'}")
        create_all(engine)
        store = FsObjectStore(tmp_path / "racing-store")
        rival = WorkerContext(session_factory=make_session_factory(engine), store=store)
        racing = WorkerContext(session_factory=_racing_factory(engine, rival), store=store)
        assert ensure_canary_session(racing) == CANARY_SESSION_ID
        with rival.session_factory() as db:
            players = list(db.scalars(select(Player)))
            assert [player.id for player in players] == [dm.CANARY_PLAYER_ID]
            sessions = list(db.scalars(select(Session)))
            assert [session.id for session in sessions] == [CANARY_SESSION_ID]
            videos = list(db.scalars(select(Video).where(Video.session_id == CANARY_SESSION_ID)))
            assert len(videos) == 1
