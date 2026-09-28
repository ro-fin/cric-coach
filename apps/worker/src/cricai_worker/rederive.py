"""Re-derive cascade (Phase-3 debt; Phase-5 plan scope decision).

Deletes every *machine-derived* dependent of a session — clips, pose tracks,
ball tracks, bounce estimates, machine-source ball-metrics values, findings,
reports, pipeline runs/stages — so the caller can re-run the DAG (the API
router does; an RQ caller enqueues) and regenerate them from the events now on
record. This replaces the Phase-3 "block beats corrupt" dead end (see
``cricai_worker.detect_events`` KNOWN LIMITATION) while keeping its protection
philosophy:

- **REFUSES before any write** when rows a human authored would be corrupted
  by re-detection renumbering — ball tags, bounce marks, reference balls,
  event corrections, and stored ball-metric values with ``source == "manual"``.
  The error is structured (a 409 in the API) and lists every blocking
  category with its row/value count.
- ``confirm_manual_invalidation=True`` deletes those manual rows too — an
  explicit, audited decision: an AuditLog row enumerates every count.
- Evidence verdicts (coach review of a finding) are *not* a blocker even
  though they are human input: they hang off ``findings.id`` via a hard FK and
  cannot outlive the findings being regenerated, so they cascade with the
  machine rows and are counted (``evidence_verdicts_deleted``) rather than
  blocking — the deletion is still in the audit row.
- Object-store artifacts (clip files, landmark/track payloads) are left in
  place: their keys are deterministic per ``(session, ball, camera)`` and the
  regenerating jobs overwrite them; rows are the source of truth for what
  exists.

Concurrency: the cascade takes the same per-session advisory lock as the DAG
runner, so it can never delete ``pipeline_runs`` out from under a live run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, cast

from cricai_data.db import session_scope
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
    PoseTrack,
    ReferenceBall,
    Report,
    ReportLLMAudit,
    TagAudit,
)
from cricai_data.models import Session as SessionRow
from sqlalchemy import CursorResult, Delete, Select, delete, func, select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import AdvisoryLock, SessionLock, advisory_lock_key, context_engine

#: Metric values with this ``source`` are human-entered (US-E2..E4 contract).
MANUAL_METRIC_SOURCE = "manual"

#: Audit action written for every cascade (US-L3-style traceability).
REDERIVE_AUDIT_ACTION = "rederive"


class RederiveBlockedError(RuntimeError):
    """Manual rows exist that re-derivation would invalidate (409 in the API).

    ``blockers`` maps each blocking category to its row/value count. Nothing
    was modified; pass ``confirm_manual_invalidation=True`` to delete the
    manual rows too (audited).
    """

    def __init__(self, session_id: uuid.UUID, blockers: dict[str, int]) -> None:
        self.session_id = session_id
        self.blockers = blockers
        detail = ", ".join(f"{count} {label}" for label, count in sorted(blockers.items()))
        super().__init__(
            f"re-derive blocked for session {session_id}: manual rows would be"
            f" invalidated ({detail}); nothing was modified. Pass"
            " confirm_manual_invalidation=true to delete them too (audited)."
        )


class SessionBusyError(RuntimeError):
    """The session's pipeline advisory lock is held — retry after the run."""

    def __init__(self, session_id: uuid.UUID) -> None:
        self.session_id = session_id
        super().__init__(f"session {session_id} is locked by a running pipeline; re-derive skipped")


@dataclass(frozen=True)
class RederiveSummary:
    """What one cascade deleted, ready for API responses and audit detail."""

    session_id: uuid.UUID
    evidence_verdicts_deleted: int
    findings_deleted: int
    report_llm_audits_deleted: int
    reports_deleted: int
    pipeline_stages_deleted: int
    pipeline_runs_deleted: int
    clips_deleted: int
    pose_tracks_deleted: int
    ball_tracks_deleted: int
    bounce_estimates_deleted: int
    ball_metrics_rows_deleted: int
    machine_metric_values_removed: int
    manual_metric_values_removed: int
    ball_tags_deleted: int
    tag_audits_deleted: int
    bounce_marks_deleted: int
    reference_balls_deleted: int
    event_corrections_deleted: int
    manual_invalidation: bool

    def counts(self) -> dict[str, Any]:
        return {
            "evidence_verdicts_deleted": self.evidence_verdicts_deleted,
            "findings_deleted": self.findings_deleted,
            "report_llm_audits_deleted": self.report_llm_audits_deleted,
            "reports_deleted": self.reports_deleted,
            "pipeline_stages_deleted": self.pipeline_stages_deleted,
            "pipeline_runs_deleted": self.pipeline_runs_deleted,
            "clips_deleted": self.clips_deleted,
            "pose_tracks_deleted": self.pose_tracks_deleted,
            "ball_tracks_deleted": self.ball_tracks_deleted,
            "bounce_estimates_deleted": self.bounce_estimates_deleted,
            "ball_metrics_rows_deleted": self.ball_metrics_rows_deleted,
            "machine_metric_values_removed": self.machine_metric_values_removed,
            "manual_metric_values_removed": self.manual_metric_values_removed,
            "ball_tags_deleted": self.ball_tags_deleted,
            "tag_audits_deleted": self.tag_audits_deleted,
            "bounce_marks_deleted": self.bounce_marks_deleted,
            "reference_balls_deleted": self.reference_balls_deleted,
            "event_corrections_deleted": self.event_corrections_deleted,
            "manual_invalidation": self.manual_invalidation,
        }


def _delete_rows(db: OrmSession, statement: Delete) -> int:
    """Execute a bulk DELETE and return the number of rows removed."""
    return cast("CursorResult[Any]", db.execute(statement)).rowcount


def _count(db: OrmSession, statement: Select[tuple[int]]) -> int:
    return int(db.scalar(statement) or 0)


def _manual_metric_values(db: OrmSession, session_id: uuid.UUID) -> int:
    """Stored metric values a human entered (``source == "manual"``)."""
    rows = db.scalars(select(BallMetrics).where(BallMetrics.session_id == session_id)).all()
    return sum(
        1
        for row in rows
        for entry in row.metrics.values()
        if isinstance(entry, dict) and entry.get("source") == MANUAL_METRIC_SOURCE
    )


def _manual_blockers(db: OrmSession, session_id: uuid.UUID) -> dict[str, int]:
    """Count every manual-row category that re-derivation would invalidate."""
    event_ids = select(BallEvent.id).where(BallEvent.session_id == session_id)
    candidates = {
        "ball_tags": _count(
            db, select(func.count(BallTag.id)).where(BallTag.session_id == session_id)
        ),
        "bounce_marks": _count(
            db, select(func.count(BounceMark.id)).where(BounceMark.session_id == session_id)
        ),
        "reference_balls": _count(
            db,
            select(func.count(ReferenceBall.id)).where(ReferenceBall.session_id == session_id),
        ),
        "event_corrections": _count(
            db,
            select(func.count(EventCorrection.id)).where(EventCorrection.event_id.in_(event_ids)),
        ),
        "manual_ball_metric_values": _manual_metric_values(db, session_id),
    }
    return {label: count for label, count in candidates.items() if count}


def _delete_ball_metrics(db: OrmSession, session_id: uuid.UUID) -> tuple[int, int, int]:
    """Delete the session's metric rows; count machine vs manual values.

    Reaching this point means either no manual values exist (unconfirmed path
    refused earlier otherwise) or the caller confirmed manual invalidation —
    so whole rows go, never a silent half-row.
    """
    rows = db.scalars(select(BallMetrics).where(BallMetrics.session_id == session_id)).all()
    machine_values = 0
    manual_values = 0
    for row in rows:
        for entry in row.metrics.values():
            if isinstance(entry, dict) and entry.get("source") == MANUAL_METRIC_SOURCE:
                manual_values += 1
            else:
                machine_values += 1
        db.delete(row)
    return len(rows), machine_values, manual_values


def _delete_machine_rows(db: OrmSession, session_id: uuid.UUID) -> dict[str, int]:
    """Delete machine-derived dependents in FK-safe order (findings before runs)."""
    finding_ids = select(Finding.id).where(Finding.session_id == session_id)
    evidence_verdicts = _delete_rows(
        db, delete(EvidenceVerdictRecord).where(EvidenceVerdictRecord.finding_id.in_(finding_ids))
    )
    findings = _delete_rows(db, delete(Finding).where(Finding.session_id == session_id))
    report_ids = select(Report.id).where(Report.session_id == session_id)
    report_llm_audits = _delete_rows(
        db, delete(ReportLLMAudit).where(ReportLLMAudit.report_id.in_(report_ids))
    )
    reports = _delete_rows(db, delete(Report).where(Report.session_id == session_id))
    run_ids = select(PipelineRun.id).where(PipelineRun.session_id == session_id)
    pipeline_stages = _delete_rows(
        db, delete(PipelineStage).where(PipelineStage.run_id.in_(run_ids))
    )
    pipeline_runs = _delete_rows(
        db, delete(PipelineRun).where(PipelineRun.session_id == session_id)
    )
    clips = _delete_rows(db, delete(Clip).where(Clip.session_id == session_id))
    pose_tracks = _delete_rows(db, delete(PoseTrack).where(PoseTrack.session_id == session_id))
    ball_tracks = _delete_rows(db, delete(BallTrack).where(BallTrack.session_id == session_id))
    bounce_estimates = _delete_rows(
        db, delete(BounceEstimate).where(BounceEstimate.session_id == session_id)
    )
    return {
        "evidence_verdicts": evidence_verdicts,
        "findings": findings,
        "report_llm_audits": report_llm_audits,
        "reports": reports,
        "pipeline_stages": pipeline_stages,
        "pipeline_runs": pipeline_runs,
        "clips": clips,
        "pose_tracks": pose_tracks,
        "ball_tracks": ball_tracks,
        "bounce_estimates": bounce_estimates,
    }


def _delete_manual_rows(db: OrmSession, session_id: uuid.UUID) -> dict[str, int]:
    """Delete the confirmed manual rows (tag audits before their FK target)."""
    tag_ids = select(BallTag.id).where(BallTag.session_id == session_id)
    tag_audits = _delete_rows(db, delete(TagAudit).where(TagAudit.tag_id.in_(tag_ids)))
    ball_tags = _delete_rows(db, delete(BallTag).where(BallTag.session_id == session_id))
    bounce_marks = _delete_rows(db, delete(BounceMark).where(BounceMark.session_id == session_id))
    reference_balls = _delete_rows(
        db, delete(ReferenceBall).where(ReferenceBall.session_id == session_id)
    )
    event_ids = select(BallEvent.id).where(BallEvent.session_id == session_id)
    event_corrections = _delete_rows(
        db, delete(EventCorrection).where(EventCorrection.event_id.in_(event_ids))
    )
    return {
        "ball_tags": ball_tags,
        "tag_audits": tag_audits,
        "bounce_marks": bounce_marks,
        "reference_balls": reference_balls,
        "event_corrections": event_corrections,
    }


def rederive_session(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    actor: str,
    confirm_manual_invalidation: bool = False,
    lock: SessionLock | None = None,
) -> RederiveSummary:
    """Run the cascade for one session; see the module docstring.

    Raises :class:`LookupError` for an unknown session,
    :class:`SessionBusyError` when a pipeline run holds the session lock, and
    :class:`RederiveBlockedError` (before any write) when unconfirmed manual
    rows exist. Re-enqueuing the DAG afterwards is the caller's step (the API
    router runs the pipeline in-process; an RQ caller enqueues) so a cascade
    can never race the run it triggers — both take this same session lock.
    """
    session_lock: SessionLock = (
        lock
        if lock is not None
        else AdvisoryLock(engine=context_engine(ctx), key=advisory_lock_key(session_id))
    )
    if not session_lock.acquire():
        raise SessionBusyError(session_id)
    try:
        with session_scope(ctx.session_factory) as db:
            return _cascade(
                db,
                session_id,
                actor=actor,
                confirm_manual_invalidation=confirm_manual_invalidation,
            )
    finally:
        session_lock.release()


def _cascade(
    db: OrmSession,
    session_id: uuid.UUID,
    *,
    actor: str,
    confirm_manual_invalidation: bool,
) -> RederiveSummary:
    if db.get(SessionRow, session_id) is None:
        raise LookupError(f"session {session_id} not found")
    blockers = _manual_blockers(db, session_id)
    if blockers and not confirm_manual_invalidation:
        raise RederiveBlockedError(session_id, blockers)

    machine = _delete_machine_rows(db, session_id)
    metric_rows, machine_values, manual_values = _delete_ball_metrics(db, session_id)
    manual_invalidation = bool(blockers)
    manual = (
        _delete_manual_rows(db, session_id)
        if manual_invalidation
        else {
            "ball_tags": 0,
            "tag_audits": 0,
            "bounce_marks": 0,
            "reference_balls": 0,
            "event_corrections": 0,
        }
    )
    summary = RederiveSummary(
        session_id=session_id,
        evidence_verdicts_deleted=machine["evidence_verdicts"],
        findings_deleted=machine["findings"],
        report_llm_audits_deleted=machine["report_llm_audits"],
        reports_deleted=machine["reports"],
        pipeline_stages_deleted=machine["pipeline_stages"],
        pipeline_runs_deleted=machine["pipeline_runs"],
        clips_deleted=machine["clips"],
        pose_tracks_deleted=machine["pose_tracks"],
        ball_tracks_deleted=machine["ball_tracks"],
        bounce_estimates_deleted=machine["bounce_estimates"],
        ball_metrics_rows_deleted=metric_rows,
        machine_metric_values_removed=machine_values,
        manual_metric_values_removed=manual_values,
        ball_tags_deleted=manual["ball_tags"],
        tag_audits_deleted=manual["tag_audits"],
        bounce_marks_deleted=manual["bounce_marks"],
        reference_balls_deleted=manual["reference_balls"],
        event_corrections_deleted=manual["event_corrections"],
        manual_invalidation=manual_invalidation,
    )
    db.add(
        AuditLog(
            actor=actor,
            action=REDERIVE_AUDIT_ACTION,
            entity="session",
            entity_id=str(session_id),
            detail=summary.counts(),
        )
    )
    return summary
