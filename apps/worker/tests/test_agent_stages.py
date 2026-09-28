"""US-J1/J2/J3/H5 agent-stage adapters: findings persistence, planner, safety supremacy, report.

SQLite-backed unit tests (the ``test_pipeline.py`` pattern). Real coaching
agents run over seeded rows; the media stages of the DAG are faked with
``ok_stage`` so the end-to-end tests exercise the agent chain, not the vision
pipeline. Covers the two finding shapes integrating (``text_data`` folded into
``payload`` and read back by ``generate_report``'s loader), analysis
idempotency, the H1 allowance / pain / workload hard blocks, safety-supremacy
rejection of a tampered plan (direct and through the DAG cascade), and the
report threading this run's persisted safety verdict in verbatim.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from cricai_coaching import analysis_agent, contracts, planner_agent, progress_agent, safety_agent
from cricai_coaching.safety_agent import SafetySupremacyError
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_data.db import create_all, make_session_factory, session_scope
from cricai_data.enums import (
    AlertAudience,
    BlockIntent,
    BowlerSource,
    ClipStatus,
    Contact,
    DeliveryIntensity,
    EvidenceVerdict,
    Footwork,
    Length,
    Line,
    Outcome,
    ReportStatus,
    SessionType,
    Shot,
    StageStatus,
)
from cricai_data.models import (
    Alert,
    AuditLog,
    BallEvent,
    BallTag,
    BowlingLedgerEntry,
    Clip,
    CoachingRule,
    Drill,
    DrillPlan,
    EvidenceVerdictRecord,
    Finding,
    MetricBaseline,
    PipelineRun,
    PipelineStage,
    Player,
    Report,
    SafetyConfig,
    WellnessCheckin,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.agent_stages import (
    ANALYSIS_PARTIAL_COVERAGE_NOTE,
    ANALYSIS_UNAVAILABLE_BANNER,
    SAFETY_SUPREMACY_ALERT_CODE,
    AnalysisBlockedError,
    ReportSafetyError,
    _drill_report_text,
    _drill_resolver,
    _evaluate_safety,
    _persist_supremacy_alert,
    _run_trends,
    _running_safety_verdict,
    _upsert_drill_plan,
    run_analysis,
    run_planner,
    run_progress,
    run_report,
    run_safety,
)
from cricai_worker.context import WorkerContext
from cricai_worker.generate_report import finding_to_contract, generate_report
from cricai_worker.pipeline import STAGES, RunPolicy, run_pipeline
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

SESSION_DATE = date(2026, 7, 7)
PLAN_DATE = SESSION_DATE + timedelta(days=1)

#: Rule-authored strings (kid-safe) that must survive the fold-and-read-back.
AUTHORED_CORRECTION = "Keep your head still on short balls at leg stump."
AUTHORED_DRILL = "Practice back-foot defence to short leg-stump balls."

RULE_DEFINITION: dict[str, Any] = {
    "metric": "control",
    "op": "eq",
    "value": False,
    "condition": {"line": ["leg"]},
    "min_n": 10,
    "severity": "minor",
    "text_data": {"correction": AUTHORED_CORRECTION, "drill": AUTHORED_DRILL},
}

#: Media stages faked so the agent chain (analysis..report) runs end-to-end.
MEDIA_STAGES: tuple[str, ...] = ("events", "clips", "pose", "detect_track", "metrics")


# --------------------------------------------------------------- fake stages


def _path(name: str) -> str:
    return f"{__name__}:{name}"


def ok_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    return {"ok": True}


def fail_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Test double: a stage that always raises (drives the degraded-but-honest path)."""
    raise RuntimeError("stage engine crashed")


def tamper_planner(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Test double: persist a plan that schedules bowling with an empty safety
    block, so the real safety stage's validator must reject it (US-H5)."""
    with session_scope(ctx.session_factory) as db:
        session = db.get(SessionRow, session_id)
        assert session is not None
        db.add(
            DrillPlan(
                player_id=session.player_id,
                plan_date=session.session_date + timedelta(days=1),
                blocks=[
                    _fun_block(),
                    {
                        "intent": "bowling",
                        "balls": 30,
                        "drill_id": None,
                        "machine_settings": {},
                        "success_metric": "landing_accuracy_pct",
                        "finding_id": "maintenance",
                    },
                ],
                finding_ids=[],
                safety={},
                safety_sha256=None,
                created_by="tamper",
            )
        )
    return {"tampered": True}


def _fun_block() -> dict[str, Any]:
    return {
        "intent": "fun",
        "balls": 50,
        "drill_id": None,
        "machine_settings": {},
        "success_metric": "enjoyment",
        "finding_id": None,
    }


def _media_ok_registry(**overrides: str) -> dict[str, str]:
    registry = {stage: _path("ok_stage") for stage in MEDIA_STAGES}
    registry.update(overrides)
    return registry


# --------------------------------------------------------------- db fixtures


@pytest.fixture
def ctx(tmp_path: Any) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )


def _seed_minimal(
    ctx: WorkerContext, *, birthdate: date = date(2014, 11, 20)
) -> tuple[uuid.UUID, uuid.UUID]:
    """A player + session, nothing else — no balls, rules, or drills."""
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=birthdate)
        db.add(player)
        db.flush()
        session = SessionRow(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.commit()
        return session.id, player.id


def _seed_full(
    ctx: WorkerContext,
    *,
    birthdate: date = date(2014, 11, 20),
    pain: bool = False,
    with_clips: bool = False,
    ledger_balls: int = 0,
    with_config: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A batting session with 20 tagged balls, one approved rule, one drill.

    10 controlled (off/good) + 10 uncontrolled (leg/short) balls make the
    leg-control rule fire and produce two zone-contrast probes (a MAJOR
    weakness on leg/short, an INFO strength on off/good).
    """
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=birthdate)
        db.add(player)
        db.flush()
        session = SessionRow(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        for ball_no in range(1, 11):
            db.add(
                BallTag(
                    session_id=session.id,
                    ball_no=ball_no,
                    line=Line.OFF,
                    length=Length.GOOD,
                    shot=Shot.DEFEND,
                    footwork=Footwork.FRONT,
                    contact=Contact.MIDDLE,
                    outcome=Outcome.CONTROLLED_GROUND_SHOT,
                    control=True,
                    created_by="coach",
                )
            )
        for ball_no in range(11, 21):
            db.add(
                BallTag(
                    session_id=session.id,
                    ball_no=ball_no,
                    line=Line.LEG,
                    length=Length.SHORT,
                    shot=Shot.PULL,
                    footwork=Footwork.BACK,
                    contact=Contact.EDGE,
                    outcome=Outcome.EDGED,
                    control=False,
                    created_by="coach",
                )
            )
            if with_clips:
                for camera_id in ("C1", "C2"):
                    db.add(
                        Clip(
                            session_id=session.id,
                            ball_no=ball_no,
                            camera_id=camera_id,
                            object_key=f"clip/{ball_no}/{camera_id}",
                            start_ms=ball_no * 1000,
                            end_ms=ball_no * 1000 + 500,
                            status=ClipStatus.CUT,
                        )
                    )
        db.add(
            CoachingRule(
                rule_key="leg_control",
                version=1,
                author="coach",
                approved_by="coach",
                rationale="Head still against short balls at leg stump.",
                definition=RULE_DEFINITION,
                enabled=True,
            )
        )
        db.add(
            Drill(
                name="Head-still leg defence",
                setup="Back-foot defence to short leg-stump balls.",
                machine_settings={},
                ball_count=30,
                target_metric="control_pct",
                intent=BlockIntent.TECHNICAL,
                author="coach",
                enabled=True,
            )
        )
        if pain:
            db.add(
                WellnessCheckin(
                    player_id=player.id,
                    session_id=session.id,
                    checkin_date=SESSION_DATE,
                    soreness={},
                    pain=True,
                    created_by="player",
                )
            )
        if ledger_balls:
            db.add(
                BowlingLedgerEntry(
                    player_id=player.id,
                    entry_date=SESSION_DATE,
                    balls=ledger_balls,
                    intensity=DeliveryIntensity.PACE_INTENT,
                    source="manual",
                    created_by="coach",
                )
            )
        if with_config:
            db.add(
                SafetyConfig(
                    version=1,
                    config=dict(DEFAULT_SAFETY_CONFIG),
                    approved_by="seed",
                    reason="canonical defaults",
                )
            )
        db.commit()
        return session.id, player.id


def _plan_for(ctx: WorkerContext, player_id: uuid.UUID) -> DrillPlan:
    with ctx.session_factory() as db:
        plan = db.scalar(
            select(DrillPlan).where(
                DrillPlan.player_id == player_id, DrillPlan.plan_date == PLAN_DATE
            )
        )
        assert plan is not None
        return plan


def _add_same_day_session(ctx: WorkerContext, player_id: uuid.UUID) -> uuid.UUID:
    """A second BATTING session for the player on ``SESSION_DATE`` (no tags).

    The daily report merges every session's findings for the date, so this
    sibling shares the (player, DAILY, date) report row with the seeded one.
    """
    with ctx.session_factory() as db:
        session = SessionRow(
            player_id=player_id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.commit()
        return session.id


# ---------------------------------------------------------- missing session


@pytest.mark.parametrize(
    "stage_fn",
    [run_analysis, run_progress, run_planner, run_safety, run_report],
)
def test_stage_raises_lookup_error_for_missing_session(
    ctx: WorkerContext, stage_fn: Callable[[WorkerContext, uuid.UUID], object]
) -> None:
    with pytest.raises(LookupError, match="not found"):
        stage_fn(ctx, uuid.uuid4())


# ------------------------------------------------------------- run_analysis


def test_run_analysis_persists_folds_text_data_and_is_idempotent(ctx: WorkerContext) -> None:
    session_id, _ = _seed_full(ctx)
    first = run_analysis(ctx, session_id)
    assert first["finding_count"] == 3
    assert len(first["finding_ids"]) == 3

    with ctx.session_factory() as db:
        rows = list(db.scalars(select(Finding).where(Finding.session_id == session_id)))
        assert len(rows) == 3
        assert all(row.run_id is None for row in rows)  # no running run -> None run_id
        rule_row = next(row for row in rows if row.rule_key == "leg_control")
        # rule-authored text_data folded into payload; the rule's own payload survives.
        assert rule_row.payload["text_data"]["correction"] == AUTHORED_CORRECTION
        assert rule_row.payload["op"] == "eq"
        # The wire finding_id is kept for traceability; rule findings carry no probe.
        assert rule_row.payload["finding_id"].startswith("ru-")
        assert "probe" not in rule_row.payload
        probe_rows = [row for row in rows if row.rule_key is None]
        assert len(probe_rows) == 2  # zone-contrast probes carry no rule payload
        # Probes keep text_data, the wire id (traceability, finding 38) and the
        # probe origin (contract #2 rule_key|probe survives persist, finding 47).
        assert all(set(row.payload) == {"text_data", "finding_id", "probe"} for row in probe_rows)
        assert all(row.payload["finding_id"].startswith("an-") for row in probe_rows)
        assert all(row.payload["probe"] == row.kind for row in probe_rows)
        first_ids = {str(row.id) for row in rows}

    # Findings are regenerable: a re-run replaces them, never duplicates, and
    # re-mints IDENTICAL ids over unchanged data (stable references, finding 38).
    second = run_analysis(ctx, session_id)
    assert second["finding_count"] == 3
    with ctx.session_factory() as db:
        rows = list(db.scalars(select(Finding).where(Finding.session_id == session_id)))
        assert len(rows) == 3
        assert {str(row.id) for row in rows} == first_ids


def test_report_loader_reads_the_folded_text_data(ctx: WorkerContext) -> None:
    """The two shapes integrate: analysis folds text_data into payload; the
    report worker's own loader reconstructs the contract text_data from it."""
    session_id, _ = _seed_full(ctx, with_clips=True)
    run_analysis(ctx, session_id)
    with ctx.session_factory() as db:
        rule_row = db.scalar(
            select(Finding).where(
                Finding.session_id == session_id, Finding.rule_key == "leg_control"
            )
        )
        assert rule_row is not None
        # Wire dict -> DB row: the fold lands text_data inside payload, the exact
        # place routers/drills.py::_finding_dict reads it (row.payload["text_data"]).
        assert rule_row.payload["text_data"]["correction"] == AUTHORED_CORRECTION
        # ...and generate_report.py's own loader reconstructs the contract text_data.
        contract = finding_to_contract(rule_row)
        assert contract["text_data"]["correction"] == AUTHORED_CORRECTION
    result = generate_report(ctx, session_id)
    with ctx.session_factory() as db:
        report = db.get(Report, result.report_id)
        assert report is not None
        assert {"safety", "claims", "main_correction"} <= set(report.body)


def test_run_analysis_attaches_the_running_run_id(ctx: WorkerContext) -> None:
    session_id, _ = _seed_full(ctx)
    with ctx.session_factory() as db:
        run = PipelineRun(session_id=session_id, status=StageStatus.RUNNING)
        db.add(run)
        db.commit()
        run_id = run.id
    run_analysis(ctx, session_id)
    with ctx.session_factory() as db:
        rows = list(db.scalars(select(Finding).where(Finding.session_id == session_id)))
        assert rows and all(row.run_id == run_id for row in rows)


def test_run_analysis_on_an_empty_session_produces_no_findings(ctx: WorkerContext) -> None:
    session_id, _ = _seed_minimal(ctx)
    assert run_analysis(ctx, session_id) == {"finding_count": 0, "finding_ids": []}
    with ctx.session_factory() as db:
        assert list(db.scalars(select(Finding).where(Finding.session_id == session_id))) == []


def test_run_analysis_blocks_when_a_coach_verdict_references_a_finding(ctx: WorkerContext) -> None:
    """Block-beats-corrupt (US-J1/G6): a plain re-run must not delete a finding a
    coach reviewed — it raises and leaves every row untouched."""
    session_id, _ = _seed_full(ctx)
    run_analysis(ctx, session_id)
    with ctx.session_factory() as db:
        before = sorted(
            str(row.id)
            for row in db.scalars(select(Finding).where(Finding.session_id == session_id))
        )
        db.add(
            EvidenceVerdictRecord(
                finding_id=uuid.UUID(before[0]), verdict=EvidenceVerdict.CONFIRMS, actor="coach"
            )
        )
        db.commit()

    with pytest.raises(AnalysisBlockedError) as excinfo:
        run_analysis(ctx, session_id)
    message = str(excinfo.value)
    assert before[0] in message  # names the reviewed finding
    assert "confirm_manual_invalidation=true" in message  # names the remedy

    with ctx.session_factory() as db:
        after = sorted(
            str(row.id)
            for row in db.scalars(select(Finding).where(Finding.session_id == session_id))
        )
    assert after == before  # nothing deleted or re-created on the block path


# ------------------------------------------------------------- run_progress


def test_run_progress_reads_frozen_baselines(ctx: WorkerContext) -> None:
    session_id, player_id = _seed_minimal(ctx)
    with ctx.session_factory() as db:
        for index, snapshot_date in enumerate(
            [date(2026, 6, 1), date(2026, 6, 15), date(2026, 7, 1)]
        ):
            db.add(
                MetricBaseline(
                    player_id=player_id,
                    metric="control_pct",
                    zone_key="off/good",
                    window="rolling_20",
                    snapshot_date=snapshot_date,
                    value=0.5 + 0.1 * index,
                    n=40,
                    payload={"session_id": str(session_id)},
                )
            )
        db.commit()
    snapshots = run_progress(ctx, session_id)["snapshots"]
    assert len(snapshots) == 1
    assert snapshots[0]["metric"] == "control_pct"
    assert snapshots[0]["qualified"] is True
    assert len(snapshots[0]["points"]) == 3


def test_run_progress_without_baselines_is_empty(ctx: WorkerContext) -> None:
    session_id, _ = _seed_minimal(ctx)
    assert run_progress(ctx, session_id)["snapshots"] == []


# -------------------------------------------------------------- run_planner


def test_run_planner_with_no_findings_or_drills_still_plans(ctx: WorkerContext) -> None:
    session_id, player_id = _seed_minimal(ctx)
    run_analysis(ctx, session_id)  # 0 findings
    out = run_planner(ctx, session_id)
    assert out["safety_active"] is False
    plan = _plan_for(ctx, player_id)
    assert plan.created_by == "planner"
    assert any(block["intent"] == "fun" for block in plan.blocks)
    assert any(block["intent"] == "bowling" for block in plan.blocks)  # age 11 -> allowance 96
    assert all(block["drill_id"] is None for block in plan.blocks)  # no drills -> maintenance


def test_run_planner_zero_bowling_when_no_age_band_applies(ctx: WorkerContext) -> None:
    """An out-of-band age has no ceiling (remaining_balls None) -> allowance 0 -> no bowling."""
    session_id, player_id = _seed_full(ctx, birthdate=date(2005, 1, 1))
    run_analysis(ctx, session_id)
    out = run_planner(ctx, session_id)
    assert out["safety_active"] is False
    assert not any(block["intent"] == "bowling" for block in _plan_for(ctx, player_id).blocks)


# ----------------------------------------------------- end-to-end DAG chains


def test_full_pipeline_threads_the_safety_verdict_into_the_report(ctx: WorkerContext) -> None:
    session_id, player_id = _seed_full(ctx, with_config=True)
    outcome = run_pipeline(ctx, session_id, registry=_media_ok_registry())
    assert outcome.status == "succeeded"
    assert [s.stage for s in outcome.stages] == list(STAGES)

    with ctx.session_factory() as db:
        findings = list(db.scalars(select(Finding).where(Finding.session_id == session_id)))
        assert len(findings) == 3
        assert all(row.run_id == outcome.run_id for row in findings)

        plan = db.scalar(
            select(DrillPlan).where(
                DrillPlan.player_id == player_id, DrillPlan.plan_date == PLAN_DATE
            )
        )
        assert plan is not None and plan.created_by == "planner"
        # One technical block cites the mapped finding; a bowling block fits the allowance.
        assert any(block["drill_id"] is not None for block in plan.blocks)
        assert any(block["intent"] == "bowling" for block in plan.blocks)

        safety_stage = db.scalar(
            select(PipelineStage).where(
                PipelineStage.run_id == outcome.run_id,
                PipelineStage.stage == "safety",
                PipelineStage.status == StageStatus.SUCCEEDED,
            )
        )
        assert safety_stage is not None and safety_stage.output is not None
        verdict = safety_stage.output["payload"]

        report = db.scalar(select(Report).where(Report.session_id == session_id))
        assert report is not None
        # The safety verdict is threaded VERBATIM, with the hash over its text.
        assert report.body["safety"] == verdict
        assert report.safety_sha256 == verdict["sha256"]


def test_pipeline_blocks_bowling_and_report_on_pain(ctx: WorkerContext) -> None:
    session_id, player_id = _seed_full(ctx, pain=True)
    outcome = run_pipeline(ctx, session_id, registry=_media_ok_registry())
    assert outcome.status == "succeeded"

    plan = _plan_for(ctx, player_id)
    assert not any(block["intent"] == "bowling" for block in plan.blocks)  # pain hard-block
    assert plan.safety["active"] is True
    assert "pain_flag" in plan.safety["codes"]


def test_pipeline_blocks_bowling_on_workload_ceiling(ctx: WorkerContext) -> None:
    # 100 pace balls in one day = 16.7 overs >= the age-11 ceiling of 16.
    session_id, player_id = _seed_full(ctx, ledger_balls=100)
    outcome = run_pipeline(ctx, session_id, registry=_media_ok_registry())
    assert outcome.status == "succeeded"

    plan = _plan_for(ctx, player_id)
    assert not any(block["intent"] == "bowling" for block in plan.blocks)
    assert plan.safety["active"] is True
    assert "workload_ceiling" in plan.safety["codes"]


# ----------------------------------------------------------- safety stage


def test_run_safety_without_a_plan_returns_the_verdict(ctx: WorkerContext) -> None:
    session_id, _ = _seed_minimal(ctx)
    verdict = run_safety(ctx, session_id)
    assert verdict["active"] is False
    assert verdict["codes"] == []


def test_run_safety_rejects_a_tampered_plan(ctx: WorkerContext) -> None:
    """A plan hand-edited to schedule bowling while pain is active must fail
    the independent safety validator (US-H5 supremacy)."""
    session_id, player_id = _seed_full(ctx, pain=True)
    run_analysis(ctx, session_id)
    run_planner(ctx, session_id)
    with ctx.session_factory() as db:
        plan = db.scalar(
            select(DrillPlan).where(
                DrillPlan.player_id == player_id, DrillPlan.plan_date == PLAN_DATE
            )
        )
        assert plan is not None
        plan.blocks = [
            *plan.blocks,
            {
                "intent": "bowling",
                "balls": 30,
                "drill_id": None,
                "machine_settings": {},
                "success_metric": "landing_accuracy_pct",
                "finding_id": "maintenance",
            },
        ]
        db.commit()
    with pytest.raises(SafetySupremacyError):
        run_safety(ctx, session_id)


def test_pipeline_safety_violation_skips_the_report(ctx: WorkerContext) -> None:
    """Safety supremacy through the DAG: a violating persisted plan fails the
    safety stage, and report (hard-depending on safety) then skips."""
    session_id, _ = _seed_full(ctx, pain=True)
    registry = _media_ok_registry(planner=_path("tamper_planner"))
    outcome = run_pipeline(ctx, session_id, registry=registry)
    fate = {s.stage: s for s in outcome.stages}
    assert outcome.status == "failed"
    assert fate["safety"].status is StageStatus.FAILED
    assert "SafetySupremacyError" in (fate["safety"].error or "")
    assert fate["report"].status is StageStatus.SKIPPED


# ----------------------------------------------------------- report stage


def test_run_report_standalone_has_no_safety_verdict(ctx: WorkerContext) -> None:
    """Called outside a pipeline run there is no running run, so the report is
    honest about carrying no safety verdict rather than inventing one."""
    session_id, _ = _seed_minimal(ctx)
    out = run_report(ctx, session_id)
    assert out["safety_active"] is False
    with ctx.session_factory() as db:
        assert db.scalar(select(Report).where(Report.session_id == session_id)) is not None


def _valid_verdict(text: str = "") -> dict[str, Any]:
    return {
        "active": bool(text),
        "codes": [],
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def test_running_safety_verdict_none_when_safety_did_not_succeed(ctx: WorkerContext) -> None:
    """Honest degradation: no RUNNING run, or a run whose safety stage never
    SUCCEEDED (skipped/failed upstream), yields None — not a fabricated verdict."""
    no_run, _ = _seed_minimal(ctx)
    no_stage, _ = _seed_minimal(ctx)
    with ctx.session_factory() as db:
        db.add(PipelineRun(session_id=no_stage, status=StageStatus.RUNNING))
        db.commit()
        assert _running_safety_verdict(db, no_run) is None
        assert _running_safety_verdict(db, no_stage) is None


@pytest.mark.parametrize("output", [None, {"payload": "not-a-mapping"}, {"payload": {"active": 1}}])
def test_running_safety_verdict_fails_closed_on_bad_succeeded_payload(
    ctx: WorkerContext, output: Any
) -> None:
    """Fail-closed (US-H5, finding 23): a SUCCEEDED safety stage that produced no
    usable/valid verdict raises rather than shipping a verdict-less report."""
    session_id, _ = _seed_minimal(ctx)
    with ctx.session_factory() as db:
        run = PipelineRun(session_id=session_id, status=StageStatus.RUNNING)
        db.add(run)
        db.flush()
        db.add(
            PipelineStage(
                run_id=run.id,
                stage="safety",
                status=StageStatus.SUCCEEDED,
                attempt=1,
                output=output,
            )
        )
        db.commit()
        with pytest.raises(ReportSafetyError):
            _running_safety_verdict(db, session_id)


def test_running_safety_verdict_returns_a_valid_succeeded_verdict(ctx: WorkerContext) -> None:
    session_id, _ = _seed_minimal(ctx)
    verdict = _valid_verdict()
    with ctx.session_factory() as db:
        run = PipelineRun(session_id=session_id, status=StageStatus.RUNNING)
        db.add(run)
        db.flush()
        # The full row shape the runner writes — identical for an executed stage
        # ("resumed": False) and a digest-resumed one ("resumed": True).
        db.add(
            PipelineStage(
                run_id=run.id,
                stage="safety",
                status=StageStatus.SUCCEEDED,
                attempt=1,
                output={"digest": "d1", "payload": verdict, "resumed": True},
            )
        )
        db.commit()
        assert _running_safety_verdict(db, session_id) == verdict


def test_running_safety_verdict_picks_the_newest_running_run(ctx: WorkerContext) -> None:
    """Focus #11 / finding 30: two RUNNING runs (a crashed one plus the live one)
    exist — the reader must return the NEWEST run's safety verdict by started_at,
    the defensive ordering the docstring claims."""
    session_id, _ = _seed_minimal(ctx)
    older = _valid_verdict()  # inactive: the stale/crashed run's verdict
    text = "SAFETY - PAIN REPORTED: Pain was reported at check-in."
    newer = {
        "active": True,
        "codes": ["pain_flag"],
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    with ctx.session_factory() as db:
        for started_at, verdict in (
            (datetime(2026, 7, 7, 9, 0), older),
            (datetime(2026, 7, 7, 12, 0), newer),
        ):
            run = PipelineRun(
                session_id=session_id, status=StageStatus.RUNNING, started_at=started_at
            )
            db.add(run)
            db.flush()
            db.add(
                PipelineStage(
                    run_id=run.id,
                    stage="safety",
                    status=StageStatus.SUCCEEDED,
                    attempt=1,
                    output={"digest": "d", "payload": verdict, "resumed": False},
                )
            )
        db.commit()
        assert _running_safety_verdict(db, session_id) == newer


# ------------------------------------------------------------ plan upsert


def test_upsert_drill_plan_creates_then_updates_and_defaults_missing_safety(
    ctx: WorkerContext,
) -> None:
    _, player_id = _seed_minimal(ctx)
    created = planner_agent.DrillPlanResult(
        blocks=[_fun_block()], finding_ids=[], safety=None, safety_sha256=None, total_balls=50
    )
    with session_scope(ctx.session_factory) as db:
        row = _upsert_drill_plan(db, player_id, PLAN_DATE, created)
        assert row.safety == {}  # None safety coerced to {} (non-null column)

    updated = planner_agent.DrillPlanResult(
        blocks=[_fun_block()],
        finding_ids=["f1"],
        safety={"active": False, "codes": [], "text": "", "sha256": "h"},
        safety_sha256="h",
        total_balls=50,
    )
    with session_scope(ctx.session_factory) as db:
        row = _upsert_drill_plan(db, player_id, PLAN_DATE, updated)
        assert row.finding_ids == ["f1"]
        assert row.safety["sha256"] == "h"
    with ctx.session_factory() as db:
        plans = list(db.scalars(select(DrillPlan).where(DrillPlan.player_id == player_id)))
        assert len(plans) == 1  # upsert, not duplicate


# --------------------------------------------- finding 22: pain on the plan date


def test_pipeline_blocks_bowling_on_pain_reported_for_the_plan_date(ctx: WorkerContext) -> None:
    """Finding 22: the plan protects ``session_date + 1``; an uncleared pain
    check-in dated that day must suppress bowling even though the pipeline runs
    after the session (pain is a current flag, not windowed to the session date)."""
    session_id, player_id = _seed_full(ctx)
    with session_scope(ctx.session_factory) as db:
        db.add(
            WellnessCheckin(
                player_id=player_id,
                session_id=session_id,
                checkin_date=PLAN_DATE,  # the plan's own day, AFTER the session date
                soreness={},
                pain=True,
                created_by="player",
            )
        )
    outcome = run_pipeline(ctx, session_id, registry=_media_ok_registry())
    assert outcome.status == "succeeded"
    plan = _plan_for(ctx, player_id)
    assert not any(block["intent"] == "bowling" for block in plan.blocks)
    assert plan.safety["active"] is True
    assert "pain_flag" in plan.safety["codes"]


# ------------------------------------ finding 34: coach-authored plan preserved


def test_run_planner_preserves_a_coach_authored_plan(ctx: WorkerContext) -> None:
    """A DrillPlan not written by the planner (created_by != 'planner') is coach
    work; the planner reports it preserved and never overwrites it (finding 34)."""
    session_id, player_id = _seed_full(ctx)
    run_analysis(ctx, session_id)
    coach_blocks = [_fun_block()]
    with session_scope(ctx.session_factory) as db:
        db.add(
            DrillPlan(
                player_id=player_id,
                plan_date=PLAN_DATE,
                blocks=coach_blocks,
                finding_ids=[],
                safety={},
                safety_sha256=None,
                created_by="coach",
            )
        )
    out = run_planner(ctx, session_id)
    assert out["plan_preserved"] is True
    assert out["created_by"] == "coach"
    plan = _plan_for(ctx, player_id)
    assert plan.created_by == "coach"  # untouched
    assert plan.blocks == coach_blocks  # not clobbered by the machine plan


# ------------------------------ finding 24: developer alert on supremacy violation


def test_safety_supremacy_violation_persists_a_developer_alert(ctx: WorkerContext) -> None:
    """A rejected (tampered) plan records a developer Alert before the stage
    fails, idempotent per run despite the runner's retries (finding 24)."""
    session_id, _ = _seed_full(ctx, pain=True)
    registry = _media_ok_registry(planner=_path("tamper_planner"))
    outcome = run_pipeline(
        ctx, session_id, registry=registry, policy=RunPolicy(max_attempts=2, backoff_base_s=0.0)
    )
    assert outcome.status == "failed"
    with ctx.session_factory() as db:
        alerts = list(db.scalars(select(Alert).where(Alert.code == SAFETY_SUPREMACY_ALERT_CODE)))
    assert len(alerts) == 1  # one per run, not one per retried attempt
    alert = alerts[0]
    assert alert.audience is AlertAudience.DEVELOPER
    assert alert.severity == "critical"
    assert alert.session_id == session_id
    assert alert.detail["run_id"] == str(outcome.run_id)
    assert alert.detail["reasons"]  # the validator's violation list


# ---------------------------- finding 40: honest degradation when analysis fails


def test_report_degrades_honestly_when_analysis_stage_failed(ctx: WorkerContext) -> None:
    """Analysis FAILED but media is fine: the report must say analysis was
    unavailable, never fabricate a clean session over zero findings (finding 40)."""
    session_id, _ = _seed_full(ctx, with_config=True)
    registry = _media_ok_registry(analysis=_path("fail_stage"))
    outcome = run_pipeline(
        ctx, session_id, registry=registry, policy=RunPolicy(max_attempts=1, backoff_base_s=0.0)
    )
    fate = {s.stage: s for s in outcome.stages}
    assert fate["analysis"].status is StageStatus.FAILED
    assert fate["planner"].status is StageStatus.SKIPPED
    assert fate["safety"].status is StageStatus.SUCCEEDED
    assert fate["report"].status is StageStatus.SUCCEEDED
    with ctx.session_factory() as db:
        report = db.scalar(select(Report).where(Report.session_id == session_id))
        assert report is not None
        assert report.body["honesty_banner"] == ANALYSIS_UNAVAILABLE_BANNER
        assert report.body["main_correction"] is None  # no fabricated correction
        assert report.body["coverage_note"] is None  # empty day: banner, not a coverage note


# --------------- major: day-scoped report honesty on a multi-session degraded day


class _GoodQuality:
    """A bannerless QualityScore stand-in (``.as_dict()`` seam)."""

    def as_dict(self) -> dict[str, Any]:
        return {"components": {}, "composite": 0.9, "banner": None}


def _force_good_quality(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a bannerless data-quality score so the DAY-scoped honesty logic —
    not the orthogonal thin-data quality banner (SQLite fixtures seed no full
    media) — decides the report path in these tests."""
    monkeypatch.setattr("cricai_worker.agent_stages.score_session", lambda _inputs: _GoodQuality())


def test_degraded_sibling_run_keeps_the_days_findings_and_audits_the_demotion(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 succeeds and is PUBLISHED; S2's later run degrades (metrics crash).

    The shared whole-day report must KEEP S1's findings under an honest coverage
    note (never wiped to a suppress-all banner), and its silent PUBLISHED->DRAFT
    demotion must be recorded in the audit log (the major cross-fix contradiction).
    """
    _force_good_quality(monkeypatch)
    fast = RunPolicy(max_attempts=1, backoff_base_s=0.0)
    s1_id, player_id = _seed_full(ctx, with_config=True, with_clips=True)
    out1 = run_pipeline(ctx, s1_id, registry=_media_ok_registry(), policy=fast)
    assert out1.status == "succeeded"
    with ctx.session_factory() as db:
        report = db.scalar(select(Report).where(Report.player_id == player_id))
        assert report is not None
        assert report.body["main_correction"] is not None
        report.status = ReportStatus.PUBLISHED
        db.commit()
        report_id = report.id

    s2_id = _add_same_day_session(ctx, player_id)
    out2 = run_pipeline(
        ctx, s2_id, registry=_media_ok_registry(metrics=_path("fail_stage")), policy=fast
    )
    fate = {s.stage: s.status for s in out2.stages}
    assert fate["metrics"] is StageStatus.FAILED
    assert fate["analysis"] is StageStatus.SKIPPED
    assert fate["report"] is StageStatus.SUCCEEDED

    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        # The day's real findings survive the degraded sibling run.
        assert report.body["main_correction"] is not None
        # Honest day-scoped coverage note instead of the suppress-all banner.
        assert report.body["coverage_note"] == ANALYSIS_PARTIAL_COVERAGE_NOTE
        assert report.body["honesty_banner"] is None
        # The PUBLISHED->DRAFT demotion is recorded, not silent.
        assert report.status is ReportStatus.DRAFT
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "report_regenerated", AuditLog.entity_id == str(report_id)
            )
        )
        assert audit is not None
        assert audit.actor == "worker"
        assert audit.detail["old_status"] == ReportStatus.PUBLISHED.value
        assert audit.detail["reason"] == "regenerated by pipeline run"


def test_degraded_run_before_a_good_sibling_leaves_no_stale_banner(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse order: S2 degrades first (empty day -> honest banner), then S1
    succeeds last. The final whole-day report carries the findings and no stale
    banner — the day's content is not run-order-dependent in a harmful way."""
    _force_good_quality(monkeypatch)
    fast = RunPolicy(max_attempts=1, backoff_base_s=0.0)
    s1_id, player_id = _seed_full(ctx, with_config=True, with_clips=True)
    s2_id = _add_same_day_session(ctx, player_id)

    out2 = run_pipeline(
        ctx, s2_id, registry=_media_ok_registry(metrics=_path("fail_stage")), policy=fast
    )
    assert {s.stage: s.status for s in out2.stages}["report"] is StageStatus.SUCCEEDED
    with ctx.session_factory() as db:
        report = db.scalar(select(Report).where(Report.player_id == player_id))
        assert report is not None
        # No sibling findings yet: the honest suppress-all banner (empty day).
        assert report.body["main_correction"] is None
        assert report.body["honesty_banner"] == ANALYSIS_UNAVAILABLE_BANNER

    out1 = run_pipeline(ctx, s1_id, registry=_media_ok_registry(), policy=fast)
    assert out1.status == "succeeded"
    with ctx.session_factory() as db:
        report = db.scalar(select(Report).where(Report.player_id == player_id))
        assert report is not None
        # Findings now ship, no stale banner, no caveat (this run was not degraded).
        assert report.body["main_correction"] is not None
        assert report.body["honesty_banner"] is None
        assert report.body["coverage_note"] is None


# ------------------------------ finding 4/48b: digit-free drill report text


def test_drill_report_text_prefers_digit_free_setup_then_name_then_none(
    ctx: WorkerContext,
) -> None:
    """A digit in the drill's free setup/name would make every report citing it
    unpublishable, so the resolver text degrades gracefully (all three branches)."""
    clean = Drill(
        name="Head-still leg defence",
        setup="Back-foot defence to short leg-stump balls.",
        machine_settings={},
        ball_count=30,
        target_metric="control_pct",
        intent=BlockIntent.TECHNICAL,
        author="coach",
    )
    assert _drill_report_text(clean) == clean.setup  # digit-free setup wins

    numeric_setup = Drill(
        name="Cone weave",
        setup="Place 2 cones 30 cm apart and defend.",
        machine_settings={},
        ball_count=30,
        target_metric="control_pct",
        intent=BlockIntent.TECHNICAL,
        author="coach",
    )
    assert _drill_report_text(numeric_setup) == "Cone weave"  # fall back to digit-free name

    numeric_both = Drill(
        name="Drill 3",
        setup="Place 2 cones 30 cm apart and defend.",
        machine_settings={},
        ball_count=30,
        target_metric="control_pct",
        intent=BlockIntent.TECHNICAL,
        author="coach",
    )
    assert _drill_report_text(numeric_both) is None  # both carry digits -> default text


# ------------------------------ finding 2: triggering session's own bowling counts


def test_triggering_bowling_session_counts_its_own_balls_toward_the_ceiling(
    ctx: WorkerContext,
) -> None:
    """A BOWLING session's own bowling must reduce the H1 allowance even with an
    empty ledger and no backfill run (finding 2): _evaluate_safety derives the
    session's auto ledger row before reading the window, so the very session that
    fired the run is not ignored by the ceiling."""
    with ctx.session_factory() as db:
        player = Player(name="Bowler", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = SessionRow(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        for ball_no in range(1, 25):  # 24 valid bowling deliveries, empty ledger
            db.add(
                BallEvent(
                    session_id=session.id,
                    ball_no=ball_no,
                    start_ms=ball_no * 1000,
                    release_ms=ball_no * 1000 + 100,
                    end_ms=ball_no * 1000 + 900,
                    confidence=0.9,
                    valid=True,
                )
            )
        db.commit()
        session_id = session.id

    with ctx.session_factory() as db:
        session = db.get(SessionRow, session_id)
        assert session is not None
        _config, summary, _verdict = _evaluate_safety(db, session)
        # The session's own balls were derived into a single auto ledger row...
        auto = list(
            db.scalars(
                select(BowlingLedgerEntry).where(BowlingLedgerEntry.session_id == session_id)
            )
        )
        assert len(auto) == 1
        assert auto[0].balls == 24
        assert auto[0].source == "auto_backfill"
        # ...and they count toward the ceiling (pre-fix the empty ledger gave 0).
        assert summary.weighted_balls > 0


# ------------------------------ finding 45: contract validators reject bad output


def test_run_analysis_rejects_a_contract_invalid_finding(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _ = _seed_full(ctx)
    invalid = [
        {
            "finding_id": "x-1",
            "agent": "analysis",
            "kind": "rule",
            "severity": "minor",
            "metric": "control",
            "condition": {},
            "n": 1,
            "effect_size": None,
            "confidence": 0.5,
            "ball_ids": [1],
            "evidence": {},
            "text_data": {},
        }  # neither rule_key nor probe -> contract #2 violation
    ]
    monkeypatch.setattr(analysis_agent, "run_analysis", lambda *_a, **_k: invalid)
    with pytest.raises(contracts.ContractViolation):
        run_analysis(ctx, session_id)


def test_run_planner_rejects_a_contract_invalid_block(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _ = _seed_full(ctx)
    run_analysis(ctx, session_id)
    bad_plan = planner_agent.DrillPlanResult(
        blocks=[
            {
                "intent": "grind",  # not a BlockIntent / bowling marker
                "balls": 10,
                "drill_id": None,
                "machine_settings": {},
                "success_metric": "control_pct",
                "finding_id": None,
            }
        ],
        finding_ids=[],
        safety=None,
        safety_sha256=None,
        total_balls=10,
    )
    monkeypatch.setattr(planner_agent, "build_plan", lambda *_a, **_k: bad_plan)
    with pytest.raises(contracts.ContractViolation):
        run_planner(ctx, session_id)


def test_run_progress_rejects_a_contract_invalid_snapshot(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-J1 wiring pin: run_progress rejects a contract-invalid snapshot (deleting
    its validate_progress_snapshot call must break a test, finding 45)."""
    session_id, _ = _seed_full(ctx)
    monkeypatch.setattr(
        progress_agent, "build_snapshots", lambda *_a, **_k: [{"not": "a valid snapshot"}]
    )
    with pytest.raises(contracts.ContractViolation):
        run_progress(ctx, session_id)


def test_run_safety_rejects_a_contract_invalid_verdict(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-J1 wiring pin: run_safety rejects a malformed verdict (deleting its
    validate_safety_verdict call must break a test, finding 45)."""
    session_id, _ = _seed_full(ctx)
    monkeypatch.setattr(safety_agent, "evaluate", lambda *_a, **_k: {"active": "not-a-bool"})
    with pytest.raises(contracts.ContractViolation):
        run_safety(ctx, session_id)


def test_run_report_rejects_a_contract_invalid_body(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-J1 wiring pin: run_report rejects a contract-invalid persisted body
    (deleting its validate_report_body call must break a test, finding 45)."""
    session_id, _ = _seed_full(ctx)
    broken_body = {
        "kind": "hourly",  # not a ReportKind -> contract #4 violation
        "period": {"start": str(SESSION_DATE), "end": str(SESSION_DATE)},
        "main_correction": None,
        "drill": None,
        "goal": None,
        "secondary": [],
        "positive": None,
        "safety": None,
        "honesty_banner": None,
        "coverage_note": None,
        "fatigue_note": None,
        "claims": [],
    }
    monkeypatch.setattr(
        "cricai_worker.generate_report.assemble_report_body", lambda **_k: dict(broken_body)
    )
    with pytest.raises(contracts.ContractViolation):
        run_report(ctx, session_id)


# ----------------------------------------------------- report collaborator seams


def test_persist_supremacy_alert_dedupes_per_run(ctx: WorkerContext) -> None:
    """One alert per run: the same run de-dupes, a different run gets its own row."""
    session_id, _ = _seed_minimal(ctx)
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    _persist_supremacy_alert(ctx, session_id, run_a, ["schedules bowling while pain active"])
    _persist_supremacy_alert(ctx, session_id, run_a, ["schedules bowling while pain active"])
    _persist_supremacy_alert(ctx, session_id, run_b, ["ceiling reached"])
    with ctx.session_factory() as db:
        alerts = list(db.scalars(select(Alert).where(Alert.code == SAFETY_SUPREMACY_ALERT_CODE)))
    assert len(alerts) == 2
    assert {alert.detail["run_id"] for alert in alerts} == {str(run_a), str(run_b)}


def test_run_trends_maps_progress_snapshot_directions(ctx: WorkerContext) -> None:
    session_id, _ = _seed_minimal(ctx)
    with ctx.session_factory() as db:
        run = PipelineRun(session_id=session_id, status=StageStatus.RUNNING)
        db.add(run)
        db.flush()
        db.add(
            PipelineStage(
                run_id=run.id,
                stage="progress",
                status=StageStatus.SUCCEEDED,
                attempt=1,
                output={
                    "digest": "d",
                    "payload": {
                        "snapshots": [
                            {"metric": "control_pct", "direction": "regressing"},
                            {"metric": "head_stability_score", "direction": "improving"},
                            "not-a-mapping",  # ignored
                            {"direction": "flat"},  # no metric -> ignored
                        ]
                    },
                    "resumed": False,
                },
            )
        )
        db.commit()
        assert _run_trends(db, session_id) == {
            "control_pct": "regressing",
            "head_stability_score": "improving",
        }


def test_run_trends_empty_without_a_succeeded_progress_stage(ctx: WorkerContext) -> None:
    session_id, _ = _seed_minimal(ctx)
    with ctx.session_factory() as db:
        assert _run_trends(db, session_id) == {}  # no running run
        db.add(PipelineRun(session_id=session_id, status=StageStatus.RUNNING))
        db.commit()
        assert _run_trends(db, session_id) == {}  # run, but no succeeded progress stage


def test_drill_resolver_maps_findings_to_the_planned_drill(ctx: WorkerContext) -> None:
    session_id, player_id = _seed_full(ctx, with_clips=True)
    run_analysis(ctx, session_id)
    run_planner(ctx, session_id)
    with ctx.session_factory() as db:
        resolver = _drill_resolver(db, player_id, PLAN_DATE)
        assert resolver is not None
        plan = db.scalar(
            select(DrillPlan).where(
                DrillPlan.player_id == player_id, DrillPlan.plan_date == PLAN_DATE
            )
        )
        assert plan is not None
        mapped = next(block for block in plan.blocks if block["drill_id"] is not None)
        drill = resolver({"finding_id": mapped["finding_id"]})
        assert drill is not None
        assert drill["drill_id"] == mapped["drill_id"]
        assert isinstance(drill["ball_count"], int)
        assert resolver({"finding_id": "no-such-finding"}) is None  # closure miss branch


def test_drill_resolver_none_when_no_plan_or_no_mapped_drill(ctx: WorkerContext) -> None:
    # No drills seeded -> the plan is all maintenance/fun/bowling (no drill_id).
    session_id, player_id = _seed_minimal(ctx)
    run_analysis(ctx, session_id)
    run_planner(ctx, session_id)
    with ctx.session_factory() as db:
        assert _drill_resolver(db, player_id, PLAN_DATE) is None  # plan exists, no mapped drill
        assert _drill_resolver(db, uuid.uuid4(), PLAN_DATE) is None  # no plan at all


def test_drill_resolver_skips_a_digit_laden_drill(ctx: WorkerContext) -> None:
    """A mapped drill whose setup AND name both carry digits is skipped, so the
    report falls back to its default drill text rather than a publish block."""
    _session_id, player_id = _seed_minimal(ctx)
    with ctx.session_factory() as db:
        drill = Drill(
            name="Drill 3",  # digit in the name
            setup="Place 2 cones 30 cm apart.",  # digits in the setup
            machine_settings={},
            ball_count=30,
            target_metric="control_pct",
            intent=BlockIntent.TECHNICAL,
            author="coach",
        )
        db.add(drill)
        db.flush()
        db.add(
            DrillPlan(
                player_id=player_id,
                plan_date=PLAN_DATE,
                blocks=[
                    {
                        "intent": "technical",
                        "balls": 30,
                        "drill_id": str(drill.id),
                        "machine_settings": {},
                        "success_metric": "control_pct",
                        "finding_id": "f-1",
                    }
                ],
                finding_ids=["f-1"],
                safety={},
                safety_sha256=None,
                created_by="planner",
            )
        )
        db.commit()
    with ctx.session_factory() as db:
        # The only drill-bearing block maps a digit-laden drill -> skipped -> None.
        assert _drill_resolver(db, player_id, PLAN_DATE) is None
