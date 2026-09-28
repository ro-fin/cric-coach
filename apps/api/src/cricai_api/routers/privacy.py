"""US-L3: privacy & consent — deletion round-trip, watermarked export, audit trail.

Parent-only surface. Deletion removes every derived row, stored object and
in-flight multipart staging area for a session; export is an explicit action
logged with a sha256 watermark so any shared copy can be traced back to the
audit trail.
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, NamedTuple, cast

from cricai_data.enums import Role
from cricai_data.models import (
    Alert,
    Annotation,
    AuditLog,
    BallEvent,
    BallMetrics,
    BallTag,
    BallTrack,
    BounceEstimate,
    BounceMark,
    BowlingLedgerEntry,
    BowlingTarget,
    Clip,
    CoachNote,
    DatasetMember,
    DeliveryLabel,
    EventCorrection,
    EvidenceVerdictRecord,
    Finding,
    FrameSample,
    MetricBaseline,
    PainClearance,
    PipelineRun,
    PipelineStage,
    PoseTrack,
    ReferenceBall,
    Report,
    ReportLLMAudit,
    Session,
    SessionBlock,
    TagAudit,
    UploadPart,
    UploadSession,
    Video,
    WellnessCheckin,
    utcnow,
)
from cricai_data.storage import FsObjectStore, StorageError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import CursorResult, Delete, delete, select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db, get_store
from cricai_api.egress import assert_no_media

router = APIRouter(prefix="/privacy", tags=["privacy"])

#: Audit actions surfaced by GET /privacy/audit.
PRIVACY_ACTIONS: tuple[str, ...] = ("privacy_delete", "share_export")

#: Live rows and objects are deleted here; offline media need the ops runbook.
BACKUPS_NOTE = (
    "Live database rows and stored objects for this session are deleted. "
    "Offline backups are purged by the ops runbook backup-purge step using the "
    "audit entry recorded for this deletion."
)


class DeletionOut(BaseModel):
    session_id: uuid.UUID
    ball_tags_deleted: int
    tag_audits_deleted: int
    bounce_marks_deleted: int
    blocks_deleted: int
    videos_deleted: int
    upload_sessions_deleted: int
    upload_parts_deleted: int
    event_corrections_deleted: int
    ball_events_deleted: int
    clips_deleted: int
    pose_tracks_deleted: int
    ball_metrics_deleted: int
    reference_balls_deleted: int
    ball_tracks_deleted: int
    bounce_estimates_deleted: int
    annotations_deleted: int
    dataset_members_deleted: int
    frame_samples_deleted: int
    evidence_verdicts_deleted: int
    findings_deleted: int
    report_llm_audits_deleted: int
    reports_deleted: int
    pipeline_stages_deleted: int
    pipeline_runs_deleted: int
    bowling_ledger_entries_deleted: int
    pain_clearances_deleted: int
    wellness_checkins_deleted: int
    alerts_deleted: int
    delivery_labels_deleted: int
    bowling_targets_deleted: int
    coach_notes_deleted: int
    metric_baselines_scrubbed: int
    multipart_uploads_aborted: int
    multipart_uploads_already_gone: int
    multipart_parts_purged: int
    objects_deleted: int
    notes_cleared: bool
    detail: str


class ExportIn(BaseModel):
    session_id: uuid.UUID
    purpose: str = Field(min_length=1, max_length=200)


class ExportOut(BaseModel):
    watermark_sha256: str
    export: dict[str, Any]


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor: str
    action: str
    entity: str
    entity_id: str
    detail: dict[str, Any] | None
    at: datetime


def _watermark(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _delete_rows(db: OrmSession, statement: Delete) -> int:
    """Execute a bulk DELETE and return the number of rows removed."""
    return cast("CursorResult[Any]", db.execute(statement)).rowcount


class _CoachingDeletion(NamedTuple):
    """Row counts from the Phase-5 coaching/safety part of a session deletion."""

    evidence_verdicts: int
    findings: int
    report_llm_audits: int
    reports: int
    pipeline_stages: int
    pipeline_runs: int
    bowling_ledger_entries: int
    pain_clearances: int
    wellness_checkins: int
    alerts: int


def _delete_coaching_rows(db: OrmSession, session_id: uuid.UUID) -> _CoachingDeletion:
    """Delete Phase-5 coaching/safety rows about one session, FK-safe order.

    Findings go before pipeline_runs (``findings.run_id`` FK); coach verdicts,
    LLM audits, stage rows and pain clearances go before their FK targets.
    Session-linked ledger entries, check-ins and alerts are footage-adjacent
    records of the child; player-scoped rows without a session link survive
    (they describe the player, not this session's data).
    """
    finding_ids = select(Finding.id).where(Finding.session_id == session_id)
    evidence_verdicts = _delete_rows(
        db,
        delete(EvidenceVerdictRecord).where(EvidenceVerdictRecord.finding_id.in_(finding_ids)),
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
    bowling_ledger_entries = _delete_rows(
        db, delete(BowlingLedgerEntry).where(BowlingLedgerEntry.session_id == session_id)
    )
    checkin_ids = select(WellnessCheckin.id).where(WellnessCheckin.session_id == session_id)
    pain_clearances = _delete_rows(
        db, delete(PainClearance).where(PainClearance.checkin_id.in_(checkin_ids))
    )
    wellness_checkins = _delete_rows(
        db, delete(WellnessCheckin).where(WellnessCheckin.session_id == session_id)
    )
    alerts = _delete_rows(db, delete(Alert).where(Alert.session_id == session_id))
    return _CoachingDeletion(
        evidence_verdicts=evidence_verdicts,
        findings=findings,
        report_llm_audits=report_llm_audits,
        reports=reports,
        pipeline_stages=pipeline_stages,
        pipeline_runs=pipeline_runs,
        bowling_ledger_entries=bowling_ledger_entries,
        pain_clearances=pain_clearances,
        wellness_checkins=wellness_checkins,
        alerts=alerts,
    )


def _scrub_metric_baselines(db: OrmSession, session_id: uuid.UUID, player_id: uuid.UUID) -> int:
    """De-attribute the deleted session from the player's ``metric_baselines``.

    Baseline rows are player-scoped aggregates (means over a date's metrics), so
    they survive the delete — but their payload records the ``session_ids`` that
    fed each slice. Privacy-delete removes the session's id from those payloads
    (list keys drop it; a singular key becomes ``None``) so no baseline stays
    attributable to purged footage, while the aggregate itself is preserved.
    Returns the number of baseline rows scrubbed.
    """
    target = str(session_id)
    scrubbed = 0
    baselines = db.scalars(select(MetricBaseline).where(MetricBaseline.player_id == player_id))
    for baseline in baselines:
        payload = dict(baseline.payload or {})
        changed = False
        ids = payload.get("session_ids")
        if isinstance(ids, list) and any(str(item) == target for item in ids):
            payload["session_ids"] = [item for item in ids if str(item) != target]
            changed = True
        if payload.get("session_id") is not None and str(payload["session_id"]) == target:
            payload["session_id"] = None
            changed = True
        if changed:
            baseline.payload = payload
            scrubbed += 1
    return scrubbed


@router.delete("/sessions/{session_id}")
def delete_session_data(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> DeletionOut:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")

    tag_ids = select(BallTag.id).where(BallTag.session_id == session_id)
    tag_audits_deleted = _delete_rows(db, delete(TagAudit).where(TagAudit.tag_id.in_(tag_ids)))
    ball_tags_deleted = _delete_rows(db, delete(BallTag).where(BallTag.session_id == session_id))
    bounce_marks_deleted = _delete_rows(
        db, delete(BounceMark).where(BounceMark.session_id == session_id)
    )
    blocks_deleted = _delete_rows(
        db, delete(SessionBlock).where(SessionBlock.session_id == session_id)
    )
    videos_deleted = _delete_rows(db, delete(Video).where(Video.session_id == session_id))
    upload_ids = select(UploadSession.id).where(UploadSession.session_id == session_id)
    upload_parts_deleted = _delete_rows(
        db, delete(UploadPart).where(UploadPart.upload_id.in_(upload_ids))
    )
    store_upload_ids = [
        store_upload_id
        for store_upload_id in db.scalars(
            select(UploadSession.store_upload_id).where(UploadSession.session_id == session_id)
        )
        if store_upload_id is not None
    ]
    upload_sessions_deleted = _delete_rows(
        db, delete(UploadSession).where(UploadSession.session_id == session_id)
    )

    # Phase-3 derived tables are session-keyed with no DB cascade; corrections
    # go first because they reference ball_events rows.
    event_ids = select(BallEvent.id).where(BallEvent.session_id == session_id)
    event_corrections_deleted = _delete_rows(
        db, delete(EventCorrection).where(EventCorrection.event_id.in_(event_ids))
    )
    ball_events_deleted = _delete_rows(
        db, delete(BallEvent).where(BallEvent.session_id == session_id)
    )
    clips_deleted = _delete_rows(db, delete(Clip).where(Clip.session_id == session_id))
    pose_tracks_deleted = _delete_rows(
        db, delete(PoseTrack).where(PoseTrack.session_id == session_id)
    )
    ball_metrics_deleted = _delete_rows(
        db, delete(BallMetrics).where(BallMetrics.session_id == session_id)
    )
    reference_balls_deleted = _delete_rows(
        db, delete(ReferenceBall).where(ReferenceBall.session_id == session_id)
    )
    ball_tracks_deleted = _delete_rows(
        db, delete(BallTrack).where(BallTrack.session_id == session_id)
    )
    bounce_estimates_deleted = _delete_rows(
        db, delete(BounceEstimate).where(BounceEstimate.session_id == session_id)
    )
    # Labeled frames are footage of the player: annotations and dataset
    # memberships go first (FKs onto frame_samples). Privacy wins over dataset
    # immutability — a frozen dataset that loses members no longer matches its
    # manifest digest, which the restore drill will report loudly.
    frame_ids = select(FrameSample.id).where(FrameSample.session_id == session_id)
    annotations_deleted = _delete_rows(
        db, delete(Annotation).where(Annotation.frame_id.in_(frame_ids))
    )
    dataset_members_deleted = _delete_rows(
        db, delete(DatasetMember).where(DatasetMember.frame_id.in_(frame_ids))
    )
    frame_samples_deleted = _delete_rows(
        db, delete(FrameSample).where(FrameSample.session_id == session_id)
    )

    coaching = _delete_coaching_rows(db, session_id)

    # Phase-6 session-keyed human input (US-I4/I6/K3): delivery labels, declared
    # bowling targets and session-linked coach notes are footage-adjacent and go
    # with the session; player-scoped coach notes (session_id NULL) survive.
    delivery_labels_deleted = _delete_rows(
        db, delete(DeliveryLabel).where(DeliveryLabel.session_id == session_id)
    )
    bowling_targets_deleted = _delete_rows(
        db, delete(BowlingTarget).where(BowlingTarget.session_id == session_id)
    )
    coach_notes_deleted = _delete_rows(
        db, delete(CoachNote).where(CoachNote.session_id == session_id)
    )

    metric_baselines_scrubbed = _scrub_metric_baselines(db, session_id, session.player_id)

    # In-flight multipart uploads keep staged part bytes on disk outside the
    # sessions/ prefix; abort each persisted store upload so nothing survives.
    multipart_uploads_aborted = 0
    multipart_uploads_already_gone = 0
    multipart_parts_purged = 0
    for store_upload_id in store_upload_ids:
        try:
            multipart_parts_purged += len(store.list_parts(store_upload_id))
            store.abort_multipart(store_upload_id)
        except StorageError:
            # Already completed or aborted: the staging area is gone upstream.
            multipart_uploads_already_gone += 1
        else:
            multipart_uploads_aborted += 1

    keys = store.list_keys(f"sessions/{session_id}")
    for key in keys:
        store.delete(key)

    notes_cleared = session.notes is not None
    session.notes = None

    report = DeletionOut(
        session_id=session_id,
        ball_tags_deleted=ball_tags_deleted,
        tag_audits_deleted=tag_audits_deleted,
        bounce_marks_deleted=bounce_marks_deleted,
        blocks_deleted=blocks_deleted,
        videos_deleted=videos_deleted,
        upload_sessions_deleted=upload_sessions_deleted,
        upload_parts_deleted=upload_parts_deleted,
        event_corrections_deleted=event_corrections_deleted,
        ball_events_deleted=ball_events_deleted,
        clips_deleted=clips_deleted,
        pose_tracks_deleted=pose_tracks_deleted,
        ball_metrics_deleted=ball_metrics_deleted,
        reference_balls_deleted=reference_balls_deleted,
        ball_tracks_deleted=ball_tracks_deleted,
        bounce_estimates_deleted=bounce_estimates_deleted,
        annotations_deleted=annotations_deleted,
        dataset_members_deleted=dataset_members_deleted,
        frame_samples_deleted=frame_samples_deleted,
        evidence_verdicts_deleted=coaching.evidence_verdicts,
        findings_deleted=coaching.findings,
        report_llm_audits_deleted=coaching.report_llm_audits,
        reports_deleted=coaching.reports,
        pipeline_stages_deleted=coaching.pipeline_stages,
        pipeline_runs_deleted=coaching.pipeline_runs,
        bowling_ledger_entries_deleted=coaching.bowling_ledger_entries,
        pain_clearances_deleted=coaching.pain_clearances,
        wellness_checkins_deleted=coaching.wellness_checkins,
        alerts_deleted=coaching.alerts,
        delivery_labels_deleted=delivery_labels_deleted,
        bowling_targets_deleted=bowling_targets_deleted,
        coach_notes_deleted=coach_notes_deleted,
        metric_baselines_scrubbed=metric_baselines_scrubbed,
        multipart_uploads_aborted=multipart_uploads_aborted,
        multipart_uploads_already_gone=multipart_uploads_already_gone,
        multipart_parts_purged=multipart_parts_purged,
        objects_deleted=len(keys),
        notes_cleared=notes_cleared,
        detail=BACKUPS_NOTE,
    )
    counts = report.model_dump(exclude={"session_id", "detail"})
    db.add(
        AuditLog(
            actor=role.value,
            action="privacy_delete",
            entity="session",
            entity_id=str(session_id),
            detail={**counts, "backups": BACKUPS_NOTE},
        )
    )
    return report


@router.post("/exports", status_code=status.HTTP_201_CREATED)
def export_session_tags(
    payload: ExportIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> ExportOut:
    session = db.get(Session, payload.session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")

    tags = db.scalars(
        select(BallTag).where(BallTag.session_id == session.id).order_by(BallTag.ball_no)
    ).all()
    export: dict[str, Any] = {
        "session_id": str(session.id),
        "session_date": session.session_date.isoformat(),
        "exported_at": utcnow().isoformat(),
        "purpose": payload.purpose,
        "tags": [
            {
                "ball_no": tag.ball_no,
                "line": tag.line.value,
                "length": tag.length.value,
                "shot": tag.shot.value,
                "footwork": tag.footwork.value,
                "contact": tag.contact.value,
                "outcome": tag.outcome.value,
                "control": tag.control,
            }
            for tag in tags
        ],
    }
    assert_no_media(export)  # exports carry structured tags only, never media

    watermark = _watermark(export)
    db.add(
        AuditLog(
            actor=role.value,
            action="share_export",
            entity="session",
            entity_id=str(session.id),
            detail={
                "purpose": payload.purpose,
                "watermark_sha256": watermark,
                "tag_count": len(tags),
            },
        )
    )
    return ExportOut(watermark_sha256=watermark, export=export)


@router.get("/audit")
def list_privacy_audit(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> list[AuditOut]:
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.action.in_(PRIVACY_ACTIONS))
        .order_by(AuditLog.at.desc(), AuditLog.id)
    ).all()
    return [AuditOut.model_validate(row) for row in rows]
