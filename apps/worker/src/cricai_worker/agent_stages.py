"""Agent-stage adapters wiring the coaching agents into the DAG (US-J1/J2/J3/H5).

The pipeline runner (:mod:`cricai_worker.pipeline`) resolves each stage to a
``(ctx, session_id) -> Mapping | None`` callable and persists the returned
payload into ``pipeline_stages.output``. The pure coaching agents in
:mod:`cricai_coaching` know nothing of the DB or the pipeline; these thin
adapters are the seam that reads the stored rows, calls the agents, persists
their artifacts and returns a size-bounded trace payload:

- ``analysis`` (:func:`run_analysis`) — assemble BallRecords, run the
  coach-approved rules (US-G2) and the analysis agent's probes (US-J2), then
  (re)persist ``findings`` rows. Findings are machine-derived and regenerable
  (Phase-5 re-derive decision), so a re-run deletes this session's existing
  findings and re-inserts — never duplicates. Each finding's rule-authored
  ``text_data`` is folded into ``payload['text_data']``, the exact shape both
  :mod:`cricai_worker.generate_report` and ``routers/drills.py`` read back.
- ``progress`` (:func:`run_progress`) — frozen ProgressSnapshots (US-J4) from
  the nightly ``metric_baselines``; the snapshots ARE the stage output (the
  progress agent caps them, so the trace stays bounded).
- ``planner`` (:func:`run_planner`) — build and upsert tomorrow's DrillPlan
  (US-J3) under the H1 allowance and the H5 safety verdict.
- ``safety`` (:func:`run_safety`) — recompute the verdict (shared with the
  planner) and INDEPENDENTLY re-validate the persisted plan (US-H5 supremacy):
  a violating plan raises (after recording a developer Alert), failing the
  stage; because ``report`` hard-depends on ``safety`` in ``STAGE_DEPS``, the
  report then skips — no report ships past a safety violation.
- ``report`` (:func:`run_report`) — the pinned writer seam (#9): score data
  quality (US-L4) and thread this run's trace collaborators (safety verdict,
  progress trends, the next-day DrillPlan drill mapping) into the report worker;
  fail closed on a succeeded-but-empty safety verdict, degrade honestly when
  analysis did not succeed, and validate the persisted body against contract #4.

Every agent stage runs the pinned cross-group contract validator on its output
before it flows downstream (US-J1: the orchestrator rejects contract-invalid
outputs) — a :class:`~cricai_coaching.contracts.ContractViolation` fails the
stage with the offending field path recorded on its trace row.

Timezone-aware UTC, no new third-party deps; the writer worker
(:func:`cricai_worker.generate_report.generate_report`) stays untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from cricai_coaching import (
    analysis_agent,
    bowling_analysis,
    contracts,
    planner_agent,
    progress_agent,
    rules,
    safety_agent,
    wellness,
    workload,
)
from cricai_coaching.planner_agent import PlanContext
from cricai_coaching.quality import score_session
from cricai_coaching.report import DrillResolver
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_data.ballrecord import session_ball_records
from cricai_data.db import session_scope
from cricai_data.enums import AlertAudience, SessionType, StageStatus
from cricai_data.models import (
    Alert,
    BowlingLedgerEntry,
    CoachingRule,
    Drill,
    DrillPlan,
    EvidenceVerdictRecord,
    Finding,
    MetricBaseline,
    PainClearance,
    PipelineRun,
    PipelineStage,
    Report,
    RuleOverride,
    SafetyConfig,
    WellnessCheckin,
)
from cricai_data.models import Session as SessionRow
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.backfill_workload import ensure_session_ledger
from cricai_worker.context import WorkerContext
from cricai_worker.drift_monitor import load_quality_inputs
from cricai_worker.generate_report import generate_report

#: ``drill_plans.created_by`` stamped on plans this pipeline stage writes.
PLANNER_ACTOR = "planner"

#: Stage names this module reads out of the run trace (single source; both the
#: pipeline and these readers agree on the vocabulary).
SAFETY_STAGE = "safety"
ANALYSIS_STAGE = "analysis"
PROGRESS_STAGE = "progress"

#: Developer Alert code raised when the safety stage rejects a violating plan —
#: a supremacy rejection is a safety event adults must see, not only a trace row
#: (US-H5, finding 24).
SAFETY_SUPREMACY_ALERT_CODE = "safety_supremacy_violation"

#: Honest degraded banner shipped when the whole DAY has no findings AND this
#: run's analysis stage did not succeed: the report says bat-path analysis was
#: unavailable rather than fabricating a clean session over zero findings (US-J1
#: degraded-but-honest, finding 40). It makes no claim about "the rest of the
#: report" — on this path there is no correction to show until re-processing.
ANALYSIS_UNAVAILABLE_BANNER = (
    "Bat-path analysis was unavailable for this session, so no coaching correction "
    "can be shown until the session is re-processed."
)

#: Day-scoped coverage note (finding: multi-session day, one session degraded).
#: When THIS run's analysis did not succeed but sibling sessions on the same date
#: DID produce findings, the day's real corrections still ship under this short
#: caveat instead of being wiped by the suppress-all banner. Number-free so it
#: adds no claim and stays publishable.
ANALYSIS_PARTIAL_COVERAGE_NOTE = (
    "One of today's sessions could not be fully analysed; the corrections below "
    "come from the sessions that were."
)


class AnalysisBlockedError(RuntimeError):
    """Re-analysis would invalidate coach evidence verdicts — block, don't corrupt.

    Findings are machine-derived and regenerated by delete-then-insert, but
    :class:`~cricai_data.models.EvidenceVerdictRecord` rows (a coach's US-G6
    review of a finding) are human input hard-FK'd to ``findings.id``. A plain
    pipeline re-run (trigger/resume, not a re-derive) must not delete or orphan
    them — block-beats-corrupt (US-J1, Phase-3 re-derive philosophy). The
    remedy is the audited re-derive path, which cascades the verdicts on an
    explicit ``confirm_manual_invalidation`` decision.
    """


class ReportSafetyError(RuntimeError):
    """The safety stage succeeded but left no usable verdict (US-H5, finding 23).

    ``report`` hard-depends on ``safety``, so a SUCCEEDED safety stage whose
    stage output is missing or is not a valid SafetyVerdict is an invariant
    violation, not a degraded mode. run_report raises so the report stage FAILS
    closed rather than shipping a verdict-less report. A safety stage that was
    SKIPPED or FAILED upstream is different — that is honest degradation, has no
    SUCCEEDED row, and yields a ``None`` verdict the report notes instead.
    """


def _load_session(db: OrmSession, session_id: uuid.UUID) -> SessionRow:
    """Load the session or raise ``LookupError`` (the runner's convention)."""
    session = db.get(SessionRow, session_id)
    if session is None:
        raise LookupError(f"session {session_id} not found")
    return session


def _latest_running_run(db: OrmSession, session_id: uuid.UUID) -> PipelineRun | None:
    """The session's most recent still-running pipeline run, if any.

    Stage callables receive only ``(ctx, session_id)`` — no run id — so the
    current run is located by its RUNNING status (the runner marks it running
    for the whole run and only flips it at the end). Newest first is defensive
    against a prior crashed run that never left RUNNING.
    """
    return db.scalar(
        select(PipelineRun)
        .where(PipelineRun.session_id == session_id, PipelineRun.status == StageStatus.RUNNING)
        .order_by(PipelineRun.started_at.desc())
        .limit(1)
    )


def _active_safety_config(db: OrmSession) -> dict[str, Any]:
    """Latest ``safety_configs`` version, else the canonical defaults.

    Mirrors ``routers/workload.py`` / ``routers/drills.py``: an unseeded table
    (fresh test DBs) falls back to :data:`DEFAULT_SAFETY_CONFIG` so the engine
    always has thresholds to evaluate.
    """
    row = db.scalar(select(SafetyConfig).order_by(SafetyConfig.version.desc()).limit(1))
    return dict(row.config) if row is not None else dict(DEFAULT_SAFETY_CONFIG)


def _finding_to_contract(row: Finding) -> dict[str, Any]:
    """Persisted finding as the pinned contract-#2 dict (mirrors drills router).

    Contract #2 requires exactly one of ``rule_key``/``probe`` to name a
    finding's origin. ``findings`` has no probe column, so a probe finding's
    name is stored in ``payload['probe']`` (finding 47) and surfaced here as the
    top-level ``probe`` key — the read-back dict then satisfies the contract.
    Rule findings keep ``rule_key`` and carry no ``probe``.
    """
    contract: dict[str, Any] = {
        "finding_id": str(row.id),
        "agent": row.agent,
        "rule_key": row.rule_key,
        "kind": row.kind,
        "severity": row.severity,
        "metric": row.metric,
        "condition": row.condition,
        "n": row.n,
        "effect_size": row.effect_size,
        "confidence": row.confidence,
        "ball_ids": row.ball_ids,
        "evidence": row.evidence,
        "text_data": row.payload.get("text_data", {}),
    }
    probe = row.payload.get("probe")
    if probe is not None:
        contract["probe"] = probe
    return contract


def _drill_seam(row: Drill) -> dict[str, Any]:
    """Enabled drill as the planner's library seam dict (mirrors drills router)."""
    return {
        "id": str(row.id),
        "target_metric": row.target_metric,
        "intent": row.intent.value,
        "enabled": row.enabled,
        "machine_settings": row.machine_settings,
    }


def _refuse_if_verdicts_block(db: OrmSession, session_id: uuid.UUID) -> None:
    """Raise :class:`AnalysisBlockedError` if coach verdicts reference the findings.

    Guards the delete-then-insert below: a re-run must not delete findings that
    a coach has reviewed (US-G6). Runs before any work, so the block never
    touches a row — the audited re-derive path is the only way to clear them.
    """
    verdicts = list(
        db.scalars(
            select(EvidenceVerdictRecord).where(
                EvidenceVerdictRecord.finding_id.in_(
                    select(Finding.id).where(Finding.session_id == session_id)
                )
            )
        )
    )
    if verdicts:
        finding_ids = ", ".join(sorted({str(verdict.finding_id) for verdict in verdicts}))
        raise AnalysisBlockedError(
            f"{len(verdicts)} coach evidence verdict(s) reference this session's findings "
            f"({finding_ids}); machine re-analysis will not delete reviewed findings. Run the "
            "audited re-derive: it cascades and counts these verdicts on its own. Pass "
            "confirm_manual_invalidation=true only if the re-derive returns a 409 blocker list "
            "you accept losing — that flag also permanently deletes this session's manual ball "
            "tags, bounce marks, reference balls and event corrections, after which re-analysis "
            "derives nothing."
        )


def _finding_row_id(session_id: uuid.UUID, wire_finding_id: str) -> uuid.UUID:
    """Deterministic Finding primary key: same (session, wire id) -> same row id.

    The wire ``finding_id`` (``ru-<hex>`` for rules, ``an-<hex>`` for probes) is
    a pure function of the finding's content, so re-running analysis over
    unchanged data re-mints identical primary keys instead of fresh random
    UUIDs. Report claims and plan finding references then stay valid across
    re-runs rather than dangling, and ranking tiebreaks stop flipping (finding
    38). The wire id itself is kept in ``payload['finding_id']`` for traceability.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"cricai:finding:{session_id}:{wire_finding_id}")


def run_analysis(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``analysis`` stage: (re)derive this session's findings (US-G2/J2/I7).

    Runs the coach-approved rules and the analysis probes over the session's
    canonical BallRecords, then replaces the session's findings idempotently
    (delete-then-insert; findings are regenerable). A BOWLING session routes to
    the leg-spin probes (US-I7:
    :func:`cricai_coaching.bowling_analysis.run_bowling_analysis`, emitting the
    Phase-6 bowling finding kinds) alongside the same rule runner — the bowling
    seed rules fire through the ordinary US-G2 DSL — while every other session
    type keeps the batting path untouched. Returns the new finding count and
    the ordered persisted finding ids for the run trace. Raises
    :class:`AnalysisBlockedError` (before touching anything) when a coach has
    reviewed a finding — re-derive is then the only, audited way forward.
    """
    with session_scope(ctx.session_factory) as db:
        session = _load_session(db, session_id)
        _refuse_if_verdicts_block(db, session_id)
        records = session_ball_records(db, session_id)
        rule_rows = list(db.scalars(select(CoachingRule)))
        overrides = list(
            db.scalars(select(RuleOverride).where(RuleOverride.player_id == session.player_id))
        )
        rule_findings = rules.run_rules(rule_rows, records, overrides)
        if session.session_type is SessionType.BOWLING:
            merged = bowling_analysis.run_bowling_analysis(records, rule_findings=rule_findings)
        else:
            merged = analysis_agent.run_analysis(records, rule_findings=rule_findings)

        for stale in db.scalars(select(Finding).where(Finding.session_id == session_id)):
            db.delete(stale)
        db.flush()

        run = _latest_running_run(db, session_id)
        run_id = None if run is None else run.id
        finding_ids: list[str] = []
        for finding in merged:
            contracts.validate_finding(finding)  # US-J1: reject contract-invalid output
            wire_id = str(finding["finding_id"])
            payload = {
                **dict(finding.get("payload", {})),
                "text_data": dict(finding["text_data"]),
                "finding_id": wire_id,  # stable content id, traceable across re-runs
            }
            probe = finding.get("probe")
            if probe is not None:
                payload["probe"] = probe  # probe origin survives persist (contract #2)
            row = Finding(
                id=_finding_row_id(session_id, wire_id),
                session_id=session_id,
                run_id=run_id,
                agent=finding["agent"],
                rule_key=finding.get("rule_key"),
                kind=finding["kind"],
                severity=finding["severity"],
                metric=finding["metric"],
                condition=dict(finding["condition"]),
                n=int(finding["n"]),
                effect_size=finding["effect_size"],
                confidence=float(finding["confidence"]),
                ball_ids=[int(ball_id) for ball_id in finding["ball_ids"]],
                evidence=dict(finding["evidence"]),
                payload=payload,
            )
            db.add(row)
            db.flush()
            finding_ids.append(str(row.id))
        return {"finding_count": len(finding_ids), "finding_ids": finding_ids}


def run_progress(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``progress`` stage: frozen ProgressSnapshots as of the session date (US-J4).

    Reads the player's ``metric_baselines`` snapshots only — never recomputes
    history — and freezes the view to the session date. The snapshots (already
    size-bounded by the progress agent) are the stage output.
    """
    with ctx.session_factory() as db:
        session = _load_session(db, session_id)
        rows = [
            progress_agent.baseline_to_mapping(row)
            for row in db.scalars(
                select(MetricBaseline).where(MetricBaseline.player_id == session.player_id)
            )
        ]
        snapshots = progress_agent.build_snapshots(rows, as_of=session.session_date)
        for snapshot in snapshots:
            contracts.validate_progress_snapshot(snapshot)  # US-J1: reject invalid output
        return {"snapshots": snapshots}


def _evaluate_safety(
    db: OrmSession, session: SessionRow
) -> tuple[dict[str, Any], workload.WindowSummary, dict[str, Any]]:
    """(config, rolling-7 summary, SafetyVerdict) for the plan the stage gates.

    The single source of the verdict, shared by the planner and safety stages
    so both agree byte-for-byte — the planner embeds it, the safety stage
    re-derives it to validate the persisted plan against.

    The plan being gated is dated ``session_date + 1`` (US-J3), so the workload
    ceiling is evaluated as of that PLAN date, not the session date — otherwise a
    run made after new state landed would bless a bowling plan for a day whose
    ceiling it never checked (finding 22). Pain is a current medical flag, not a
    windowed statistic: it is evaluated against EVERY uncleared check-in
    regardless of date (``as_of=date.max``), so an uncleared pain report on the
    plan's own day — or any later day — suppresses bowling (US-H4, finding 22).

    Ledger rows are read in a deterministic ``(entry_date, created_at, id)``
    order so the summary never depends on insertion or fetch order (finding 2).

    A BOWLING triggering session's OWN bowling must count toward the ceiling:
    the backfill job has no production caller yet, so its auto ledger row is
    derived here (idempotently, via the shared
    :func:`~cricai_worker.backfill_workload.ensure_session_ledger`) before the
    window is read — otherwise the H1 allowance ignores the very session that
    fired the run (finding 2).
    """
    player = session.player
    config = _active_safety_config(db)
    plan_date = session.session_date + timedelta(days=1)
    if session.session_type is SessionType.BOWLING:
        ensure_session_ledger(db, session)
    entries = list(
        db.scalars(
            select(BowlingLedgerEntry)
            .where(BowlingLedgerEntry.player_id == player.id)
            .order_by(
                BowlingLedgerEntry.entry_date,
                BowlingLedgerEntry.created_at,
                BowlingLedgerEntry.id,
            )
        )
    )
    summary = workload.summarize_window(
        entries, birthdate=player.birthdate, end=plan_date, config=config
    )
    ledger_summary = {"violations": [code.value for code in summary.violations]}
    checkins = list(
        db.scalars(select(WellnessCheckin).where(WellnessCheckin.player_id == player.id))
    )
    clearances = list(db.scalars(select(PainClearance).where(PainClearance.player_id == player.id)))
    wellness_state = wellness.evaluate_wellness(
        checkins, clearances, as_of=date.max, config=config.get("wellness")
    )
    verdict = safety_agent.evaluate(ledger_summary, wellness_state, config)
    return config, summary, verdict


def _upsert_drill_plan(
    db: OrmSession, player_id: uuid.UUID, plan_date: date, plan: planner_agent.DrillPlanResult
) -> DrillPlan:
    """Upsert the day's plan on ``(player_id, plan_date)`` (mirrors drills router)."""
    row = db.scalar(
        select(DrillPlan).where(DrillPlan.player_id == player_id, DrillPlan.plan_date == plan_date)
    )
    if row is None:
        row = DrillPlan(player_id=player_id, plan_date=plan_date)
        db.add(row)
    row.blocks = plan.blocks
    row.finding_ids = plan.finding_ids
    row.safety = plan.safety if plan.safety is not None else {}
    row.safety_sha256 = plan.safety_sha256
    row.created_by = PLANNER_ACTOR
    db.flush()
    return row


def run_planner(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``planner`` stage: build and upsert tomorrow's DrillPlan (US-J3/H1/H5).

    Ranks the session's persisted findings, maps them to enabled library drills
    under the batting split, and adds a bowling block only up to the remaining
    H1 allowance — zero when a workload-ceiling or pain verdict is active (the
    planner enforces the hard block itself). The plan is upserted idempotently.

    A DrillPlan a coach authored or edited is NEVER clobbered (finding 34): if
    the existing ``(player, plan_date)`` row was not written by this stage
    (``created_by != PLANNER_ACTOR``), the planner leaves it untouched and
    reports it preserved. Coach workload/safety decisions are human input; the
    safety stage still re-validates the preserved plan against the live verdict.
    """
    with session_scope(ctx.session_factory) as db:
        session = _load_session(db, session_id)
        config, summary, verdict = _evaluate_safety(db, session)
        plan_date = session.session_date + timedelta(days=1)
        existing = db.scalar(
            select(DrillPlan).where(
                DrillPlan.player_id == session.player_id, DrillPlan.plan_date == plan_date
            )
        )
        if existing is not None and existing.created_by != PLANNER_ACTOR:
            return {
                "plan_date": plan_date.isoformat(),
                "plan_preserved": True,
                "created_by": existing.created_by,
                "safety_active": bool(verdict["active"]),
                "safety_codes": list(verdict["codes"]),
            }
        allowance = summary.remaining_balls if summary.remaining_balls is not None else 0
        findings = analysis_agent.rank_findings(
            [
                _finding_to_contract(row)
                for row in db.scalars(
                    select(Finding).where(Finding.session_id == session_id).order_by(Finding.id)
                )
            ],
            top_k=analysis_agent.DEFAULT_ANALYSIS_CONFIG.top_k,
        )
        drills = [
            _drill_seam(row)
            for row in db.scalars(select(Drill).where(Drill.enabled.is_(True)).order_by(Drill.name))
        ]
        context = PlanContext(
            split=dict(config["batting_split"]),
            bowling_allowance_balls=allowance,
            safety=verdict,
        )
        plan = planner_agent.build_plan(findings, drills, context)
        contracts.validate_drill_plan_blocks(plan.blocks)  # US-J1: reject invalid output
        _upsert_drill_plan(db, session.player_id, plan_date, plan)
        return {
            "plan_date": plan_date.isoformat(),
            "plan_preserved": False,
            "block_count": len(plan.blocks),
            "total_balls": plan.total_balls,
            "finding_ids": plan.finding_ids,
            "safety_active": bool(verdict["active"]),
            "safety_codes": list(verdict["codes"]),
        }


def _persist_supremacy_alert(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    run_id: uuid.UUID | None,
    problems: Sequence[str],
) -> None:
    """Persist a developer Alert for a safety-supremacy rejection (US-H5, finding 24).

    Committed in its own transaction so it survives the runner rolling back the
    failed safety stage. Idempotent per run: the safety stage is retried on
    failure, so the alert is de-duplicated on ``(code, session, run)`` — one
    alert per run, never one per attempt.
    """
    run_marker = None if run_id is None else str(run_id)
    with session_scope(ctx.session_factory) as db:
        for existing in db.scalars(
            select(Alert).where(
                Alert.code == SAFETY_SUPREMACY_ALERT_CODE, Alert.session_id == session_id
            )
        ):
            if existing.detail.get("run_id") == run_marker:
                return
        db.add(
            Alert(
                audience=AlertAudience.DEVELOPER,
                code=SAFETY_SUPREMACY_ALERT_CODE,
                severity="critical",
                detail={
                    "session_id": str(session_id),
                    "run_id": run_marker,
                    "reasons": list(problems),
                },
                session_id=session_id,
            )
        )


def run_safety(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``safety`` stage: recompute the verdict and validate the plan (US-H5).

    Independent of the planner (defense in depth): it re-derives the verdict
    and runs the publish-time validator against the persisted plan for the next
    day. A violating plan raises :class:`~cricai_coaching.safety_agent.SafetySupremacyError`,
    which fails this stage; ``report`` hard-depends on ``safety`` in the DAG, so
    it then skips. Before re-raising, a developer Alert records the violation so
    it surfaces to adults, not only to trace readers (finding 24). The verdict
    is returned as the stage output for the report.
    """
    with ctx.session_factory() as db:
        session = _load_session(db, session_id)
        _config, _summary, verdict = _evaluate_safety(db, session)
        contracts.validate_safety_verdict(verdict)  # US-J1: reject invalid output
        plan_date = session.session_date + timedelta(days=1)
        plan = db.scalar(
            select(DrillPlan).where(
                DrillPlan.player_id == session.player_id, DrillPlan.plan_date == plan_date
            )
        )
        run = _latest_running_run(db, session_id)
        run_id = None if run is None else run.id
        artifact = (
            {
                "blocks": list(plan.blocks),
                "safety": dict(plan.safety) if plan.safety else None,
                "safety_sha256": plan.safety_sha256,
            }
            if plan is not None
            else None
        )
    if artifact is not None:
        try:
            safety_agent.validate_artifact(artifact, verdict)
        except safety_agent.SafetySupremacyError as exc:
            _persist_supremacy_alert(ctx, session_id, run_id, exc.problems)
            raise
    return verdict


def _succeeded_stage(db: OrmSession, run_id: uuid.UUID, stage: str) -> PipelineStage | None:
    """The latest SUCCEEDED attempt of ``stage`` in a run (newest attempt first)."""
    return db.scalar(
        select(PipelineStage)
        .where(
            PipelineStage.run_id == run_id,
            PipelineStage.stage == stage,
            PipelineStage.status == StageStatus.SUCCEEDED,
        )
        .order_by(PipelineStage.attempt.desc())
        .limit(1)
    )


def _running_safety_verdict(db: OrmSession, session_id: uuid.UUID) -> dict[str, Any] | None:
    """This run's safety verdict, or None when safety did not succeed this run.

    Honors the pinned writer seam (#9): the report embeds the exact verdict the
    safety stage produced for the current run. Fail-closed (US-H5, finding 23):
    a SUCCEEDED safety stage whose stage output is missing or is not a valid
    SafetyVerdict raises :class:`ReportSafetyError`, so the report stage FAILS
    rather than shipping a verdict-less report. A safety stage SKIPPED or FAILED
    upstream is honest degradation — it has no SUCCEEDED row, so this returns
    None and the report notes the gap instead of inventing a verdict.
    """
    run = _latest_running_run(db, session_id)
    if run is None:
        return None
    stage = _succeeded_stage(db, run.id, SAFETY_STAGE)
    if stage is None:
        return None
    payload = stage.output.get("payload") if stage.output is not None else None
    if not isinstance(payload, Mapping):
        raise ReportSafetyError(
            "safety stage succeeded but its stage output carries no verdict payload"
        )
    verdict = dict(payload)
    try:
        contracts.validate_safety_verdict(verdict)
    except contracts.ContractViolation as exc:
        raise ReportSafetyError(f"safety stage verdict is malformed: {exc}") from exc
    return verdict


def _analysis_degraded_this_run(db: OrmSession, session_id: uuid.UUID) -> bool:
    """True when this run's analysis stage exists but did not SUCCEED (finding 40).

    Only meaningful inside a pipeline run; a standalone call (no RUNNING run) is
    not a degraded pipeline and returns False. When analysis did not succeed, the
    report degrades honestly — the whole-day findings decide whether that is a
    suppress-all banner or a day-scoped coverage note (see :func:`run_report`).
    """
    run = _latest_running_run(db, session_id)
    if run is None:
        return False
    return _succeeded_stage(db, run.id, ANALYSIS_STAGE) is None


def _day_has_findings(db: OrmSession, session: SessionRow) -> bool:
    """True when ANY of the player's sessions on the report's date has findings.

    The daily report is DAY-scoped (finding [25/6]): it merges every session's
    findings for the date. So the degraded-run honesty check must be day-scoped
    too — one session's failed analysis must never suppress findings a sibling
    session already produced (the run-scoped banner vs day-scoped report
    contradiction). When siblings have findings, the day's corrections ship under
    a coverage note; only a day with NO findings at all takes the banner path.
    """
    day_session_ids = select(SessionRow.id).where(
        SessionRow.player_id == session.player_id,
        SessionRow.session_date == session.session_date,
    )
    hit = db.scalar(select(Finding.id).where(Finding.session_id.in_(day_session_ids)).limit(1))
    return hit is not None


def _run_trends(db: OrmSession, session_id: uuid.UUID) -> dict[str, str]:
    """This run's progress-stage metric->direction map (US-G3 ranking, finding 4).

    Read from the succeeded progress stage's snapshots in the trace; an empty
    map (progress skipped/failed, or no snapshots) is the honest default — the
    report's trend weight is then a neutral 1.0.
    """
    run = _latest_running_run(db, session_id)
    if run is None:
        return {}
    stage = _succeeded_stage(db, run.id, PROGRESS_STAGE)
    payload = stage.output.get("payload") if stage is not None and stage.output else None
    snapshots = payload.get("snapshots") if isinstance(payload, Mapping) else None
    trends: dict[str, str] = {}
    for snapshot in snapshots if isinstance(snapshots, list) else ():
        if isinstance(snapshot, Mapping):
            metric, direction = snapshot.get("metric"), snapshot.get("direction")
            if isinstance(metric, str) and isinstance(direction, str):
                trends[metric] = direction
    return trends


def _drill_report_text(drill: Drill) -> str | None:
    """Digit-free report wording for a library drill, or None.

    The publish gate claims-covers every number in report wording, but a coach's
    free-text drill ``setup``/``name`` carries no claim, so a numeral there (e.g.
    "Place 2 cones 30 cm apart") would hard-block every daily report citing that
    drill. Prefer the ``setup``, fall back to the ``name``, and if both carry a
    digit return None so the report uses its own number-free default drill text
    instead of an unexplained publish block (finding: drill.setup free text).
    """
    for candidate in (drill.setup, drill.name):
        if candidate and not any(char.isdigit() for char in candidate):
            return candidate
    return None


def _drill_resolver(db: OrmSession, player_id: uuid.UUID, plan_date: date) -> DrillResolver | None:
    """A finding->drill resolver from the next day's DrillPlan (US-J3, finding 4/48).

    Maps each drill-bearing plan block back to its motivating finding and the
    library drill it scheduled, so the report cites the SAME drill the planner
    chose. A drill whose ``setup`` and ``name`` both carry a digit is skipped so
    the report falls back to its number-free default drill text rather than a
    hard publish block. Returns None when no plan (or no resolvable drill-bearing
    block) exists — the report then falls back to rule-authored drill text with a
    null drill_id.
    """
    plan = db.scalar(
        select(DrillPlan).where(DrillPlan.player_id == player_id, DrillPlan.plan_date == plan_date)
    )
    if plan is None:
        return None
    drills = {str(row.id): row for row in db.scalars(select(Drill))}
    by_finding: dict[str, dict[str, Any]] = {}
    for block in plan.blocks:
        drill_id, finding_id = block.get("drill_id"), block.get("finding_id")
        drill = drills.get(str(drill_id)) if drill_id is not None else None
        if drill is None or not isinstance(finding_id, str):
            continue
        text = _drill_report_text(drill)
        if text is None:  # digit-laden setup and name: use the report's default text
            continue
        by_finding[finding_id] = {
            "drill_id": str(drill_id),
            "text": text,
            "machine_settings": dict(block.get("machine_settings", {})),
            "success_metric": str(block.get("success_metric", drill.target_metric)),
            "ball_count": drill.ball_count,
        }
    if not by_finding:
        return None

    def resolve(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return by_finding.get(str(finding.get("finding_id")))

    return resolve


def run_report(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``report`` stage: score quality, thread the safety verdict, word the report.

    The pinned writer seam (#9): compute the US-L4 data-quality score and read
    this run's collaborators out of the persisted stage trace — the safety
    verdict (fail-closed if the safety stage succeeded but left no usable
    verdict, finding 23), the progress-stage trends (US-G3 ranking, finding 4)
    and the next day's DrillPlan drill mapping (finding 4/48) — then hand them to
    the report worker. When this run's analysis stage did not succeed the report
    degrades honestly, but DAY-scoped to match the day-scoped report: if sibling
    sessions on the date have findings, the day's corrections ship under a
    coverage note; only a day with no findings at all takes the suppress-all
    banner (finding 40 + the day-vs-run contradiction fix). The persisted report
    body is validated against contract #4 before returning so a structurally
    broken body FAILS the stage rather than shipping (finding 45).
    """
    with ctx.session_factory() as db:
        session = _load_session(db, session_id)
        quality = score_session(load_quality_inputs(db, session)).as_dict()
        safety = _running_safety_verdict(db, session_id)
        trends = _run_trends(db, session_id)
        drill_for = _drill_resolver(db, session.player_id, session.session_date + timedelta(days=1))
        analysis_degraded = _analysis_degraded_this_run(db, session_id)
        day_has_findings = _day_has_findings(db, session)
    coverage_note: str | None = None
    if analysis_degraded and day_has_findings:
        # This run degraded but a sibling session on the date has findings: ship
        # the day's real corrections under an honest caveat — never wipe them.
        coverage_note = ANALYSIS_PARTIAL_COVERAGE_NOTE
    elif analysis_degraded:
        # The whole day has no findings and this run degraded: the honest
        # suppress-all banner dominates any quality banner (nothing to analyse).
        quality["banner"] = ANALYSIS_UNAVAILABLE_BANNER
    result = generate_report(
        ctx,
        session_id,
        quality=quality,
        safety=safety,
        trends=trends,
        drill_for=drill_for,
        coverage_note=coverage_note,
    )
    with ctx.session_factory() as db:
        report = db.scalars(select(Report).where(Report.id == result.report_id)).one()
        contracts.validate_report_body(report.body)
    return {
        "report_id": str(result.report_id),
        "created": result.created,
        "writer": result.writer,
        "fallback_reason": result.fallback_reason,
        "honest": result.honest,
        "safety_active": bool(safety["active"]) if safety is not None else False,
        "quality_composite": quality["composite"],
    }
