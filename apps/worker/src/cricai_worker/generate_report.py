"""US-G3: the daily-report job — assemble, word, hash, persist (contract #9).

``generate_report(ctx, session_id, *, writer=None, ...)`` is the seam between
the report assembler (g2), the LLM writer (g3), the safety agent (h2) and the
quality scorer (l4):

- **findings** load from ``findings`` rows for the session (contract #2);
- **quality** (contract #6) and **safety** (contract #3) arrive as plain
  dicts via parameters with honest ``None`` defaults — the pipeline (j1)
  injects real values; standalone runs stay honest about what they lack;
- the injected ``writer`` words the body; ANY exception — or a structural
  mutation the guard catches — falls back to the deterministic rule-based
  writer and records why in an ``audit_log`` row (US-G4 fallback);
- the safety verdict is re-inserted VERBATIM after the writer ran and its
  text is SHA-256 hashed into ``reports.safety_sha256`` (US-H5: the publish
  validator recomputes the hash and rejects mismatch);
- the ``reports`` row upserts on the (player, kind, period) unique key, so
  regenerating a session's report is idempotent and always lands in DRAFT.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cricai_coaching import workload
from cricai_coaching.bowling_report import assemble_bowling_report_body
from cricai_coaching.fatigue import (
    HIGHER_IS_BETTER,
    BallSample,
    FatigueConfig,
    score_fatigue,
)
from cricai_coaching.report import (
    DrillResolver,
    ReportSources,
    ReportWriter,
    RuleBasedWriter,
    assemble_report_body,
    batting_split_block,
)
from cricai_coaching.review_gate import review_due_at_for
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_data.ballrecord import session_ball_records
from cricai_data.enums import ReportKind, ReportStatus, SessionType
from cricai_data.models import (
    AuditLog,
    BowlingLedgerEntry,
    Finding,
    Report,
    ReportLLMAudit,
    SafetyConfig,
    Session,
    SessionBlock,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.backfill_workload import ensure_session_ledger
from cricai_worker.context import WorkerContext

#: audit_log actor for writer-fallback rows (auditable US-G4 fallback path).
AUDIT_ACTOR = "worker:generate_report"

#: Body fields an injected writer may reword; everything else must survive
#: byte-identical (numbers, keys, evidence, safety are not the writer's).
#: ``coverage_note`` (day-scoped honesty caveat) and ``fatigue_note`` (whose
#: numeric window/control-drop/degrading-metrics are ours, not the writer's)
#: are structural so an injected writer can neither invent nor tamper with them.
_STRUCTURAL_KEYS = (
    "kind",
    "period",
    "goal",
    "claims",
    "honesty_banner",
    "coverage_note",
    "fatigue_note",
    "safety",
)

#: Additive data sections that ride on some bodies only: the leg-spin block
#: (US-I7 — scorecards, scatter, agreement matrix, workload, coach-signed
#: modules) on bowling bodies, and the plan-vs-actual split (US-H2) on
#: batting/MIXED bodies with tagged blocks. Both are pure structured data
#: (measurements, reconciliations), so on a body that carries one the writer
#: may not reword it — like the fatigue note's numeric components. A body
#: without the key stays byte-identical.
_ADDITIVE_STRUCTURAL_KEYS = ("bowling", "batting_split")

#: Fatigue (US-H3) monitors the two numeric technique metrics a BallRecord
#: carries; both degrade downward (a tired batter's head steadies less and the
#: front foot moves less to the pitch), so both are "higher is better".
REPORT_FATIGUE_DIRECTIONS: dict[str, str] = {
    "head_stability_score": HIGHER_IS_BETTER,
    "front_foot_direction_cm": HIGHER_IS_BETTER,
}

#: Default fatigue config for report generation (full-session window/baseline).
REPORT_FATIGUE_CONFIG = FatigueConfig(directions=dict(REPORT_FATIGUE_DIRECTIONS))


class WriterRejected(ValueError):
    """An injected writer broke the ReportWriter contract (guard, not crash)."""


@dataclass(frozen=True)
class ReportResult:
    """What one generate_report run did (job summary, test surface)."""

    report_id: uuid.UUID
    created: bool
    writer: str  # "injected" | "rule_based"
    fallback_reason: str | None
    honest: bool  # True when the honesty banner shipped instead of findings


def finding_to_contract(row: Finding) -> dict[str, Any]:
    """Project a ``findings`` row onto the contract-#2 dict shape.

    Contract #2 requires exactly one of ``rule_key``/``probe`` to name the
    finding's origin; a probe finding has no ``rule_key`` and stores its probe
    name in ``payload['probe']`` (finding 47), so it is surfaced here as a
    top-level ``probe`` key — mirroring ``agent_stages._finding_to_contract`` so
    both read-back shapes satisfy the contract.
    """
    payload = dict(row.payload)
    contract: dict[str, Any] = {
        "finding_id": str(row.id),
        "agent": row.agent,
        "rule_key": row.rule_key,
        "kind": row.kind,
        "severity": row.severity,
        "metric": row.metric,
        "condition": dict(row.condition),
        "n": row.n,
        "effect_size": row.effect_size,
        "confidence": row.confidence,
        "ball_ids": list(row.ball_ids),
        "evidence": dict(row.evidence),
        "payload": payload,
        "text_data": dict(payload.get("text_data", {})),
    }
    probe = payload.get("probe")
    if probe is not None:
        contract["probe"] = probe
    return contract


def _day_findings(db: OrmSession, session: Session) -> list[dict[str, Any]]:
    """Every finding from ALL of the player's sessions on the report's date.

    A daily report is the player's WHOLE day (US-G3): findings reference their
    triggering session via ``findings.session_id``, so a player with a morning
    and an afternoon session gets one report merging both (finding [25/6]).
    """
    day_session_ids = list(
        db.scalars(
            select(Session.id).where(
                Session.player_id == session.player_id,
                Session.session_date == session.session_date,
            )
        )
    )
    rows = db.scalars(
        select(Finding).where(Finding.session_id.in_(day_session_ids)).order_by(Finding.id)
    )
    return [finding_to_contract(row) for row in rows]


def _fatigue_note(
    db: OrmSession, session_id: uuid.UUID, config: FatigueConfig
) -> dict[str, Any] | None:
    """The US-H3 fatigue note for the triggering session, or None (finding [53]).

    Per-ball technique series and block context are read from the canonical
    BallRecords; the scorer's context-change guard makes a block switch mid
    session read as "not evaluated" (None), never a false fatigue flag.
    """
    records = session_ball_records(db, session_id)
    samples = [
        BallSample(
            ball_no=int(record["ball_id"]),
            control=record["control"],
            technique={
                metric: float(record[metric])
                for metric in config.directions
                if isinstance(record.get(metric), int | float)
            },
            intent=record["block_id"],
            machine_settings=None,
        )
        for record in records
    ]
    return score_fatigue(samples, config).note


def _bowling_workload_block(db: OrmSession, session: Session) -> dict[str, Any]:
    """Week-to-date bowling workload vs the US-H1 ceiling (US-I7 AC).

    A bowling report ALWAYS carries this block — the honesty path included — so
    a ceiling breach stays visible even on a day with no coaching correction
    (SAF). The triggering session's own balls are ensured into the ledger first
    (the same idempotent unit the safety stage uses, so both agree), entries
    are read in the deterministic ``(entry_date, created_at, id)`` order, and
    the window ends on the session date ("week-to-date"). Numbers ride as
    structured data fields (like the fatigue note's components), each
    recomputable from ledger rows plus the active safety config.
    """
    ensure_session_ledger(db, session)
    config_row = db.scalar(select(SafetyConfig).order_by(SafetyConfig.version.desc()).limit(1))
    config = dict(config_row.config) if config_row is not None else dict(DEFAULT_SAFETY_CONFIG)
    entries = list(
        db.scalars(
            select(BowlingLedgerEntry)
            .where(BowlingLedgerEntry.player_id == session.player_id)
            .order_by(
                BowlingLedgerEntry.entry_date,
                BowlingLedgerEntry.created_at,
                BowlingLedgerEntry.id,
            )
        )
    )
    summary = workload.summarize_window(
        entries, birthdate=session.player.birthdate, end=session.session_date, config=config
    )
    return {
        "window": {
            "start": summary.window_start.isoformat(),
            "end": summary.window_end.isoformat(),
        },
        "weighted_overs": round(summary.weighted_overs, 2),
        "ceiling_overs": summary.ceiling_overs,
        "remaining_balls": summary.remaining_balls,
        "violations": [code.value for code in summary.violations],
    }


def _day_tagged_blocks(db: OrmSession, session: Session) -> list[dict[str, Any]]:
    """The day's tagged batting/MIXED blocks with their actual ball counts (US-H2).

    A daily report is the player's WHOLE day (US-G3): every BATTING or MIXED
    session on the report date contributes its declared blocks. A block's
    actual balls are the canonical BallRecords attributed to it — by the US-B3
    by-timestamp rule or an explicit tag hint, exactly as the T5 #2 golden
    counts them — so this reconciles against the real pipeline's attribution,
    not a tag-level shortcut. Each block is the ``{intent, balls, drill_id}``
    shape ``reconcile_batting_split`` consumes; ``drill_id`` is null (session
    blocks are not drill-converted), matching the fun-block predicate's input.
    """
    day_sessions = db.scalars(
        select(Session).where(
            Session.player_id == session.player_id,
            Session.session_date == session.session_date,
            Session.session_type.in_((SessionType.BATTING, SessionType.MIXED)),
        )
    )
    blocks: list[dict[str, Any]] = []
    for day_session in day_sessions:
        session_blocks = list(
            db.scalars(
                select(SessionBlock)
                .where(SessionBlock.session_id == day_session.id)
                .order_by(SessionBlock.block_no)
            )
        )
        if not session_blocks:
            continue
        # A ball's block is its canonical BallRecord ``block_id`` (str, or None
        # when it fell outside every block); a None tally is harmless — no
        # declared block's id ever equals it, so those balls count toward no
        # intent, which is exactly right.
        counts: dict[str, int] = {}
        for record in session_ball_records(db, day_session.id):
            block_id = record["block_id"]
            counts[block_id] = counts.get(block_id, 0) + 1
        blocks.extend(
            {
                "intent": block.intent.value,
                "balls": counts.get(str(block.id), 0),
                "drill_id": None,
            }
            for block in session_blocks
        )
    return blocks


def _batting_split_block(db: OrmSession, session: Session) -> dict[str, Any] | None:
    """The US-H2 plan-vs-actual split for the day, or None (finding [12/19/31]).

    Wires US-H2's report AC ("Report shows plan-vs-actual per block; > 25%
    deviation flagged") onto a production surface: the day's tagged blocks
    reconcile against the GOVERNING safety config's batting split (latest
    version, else the seed default — the same config
    ``routers/workload.py._active_config`` resolves), and the additive
    structured block ships on the report body. Returns None when the day
    declared no tagged blocks, so the key is simply absent rather than a
    fabricated zero-plan reconciliation.
    """
    blocks = _day_tagged_blocks(db, session)
    if not blocks:
        return None
    config_row = db.scalar(select(SafetyConfig).order_by(SafetyConfig.version.desc()).limit(1))
    config = dict(config_row.config) if config_row is not None else dict(DEFAULT_SAFETY_CONFIG)
    return batting_split_block(workload.reconcile_batting_split(blocks, config))


def _guard_writer_output(original: dict[str, Any], worded: Any) -> dict[str, Any]:
    """Reject writer output that changed anything but wording (US-G4 guard)."""
    if not isinstance(worded, dict):
        raise WriterRejected("writer returned a non-dict body")
    if set(worded.keys()) != set(original.keys()):
        raise WriterRejected("writer changed the body's keys")
    structural: tuple[str, ...] = (
        *_STRUCTURAL_KEYS,
        *(key for key in _ADDITIVE_STRUCTURAL_KEYS if key in original),
    )
    for key in structural:
        if worded[key] != original[key]:
            raise WriterRejected(f"writer changed structural field {key!r}")
    for field in ("main_correction", "secondary"):
        if (worded[field] is None) != (original[field] is None):
            raise WriterRejected(f"writer added or removed {field!r}")
    main = original["main_correction"]
    if main is not None:
        reworded = worded["main_correction"]
        if reworded["finding_id"] != main["finding_id"] or reworded["evidence"] != main["evidence"]:
            raise WriterRejected("writer changed the main correction's identity or evidence")
    return worded


def _apply_writer(
    body: dict[str, Any], context: dict[str, Any], writer: ReportWriter | None
) -> tuple[dict[str, Any], str, str | None]:
    """Word the body; any injected-writer failure falls back deterministically."""
    fallback = RuleBasedWriter()
    if writer is None:
        return fallback.write(body, context), "rule_based", None
    try:
        worded = _guard_writer_output(body, writer.write(copy.deepcopy(body), context))
    except Exception as exc:  # the contract: ANY writer failure falls back
        return fallback.write(body, context), "rule_based", f"{type(exc).__name__}: {exc}"
    return worded, "injected", None


def _upsert_report(
    db: OrmSession, session: Session, body: dict[str, Any], quality: dict[str, Any] | None
) -> tuple[Report, bool]:
    """Insert-or-refresh the daily report on its (player, kind, period) key."""
    report = db.scalar(
        select(Report).where(
            Report.player_id == session.player_id,
            Report.kind == ReportKind.DAILY,
            Report.period_start == session.session_date,
            Report.period_end == session.session_date,
        )
    )
    created = report is None
    if report is None:
        report = Report(
            player_id=session.player_id,
            session_id=session.id,
            kind=ReportKind.DAILY,
            period_start=session.session_date,
            period_end=session.session_date,
            body=body,
        )
        db.add(report)
    else:
        # Regeneration always lands in DRAFT. If it overwrites a PUBLISHED
        # report, that demotion is a visible event a coach must be able to
        # trace (a re-run silently un-publishing the day's record was the
        # finding), so it is recorded in the audit log.
        if report.status is ReportStatus.PUBLISHED:
            db.add(
                AuditLog(
                    actor="worker",
                    action="report_regenerated",
                    entity="report",
                    entity_id=str(report.id),
                    detail={
                        "old_status": report.status.value,
                        "reason": "regenerated by pipeline run",
                    },
                )
            )
        report.session_id = session.id
        report.body = body
    # US-J5 review gate: in coach_gate mode a FRESH draft carries the deadline
    # the review sweep enforces (None under auto_publish). An already
    # gate-held draft keeps its existing deadline: pipeline re-runs (event
    # corrections, rederive cascades, crash-resume) must not creep the coach's
    # timeout window indefinitely (findings [14/30/38]). A demoted
    # PUBLISHED/BLOCKED report is a new review decision, so it restarts.
    gate_held_draft = (
        not created and report.status is ReportStatus.DRAFT and report.review_due_at is not None
    )
    report.status = ReportStatus.DRAFT
    if not gate_held_draft:
        report.review_due_at = review_due_at_for(db)
    report.quality = dict(quality) if quality is not None else None
    return report, created


@dataclass(frozen=True)
class _PersistInput:
    """The report row plus its audit sidecars to persist in one transaction."""

    worded: dict[str, Any]
    quality: dict[str, Any] | None
    safety_sha256: str | None
    fallback_reason: str | None
    llm_exchanges: list[dict[str, Any]] | None


def _do_persist(db: OrmSession, session: Session, data: _PersistInput) -> tuple[uuid.UUID, bool]:
    """Upsert the report, its fallback audit and LLM exchange rows, then commit."""
    report, created = _upsert_report(db, session, data.worded, data.quality)
    report.safety_sha256 = data.safety_sha256
    db.flush()  # surfaces the (player, kind, period) uq race as IntegrityError
    if data.fallback_reason is not None:
        db.add(
            AuditLog(
                actor=AUDIT_ACTOR,
                action="writer_fallback",
                entity="report",
                entity_id=str(report.id),
                detail={"reason": data.fallback_reason},
            )
        )
    for exchange in data.llm_exchanges or ():
        db.add(
            ReportLLMAudit(
                report_id=report.id,
                prompt_key=exchange["prompt_key"],
                request=exchange["request"],
                response=exchange["response"],
                accepted=exchange["accepted"],
                reason=exchange["reason"],
            )
        )
    db.commit()
    return report.id, created


def _persist_report(
    db: OrmSession, session: Session, data: _PersistInput
) -> tuple[uuid.UUID, bool]:
    """Persist with one retry on the concurrent-upsert race (finding [25/6]).

    Two same-day sessions for one player hold different per-session locks, so
    both can select-miss and insert the same (player, kind, period) row; the
    loser's flush raises :class:`IntegrityError`. We roll back and retry once —
    the retry's select now finds the committed row and converts to an update.
    A second failure propagates (a real, non-transient integrity problem).
    """
    try:
        return _do_persist(db, session, data)
    except IntegrityError:
        db.rollback()
        return _do_persist(db, session, data)


def generate_report(  # noqa: PLR0913  (public seam: honest optional keyword collaborators)
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    writer: ReportWriter | None = None,
    quality: dict[str, Any] | None = None,
    safety: dict[str, Any] | None = None,
    trends: Mapping[str, str] | None = None,
    drill_for: DrillResolver | None = None,
    fatigue_config: FatigueConfig | None = None,
    coverage_note: str | None = None,
    llm_exchanges: list[dict[str, Any]] | None = None,
) -> ReportResult:
    """Generate (or regenerate) the daily report for the player's WHOLE day (US-G3).

    A BOWLING triggering session assembles the leg-spin body instead (US-I7):
    the same Report-body-v1 plus the additive ``bowling`` section built from
    the session's canonical BallRecords and the week-to-date US-H1 workload
    block; batting and mixed sessions keep the batting path untouched.
    Findings are loaded from every session the player had on this date (a
    daily report merges them). ``trends`` (progress-agent metric->direction,
    US-G3 ranking factor) and ``drill_for`` (the planner's finding->drill
    mapping) are threaded into assembly with honest ``None`` defaults — the
    pipeline's ``run_report`` supplies them from the progress stage output and
    the next day's DrillPlan. The US-H3 fatigue note is computed from the
    triggering session's per-ball technique series. ``coverage_note`` is the
    day-scoped honesty caveat ``run_report`` sets when this run degraded but
    sibling sessions on the date still produced findings (the corrections
    ship under the note rather than being suppressed). ``llm_exchanges``, when a
    writer emits into it, is persisted as ``report_llm_audits`` rows linked to
    the report (accepted and rejected); the rule-based default writes none.
    """
    with ctx.session_factory() as db:
        session = db.get(Session, session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        findings = _day_findings(db, session)
        fatigue = _fatigue_note(db, session_id, fatigue_config or REPORT_FATIGUE_CONFIG)
        sources = ReportSources(
            trends=trends,
            quality=quality,
            safety=safety,
            drill_for=drill_for,
            fatigue=fatigue,
            coverage_note=coverage_note,
        )
        if session.session_type is SessionType.BOWLING:
            # US-I7: a bowling session's daily report is the leg-spin body —
            # Report-body-v1 (all US-G3 ACs unchanged) plus the additive
            # ``bowling`` blocks (scorecard, release scatter, variation
            # agreement, learning modules, week-to-date workload vs ceiling).
            # Dispatch follows the TRIGGERING session's type; day-scoped
            # finding merging is unchanged either way.
            body = assemble_bowling_report_body(
                kind=ReportKind.DAILY.value,
                period_start=session.session_date,
                period_end=session.session_date,
                findings=findings,
                sources=sources,
                records=session_ball_records(db, session_id),
                workload=_bowling_workload_block(db, session),
            )
        else:
            body = assemble_report_body(
                kind=ReportKind.DAILY.value,
                period_start=session.session_date,
                period_end=session.session_date,
                findings=findings,
                sources=sources,
            )
            # US-H2 (finding [12/19/31]): a batting/MIXED day with tagged blocks
            # gains the additive plan-vs-actual split, computed over the whole
            # day's blocks against the governing safety config (like the bowling
            # body's additive data blocks). Absent on block-less days.
            split = _batting_split_block(db, session)
            if split is not None:
                body["batting_split"] = split
        context: dict[str, Any] = {
            "findings": findings,
            "history": {},
            "rules": [],
            "tone": "encouraging",
            "safety": dict(safety) if safety is not None else None,
        }
        worded, writer_used, fallback_reason = _apply_writer(body, context, writer)

        # US-H5: safety verdict re-inserted VERBATIM post-writer, then hashed.
        worded["safety"] = dict(safety) if safety is not None else None
        safety_sha256 = (
            hashlib.sha256(str(safety["text"]).encode("utf-8")).hexdigest()
            if safety is not None
            else None
        )

        report_id, created = _persist_report(
            db,
            session,
            _PersistInput(
                worded=worded,
                quality=quality,
                safety_sha256=safety_sha256,
                fallback_reason=fallback_reason,
                llm_exchanges=llm_exchanges,
            ),
        )
        return ReportResult(
            report_id=report_id,
            created=created,
            writer=writer_used,
            fallback_reason=fallback_reason,
            honest=worded["honesty_banner"] is not None,
        )
