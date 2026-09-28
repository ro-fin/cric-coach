"""Re-derive cascade unit tests (Phase-3 debt / Phase-5 plan scope decision).

In-memory SQLite exercises every branch: the confirm-gated deletion of manual
rows, the block-before-any-write refusal, machine-vs-manual metric counting,
the audit row, and the advisory-lock busy path. The FK ordering the cascade
relies on is re-proven against real PostgreSQL in the integration suite.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from cricai_data.db import create_all, session_scope
from cricai_data.enums import (
    BowlerSource,
    Contact,
    EvidenceVerdict,
    Footwork,
    Length,
    Line,
    MetricPhase,
    Outcome,
    ReportKind,
    SessionType,
    Shot,
)
from cricai_data.models import (
    AuditLog,
    BallEvent,
    BallMetrics,
    BallTag,
    BallTrack,
    BounceEstimate,
    BounceMark,
    Clip,
    EventCorrection,
    EvidenceVerdictRecord,
    Finding,
    PipelineRun,
    PipelineStage,
    Player,
    PoseTrack,
    ReferenceBall,
    Report,
    ReportLLMAudit,
    TagAudit,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.rederive import (
    REDERIVE_AUDIT_ACTION,
    RederiveBlockedError,
    SessionBusyError,
    rederive_session,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

MACHINE_METRICS = {"control": {"value": 0.7, "source": "auto"}, "raw_count": 5}
MANUAL_METRICS = {
    "control": {"value": 0.7, "source": "auto"},  # machine dict entry
    "speed_kph": {"value": 82.0, "source": "manual"},  # manual dict entry
    "raw_count": 5,  # non-dict entry -> machine
}


@pytest.fixture
def ctx(tmp_path: Any) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        store=FsObjectStore(tmp_path / "store"),
    )


def _machine_rows(db: Any, session_id: uuid.UUID) -> None:
    """The regenerable dependents a re-derive always deletes."""
    run = PipelineRun(session_id=session_id)
    db.add(run)
    db.flush()
    db.add(PipelineStage(run_id=run.id, stage="events"))
    finding = Finding(
        session_id=session_id,
        run_id=run.id,
        agent="batting",
        kind="rule_hit",
        severity="major",
        metric="control_pct",
        n=10,
        confidence=0.9,
    )
    db.add(finding)
    db.flush()
    db.add(
        EvidenceVerdictRecord(finding_id=finding.id, verdict=EvidenceVerdict.CONFIRMS, actor="c")
    )
    report = Report(
        player_id=db.get(SessionRow, session_id).player_id,
        session_id=session_id,
        kind=ReportKind.DAILY,
        period_start=date(2026, 7, 7),
        period_end=date(2026, 7, 7),
        body={"kind": "daily"},
    )
    db.add(report)
    db.flush()
    db.add(
        ReportLLMAudit(
            report_id=report.id, prompt_key="daily", request={}, response={}, accepted=True
        )
    )
    db.add(Clip(session_id=session_id, ball_no=1, camera_id="C1", start_ms=0, end_ms=100))
    db.add(
        PoseTrack(
            session_id=session_id,
            ball_no=1,
            camera_id="C1",
            model_name="m",
            model_version="1",
            landmarks_key="k",
            frame_count=10,
            availability=1.0,
            subject_confidence=1.0,
        )
    )
    db.add(
        BallTrack(
            session_id=session_id,
            ball_no=1,
            camera_id="C1",
            tracker_version="1",
            points_key="k",
            coverage=1.0,
            confidence=1.0,
        )
    )
    db.add(
        BounceEstimate(
            session_id=session_id,
            ball_no=1,
            pitch_x=0.0,
            pitch_y=0.0,
            confidence=1.0,
            tracker_version="1",
        )
    )


def _manual_rows(db: Any, session_id: uuid.UUID, event_id: uuid.UUID) -> None:
    """Human-authored rows re-detection renumbering would corrupt."""
    tag = BallTag(
        session_id=session_id,
        ball_no=1,
        line=Line.OUTSIDE_OFF,
        length=Length.GOOD,
        shot=Shot.DRIVE,
        footwork=Footwork.FRONT,
        contact=Contact.MIDDLE,
        outcome=Outcome.CONTROLLED_GROUND_SHOT,
        control=True,
        created_by="parent",
    )
    db.add(tag)
    db.flush()
    db.add(TagAudit(tag_id=tag.id, actor="parent", field="shot"))
    db.add(
        BounceMark(
            session_id=session_id,
            ball_no=1,
            camera_id="C3",
            frame_no=5,
            px_x=1.0,
            px_y=1.0,
            pitch_x=0.0,
            pitch_y=0.0,
        )
    )
    db.add(ReferenceBall(session_id=session_id, ball_no=1, label="best drive", marked_by="coach"))
    db.add(EventCorrection(event_id=event_id, action="adjust", actor="parent"))


def seed(
    ctx: WorkerContext,
    *,
    manual: bool = True,
    metrics: dict[str, Any] | None = None,
) -> uuid.UUID:
    with session_scope(ctx.session_factory) as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.flush()
        event = BallEvent(
            session_id=session.id, ball_no=1, start_ms=0, release_ms=10, end_ms=100, confidence=0.9
        )
        db.add(event)
        db.flush()
        _machine_rows(db, session.id)
        if metrics is not None:
            db.add(
                BallMetrics(
                    session_id=session.id, ball_no=1, phase=MetricPhase.CONTACT, metrics=metrics
                )
            )
        if manual:
            _manual_rows(db, session.id, event.id)
        return session.id


def count(ctx: WorkerContext, model: Any, session_id: uuid.UUID) -> int:
    with ctx.session_factory() as db:
        return int(
            db.scalar(select(func.count(model.id)).where(model.session_id == session_id)) or 0
        )


# ------------------------------------------------------------ machine-only


def test_machine_only_session_needs_no_confirmation(ctx: WorkerContext) -> None:
    session_id = seed(ctx, manual=False, metrics=MACHINE_METRICS)
    summary = rederive_session(ctx, session_id, actor="parent")
    assert summary.manual_invalidation is False
    assert summary.findings_deleted == 1
    assert summary.evidence_verdicts_deleted == 1
    assert summary.reports_deleted == 1
    assert summary.report_llm_audits_deleted == 1
    assert summary.pipeline_runs_deleted == 1
    assert summary.pipeline_stages_deleted == 1
    assert summary.clips_deleted == 1
    assert summary.pose_tracks_deleted == 1
    assert summary.ball_tracks_deleted == 1
    assert summary.bounce_estimates_deleted == 1
    assert summary.ball_metrics_rows_deleted == 1
    assert summary.machine_metric_values_removed == 2  # {control dict, raw non-dict}
    assert summary.manual_metric_values_removed == 0
    assert summary.ball_tags_deleted == 0
    # the machine dependents are gone; the source-of-truth events remain
    assert count(ctx, Finding, session_id) == 0
    assert count(ctx, Clip, session_id) == 0
    assert count(ctx, BallEvent, session_id) == 1


# ------------------------------------------------------------- block path


def test_unconfirmed_manual_rows_block_before_any_write(ctx: WorkerContext) -> None:
    session_id = seed(ctx, manual=True, metrics=MANUAL_METRICS)
    with pytest.raises(RederiveBlockedError) as excinfo:
        rederive_session(ctx, session_id, actor="parent")
    blockers = excinfo.value.blockers
    assert blockers == {
        "ball_tags": 1,
        "bounce_marks": 1,
        "reference_balls": 1,
        "event_corrections": 1,
        "manual_ball_metric_values": 1,
    }
    assert excinfo.value.session_id == session_id
    assert "nothing was modified" in str(excinfo.value)
    # nothing deleted — the whole session survives the refusal
    assert count(ctx, Finding, session_id) == 1
    assert count(ctx, BallTag, session_id) == 1
    assert count(ctx, Clip, session_id) == 1
    with ctx.session_factory() as db:
        assert db.scalar(select(func.count(AuditLog.id))) == 0


def test_partial_blockers_list_only_present_categories(ctx: WorkerContext) -> None:
    # Only ball tags are manual here; the other categories must be absent, not zero.
    session_id = seed(ctx, manual=False, metrics=MACHINE_METRICS)
    with session_scope(ctx.session_factory) as db:
        db.add(
            BallTag(
                session_id=session_id,
                ball_no=1,
                line=Line.OFF,
                length=Length.FULL,
                shot=Shot.DEFEND,
                footwork=Footwork.BACK,
                contact=Contact.EDGE,
                outcome=Outcome.UNCONTROLLED,
                control=False,
                created_by="parent",
            )
        )
    with pytest.raises(RederiveBlockedError) as excinfo:
        rederive_session(ctx, session_id, actor="parent")
    assert excinfo.value.blockers == {"ball_tags": 1}


# ---------------------------------------------------- confirmed invalidation


def test_confirmed_cascade_deletes_manual_rows_and_audits(ctx: WorkerContext) -> None:
    session_id = seed(ctx, manual=True, metrics=MANUAL_METRICS)
    summary = rederive_session(ctx, session_id, actor="parent", confirm_manual_invalidation=True)
    assert summary.manual_invalidation is True
    assert summary.ball_tags_deleted == 1
    assert summary.tag_audits_deleted == 1
    assert summary.bounce_marks_deleted == 1
    assert summary.reference_balls_deleted == 1
    assert summary.event_corrections_deleted == 1
    assert summary.machine_metric_values_removed == 2
    assert summary.manual_metric_values_removed == 1
    # everything human + machine is gone; only the events remain
    assert count(ctx, BallTag, session_id) == 0
    assert count(ctx, BounceMark, session_id) == 0
    assert count(ctx, ReferenceBall, session_id) == 0
    assert count(ctx, BallEvent, session_id) == 1
    # the deletion is audited with the full counts
    with ctx.session_factory() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == REDERIVE_AUDIT_ACTION))
        assert audit is not None
        assert audit.entity == "session"
        assert audit.entity_id == str(session_id)
        assert audit.detail == summary.counts()
        assert audit.detail["manual_invalidation"] is True


# ------------------------------------------------------------- lock / errors


class _DenyLock:
    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        raise AssertionError("release must not run when acquire failed")


def test_busy_session_is_skipped(ctx: WorkerContext) -> None:
    session_id = seed(ctx, manual=False, metrics=MACHINE_METRICS)
    with pytest.raises(SessionBusyError) as excinfo:
        rederive_session(ctx, session_id, actor="parent", lock=_DenyLock())
    assert excinfo.value.session_id == session_id
    assert "locked by a running pipeline" in str(excinfo.value)
    # a busy skip touches nothing
    assert count(ctx, Finding, session_id) == 1


def test_unknown_session_raises_lookup_error(ctx: WorkerContext) -> None:
    with pytest.raises(LookupError, match="not found"):
        rederive_session(ctx, uuid.uuid4(), actor="parent")
