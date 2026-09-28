"""US-L3 acceptance: deletion round-trip, watermarked export, privacy audit, RBAC."""

import hashlib
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cricai_data.enums import (
    AlertAudience,
    BlockIntent,
    BowlerSource,
    BowlingVariation,
    ClipStatus,
    Contact,
    DatasetSplit,
    DeliveryIntensity,
    EventSource,
    EvidenceVerdict,
    Footwork,
    LabelClass,
    Length,
    Line,
    MetricPhase,
    Outcome,
    ReportKind,
    Shot,
)
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
    CoachingRule,
    CoachNote,
    Dataset,
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
)
from cricai_data.storage import clip_key, video_key
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return make_test_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _create_session(
    client: TestClient,
    *,
    player_name: str = "Arjun",
    guest: bool = False,
    notes: str | None = "parent note about the child",
) -> str:
    player = client.post(
        "/players",
        json={"name": player_name, "birthdate": "2014-11-20", "is_guest": guest},
        headers=auth(PARENT_TOKEN),
    )
    assert player.status_code == 201
    session = client.post(
        "/sessions",
        json={
            "player_id": player.json()["id"],
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "human",
            "notes": notes,
        },
        headers=auth(PARENT_TOKEN),
    )
    assert session.status_code == 201
    session_id: str = session.json()["id"]
    return session_id


#: Bytes staged on disk for the in-flight multipart upload seeded per session.
STAGED_PARTS: dict[int, bytes] = {1: b"staged part one", 2: b"staged part two"}


def _seed_artifacts(app: FastAPI, session_id: str) -> str:
    """Attach blocks, tags+audits, video/upload rows and stored objects to a session.

    Returns the store upload id of an in-flight multipart upload whose staged
    part bytes privacy deletion must purge from disk.
    """
    sid = uuid.UUID(session_id)
    store = app.state.store
    inflight_store_id: str = store.begin_multipart()
    for part_no, data in STAGED_PARTS.items():
        store.put_part(inflight_store_id, part_no, data)

    factory = app.state.session_factory
    with factory() as db:
        block = SessionBlock(
            session_id=sid,
            block_no=1,
            start_s=0.0,
            bowler_source=BowlerSource.HUMAN,
            intent=BlockIntent.TECHNICAL,
        )
        db.add(block)
        db.flush()
        tag = BallTag(
            session_id=sid,
            ball_no=1,
            block_id=block.id,
            line=Line.OFF,
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
        db.add(
            TagAudit(tag_id=tag.id, actor="coach", field="shot", old_value="cut", new_value="drive")
        )
        db.add(
            BounceMark(
                session_id=sid,
                ball_no=1,
                camera_id="C3",
                frame_no=42,
                px_x=640.0,
                px_y=360.0,
                pitch_x=1.2,
                pitch_y=6.5,
                line=Line.OFF,
                length=Length.GOOD,
            )
        )
        db.add(
            Video(
                session_id=sid,
                camera_id="C1",
                object_key=video_key(session_id, "C1", "c1.mp4"),
                filename="c1.mp4",
                checksum_sha256="ab" * 32,
                size_bytes=4,
            )
        )
        upload = UploadSession(
            session_id=sid,
            camera_id="C2",
            filename="c2.mp4",
            declared_checksum="cd" * 32,
            declared_size=8,
            store_upload_id=inflight_store_id,
        )
        db.add(upload)
        db.flush()
        db.add(UploadPart(upload_id=upload.id, part_no=1, size_bytes=4, checksum_sha256="ef" * 32))
        db.add(UploadPart(upload_id=upload.id, part_no=2, size_bytes=4, checksum_sha256="12" * 32))
        # Completed upload: its staging area was already promoted and removed.
        db.add(
            UploadSession(
                session_id=sid,
                camera_id="C3",
                filename="c3.mp4",
                declared_checksum="34" * 32,
                declared_size=4,
                store_upload_id=uuid.uuid4().hex,
                completed=True,
            )
        )
        # Legacy upload begun before store upload ids were persisted.
        db.add(
            UploadSession(
                session_id=sid,
                camera_id="C4",
                filename="c4.mp4",
                declared_checksum="56" * 32,
                declared_size=4,
            )
        )
        # Phase-3 derived rows: event + its correction, clip, pose track,
        # metrics and reference marking — deletion must remove all of them.
        event = BallEvent(
            session_id=sid,
            ball_no=1,
            start_ms=1000,
            release_ms=1500,
            contact_ms=2000,
            end_ms=3000,
            confidence=0.9,
            source=EventSource.CORRECTED,
            detector_version="cue-segmenter-1.0.0",
        )
        db.add(event)
        db.flush()
        db.add(
            EventCorrection(
                event_id=event.id,
                action="adjust",
                before={"start_ms": 900},
                after={"start_ms": 1000},
                actor="coach",
            )
        )
        db.add(
            Clip(
                session_id=sid,
                ball_no=1,
                camera_id="C1",
                object_key=clip_key(session_id, 1, "C1"),
                start_ms=0,
                end_ms=4500,
                status=ClipStatus.CUT,
            )
        )
        db.add(
            PoseTrack(
                session_id=sid,
                ball_no=1,
                camera_id="C1",
                model_name="mediapipe-pose",
                model_version="0.10.14",
                landmarks_key=f"sessions/{session_id}/balls/1/pose-C1.json",
                frame_count=90,
                availability=1.0,
                subject_confidence=0.9,
            )
        )
        db.add(
            BallMetrics(
                session_id=sid,
                ball_no=1,
                phase=MetricPhase.CONTACT,
                metrics={"head_over_ball": {"value": 1.0, "unit": "ratio", "confidence": 0.8}},
            )
        )
        db.add(
            ReferenceBall(session_id=sid, ball_no=1, label="model cover drive", marked_by="coach")
        )
        # Phase-4 derived rows: track, auto bounce, labeled frame with its
        # annotation and dataset membership — deletion must remove all of them.
        db.add(
            BallTrack(
                session_id=sid,
                ball_no=1,
                camera_id="C1",
                tracker_version="kalman-1.0.0",
                points_key=f"sessions/{session_id}/balls/1/track-C1.json",
                coverage=0.95,
                confidence=0.9,
            )
        )
        db.add(
            BounceEstimate(
                session_id=sid,
                ball_no=1,
                pitch_x=6.0,
                pitch_y=0.2,
                confidence=0.8,
                tracker_version="kalman-1.0.0",
            )
        )
        frame = FrameSample(
            session_id=sid,
            ball_no=1,
            camera_id="C1",
            frame_no=100,
            ts_ms=1500,
            object_key=f"sessions/{session_id}/frames/C1/100.jpg",
            sampler_version="sampler-1.0.0",
        )
        db.add(frame)
        db.flush()
        db.add(
            Annotation(
                frame_id=frame.id,
                label_class=LabelClass.BALL,
                cx=0.5,
                cy=0.4,
                w=0.02,
                h=0.03,
                annotator="coach",
            )
        )
        dataset = db.scalar(select(Dataset).where(Dataset.version == "v1"))
        if dataset is None:
            dataset = Dataset(version="v1")
            db.add(dataset)
            db.flush()
        db.add(DatasetMember(dataset_id=dataset.id, frame_id=frame.id, split=DatasetSplit.TRAIN))
        _seed_phase5_rows(db, sid)
        db.commit()

    store.put(video_key(session_id, "C1", "c1.mp4"), b"raw video bytes")
    store.put(clip_key(session_id, 1, "C1"), b"clip bytes")
    store.put(f"sessions/{session_id}/balls/1/pose-C1.json", b'{"frames": []}')
    store.put(f"sessions/{session_id}/balls/1/track-C1.json", b'{"points": []}')
    store.put(f"sessions/{session_id}/frames/C1/100.jpg", b"frame bytes")
    return inflight_store_id


def _seed_phase5_rows(db: OrmSession, sid: uuid.UUID) -> None:
    """Phase-5 coaching/safety rows: pipeline run + stage, finding with a
    coach verdict, session report with its LLM audit, session-linked ledger
    entry, a pain-flagged check-in with its clearance, a session-scoped quality
    alert (deleted) and a player metric baseline attributed to the session (its
    row survives but the session id is scrubbed from the payload)."""
    player_id = db.scalar(select(Session.player_id).where(Session.id == sid))
    assert player_id is not None
    db.add(
        Alert(
            audience=AlertAudience.PARENT,
            code="low_quality",
            severity="warning",
            detail={"session_id": str(sid), "composite": 0.4},
            session_id=sid,
        )
    )
    db.add(
        MetricBaseline(
            player_id=player_id,
            metric="head_stability_score",
            zone_key="all",
            window="rolling_7",
            snapshot_date=date(2026, 7, 7),
            value=0.8,
            n=12,
            payload={"session_ids": [str(sid)]},
        )
    )
    run = PipelineRun(session_id=sid)
    db.add(run)
    db.flush()
    db.add(PipelineStage(run_id=run.id, stage="events"))
    finding = Finding(
        session_id=sid,
        run_id=run.id,
        agent="analysis",
        rule_key="head_falls_off",
        kind="rule",
        severity="major",
        metric="head_stability_score",
        n=12,
        confidence=0.8,
        ball_ids=[1],
    )
    db.add(finding)
    db.flush()
    db.add(
        EvidenceVerdictRecord(
            finding_id=finding.id, verdict=EvidenceVerdict.CONFIRMS, actor="coach"
        )
    )
    report = Report(
        player_id=player_id,
        session_id=sid,
        kind=ReportKind.DAILY,
        period_start=date(2026, 7, 7),
        period_end=date(2026, 7, 7),
        body={"main_correction": {"finding_id": str(finding.id)}},
    )
    db.add(report)
    db.flush()
    db.add(
        ReportLLMAudit(
            report_id=report.id,
            prompt_key="daily_report",
            request={"findings": []},
            response={"text": "Keep your head still through contact."},
            accepted=True,
        )
    )
    db.add(
        BowlingLedgerEntry(
            player_id=player_id,
            entry_date=date(2026, 7, 7),
            balls=24,
            intensity=DeliveryIntensity.PACE_INTENT,
            session_id=sid,
            source="auto_backfill",
            created_by="worker",
        )
    )
    checkin = WellnessCheckin(
        player_id=player_id,
        session_id=sid,
        checkin_date=date(2026, 7, 7),
        pain=True,
        pain_note="left knee twinge",
        created_by="parent",
    )
    db.add(checkin)
    db.flush()
    db.add(
        PainClearance(
            player_id=player_id,
            checkin_id=checkin.id,
            cleared_by="parent",
            role="parent",
            note="physio cleared after review",
        )
    )
    _seed_phase6_rows(db, sid)


def _seed_phase6_rows(db: OrmSession, sid: uuid.UUID) -> None:
    """Phase-6 session-keyed human input (US-I4/I6/K3): a declared bowling
    target, a delivery label (ball 1) and a session-linked coach note — all
    footage-adjacent, all deleted with the session. Player-scoped coach notes
    (session_id NULL) are seeded elsewhere and must survive."""
    player_id = db.scalar(select(Session.player_id).where(Session.id == sid))
    assert player_id is not None
    db.add(
        BowlingTarget(
            session_id=sid,
            line=Line.OFF,
            length=Length.GOOD,
            description="top of off stump",
            created_by="coach",
        )
    )
    db.add(
        DeliveryLabel(
            session_id=sid,
            ball_no=1,
            variation_intent=BowlingVariation.LEG_BREAK,
            labeler="coach",
        )
    )
    db.add(
        CoachNote(
            player_id=player_id,
            session_id=sid,
            ball_no=1,
            body="lovely loop on that leg break",
            author="coach",
        )
    )


def _row_counts(app: FastAPI, session_id: str) -> dict[str, int]:
    sid = uuid.UUID(session_id)
    factory = app.state.session_factory
    with factory() as db:
        tag_ids = select(BallTag.id).where(BallTag.session_id == sid)
        upload_ids = select(UploadSession.id).where(UploadSession.session_id == sid)
        event_ids = select(BallEvent.id).where(BallEvent.session_id == sid)
        frame_ids = select(FrameSample.id).where(FrameSample.session_id == sid)
        finding_ids = select(Finding.id).where(Finding.session_id == sid)
        report_ids = select(Report.id).where(Report.session_id == sid)
        run_ids = select(PipelineRun.id).where(PipelineRun.session_id == sid)
        checkin_ids = select(WellnessCheckin.id).where(WellnessCheckin.session_id == sid)
        counted: dict[str, Any] = {
            "ball_tags": select(func.count()).where(BallTag.session_id == sid),
            "tag_audits": select(func.count()).where(TagAudit.tag_id.in_(tag_ids)),
            "bounce_marks": select(func.count()).where(BounceMark.session_id == sid),
            "blocks": select(func.count()).where(SessionBlock.session_id == sid),
            "videos": select(func.count()).where(Video.session_id == sid),
            "uploads": select(func.count()).where(UploadSession.session_id == sid),
            "parts": select(func.count()).where(UploadPart.upload_id.in_(upload_ids)),
            "ball_events": select(func.count()).where(BallEvent.session_id == sid),
            "event_corrections": select(func.count()).where(
                EventCorrection.event_id.in_(event_ids)
            ),
            "clips": select(func.count()).where(Clip.session_id == sid),
            "pose_tracks": select(func.count()).where(PoseTrack.session_id == sid),
            "ball_metrics": select(func.count()).where(BallMetrics.session_id == sid),
            "reference_balls": select(func.count()).where(ReferenceBall.session_id == sid),
            "ball_tracks": select(func.count()).where(BallTrack.session_id == sid),
            "bounce_estimates": select(func.count()).where(BounceEstimate.session_id == sid),
            "frame_samples": select(func.count()).where(FrameSample.session_id == sid),
            "annotations": select(func.count()).where(Annotation.frame_id.in_(frame_ids)),
            "dataset_members": select(func.count()).where(DatasetMember.frame_id.in_(frame_ids)),
            "findings": select(func.count()).where(Finding.session_id == sid),
            "evidence_verdicts": select(func.count()).where(
                EvidenceVerdictRecord.finding_id.in_(finding_ids)
            ),
            "reports": select(func.count()).where(Report.session_id == sid),
            "report_llm_audits": select(func.count()).where(
                ReportLLMAudit.report_id.in_(report_ids)
            ),
            "pipeline_runs": select(func.count()).where(PipelineRun.session_id == sid),
            "pipeline_stages": select(func.count()).where(PipelineStage.run_id.in_(run_ids)),
            "bowling_ledger_entries": select(func.count()).where(
                BowlingLedgerEntry.session_id == sid
            ),
            "wellness_checkins": select(func.count()).where(WellnessCheckin.session_id == sid),
            "pain_clearances": select(func.count()).where(
                PainClearance.checkin_id.in_(checkin_ids)
            ),
            "alerts": select(func.count()).where(Alert.session_id == sid),
            "delivery_labels": select(func.count()).where(DeliveryLabel.session_id == sid),
            "bowling_targets": select(func.count()).where(BowlingTarget.session_id == sid),
            "coach_notes": select(func.count()).where(CoachNote.session_id == sid),
        }
        return {name: db.scalar(query) or 0 for name, query in counted.items()}


@pytest.mark.safety
def test_deletion_removes_rows_objects_and_writes_audit(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    session_id = _create_session(client)
    inflight_store_id = _seed_artifacts(app, session_id)
    staging_dir = tmp_path / "storage" / "uploads" / inflight_store_id
    assert staging_dir.is_dir()  # staged part bytes live outside sessions/
    assert app.state.store.list_keys(f"sessions/{session_id}") != []

    # Player-scoped rows without a session link, and global rule config:
    # these describe the player/system, not this session's data — they survive.
    with app.state.session_factory() as db:
        player_id = db.scalar(select(Session.player_id).where(Session.id == uuid.UUID(session_id)))
        assert player_id is not None
        db.add(
            Report(
                player_id=player_id,
                kind=ReportKind.WEEKLY,
                period_start=date(2026, 7, 6),
                period_end=date(2026, 7, 12),
                body={"positive": "three straight sessions"},
            )
        )
        db.add(
            WellnessCheckin(
                player_id=player_id,
                checkin_date=date(2026, 7, 6),
                created_by="parent",
            )
        )
        db.add(
            CoachingRule(
                rule_key="head_falls_off",
                author="coach",
                rationale="head stability drives contact quality",
                definition={"metric": "head_stability_score", "op": "lt", "value": 0.6},
            )
        )
        # A player-scoped coach note (no session link): survives the delete.
        db.add(
            CoachNote(
                player_id=player_id,
                body="general note about the season",
                author="coach",
            )
        )
        db.commit()

    response = client.delete(f"/privacy/sessions/{session_id}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    body = response.json()
    expected_counts = {
        "ball_tags_deleted": 1,
        "tag_audits_deleted": 1,
        "bounce_marks_deleted": 1,
        "blocks_deleted": 1,
        "videos_deleted": 1,
        "upload_sessions_deleted": 3,
        "upload_parts_deleted": 2,
        "event_corrections_deleted": 1,
        "ball_events_deleted": 1,
        "clips_deleted": 1,
        "pose_tracks_deleted": 1,
        "ball_metrics_deleted": 1,
        "reference_balls_deleted": 1,
        "ball_tracks_deleted": 1,
        "bounce_estimates_deleted": 1,
        "annotations_deleted": 1,
        "dataset_members_deleted": 1,
        "frame_samples_deleted": 1,
        "evidence_verdicts_deleted": 1,
        "findings_deleted": 1,
        "report_llm_audits_deleted": 1,
        "reports_deleted": 1,
        "pipeline_stages_deleted": 1,
        "pipeline_runs_deleted": 1,
        "bowling_ledger_entries_deleted": 1,
        "pain_clearances_deleted": 1,
        "wellness_checkins_deleted": 1,
        "alerts_deleted": 1,
        "delivery_labels_deleted": 1,
        "bowling_targets_deleted": 1,
        "coach_notes_deleted": 1,
        "metric_baselines_scrubbed": 1,
        "multipart_uploads_aborted": 1,
        "multipart_uploads_already_gone": 1,
        "multipart_parts_purged": 2,
        "objects_deleted": 5,
    }
    assert {key: body[key] for key in expected_counts} == expected_counts
    assert body["notes_cleared"] is True
    assert "runbook" in body["detail"]

    assert not staging_dir.exists()  # multipart part bytes are gone from disk
    assert app.state.store.list_keys(f"sessions/{session_id}") == []
    assert _row_counts(app, session_id) == {
        "ball_tags": 0,
        "tag_audits": 0,
        "bounce_marks": 0,
        "blocks": 0,
        "videos": 0,
        "uploads": 0,
        "parts": 0,
        "ball_events": 0,
        "event_corrections": 0,
        "clips": 0,
        "pose_tracks": 0,
        "ball_metrics": 0,
        "reference_balls": 0,
        "ball_tracks": 0,
        "bounce_estimates": 0,
        "frame_samples": 0,
        "annotations": 0,
        "dataset_members": 0,
        "findings": 0,
        "evidence_verdicts": 0,
        "reports": 0,
        "report_llm_audits": 0,
        "pipeline_runs": 0,
        "pipeline_stages": 0,
        "bowling_ledger_entries": 0,
        "wellness_checkins": 0,
        "pain_clearances": 0,
        "alerts": 0,
        "delivery_labels": 0,
        "bowling_targets": 0,
        "coach_notes": 0,
    }
    # Player-scoped rows and global rule config survive the session deletion.
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Report)) == 1
        assert db.scalar(select(func.count()).select_from(WellnessCheckin)) == 1
        assert db.scalar(select(func.count()).select_from(CoachingRule)) == 1
        # The session-linked coach note is gone; the player-scoped one survives.
        assert db.scalar(select(func.count()).select_from(CoachNote)) == 1
        # The metric baseline (a player aggregate) survives, de-attributed from
        # the deleted session — its payload no longer references the session id.
        baseline = db.scalars(select(MetricBaseline)).one()
        assert baseline.payload["session_ids"] == []
    fetched = client.get(f"/sessions/{session_id}", headers=auth(PARENT_TOKEN))
    assert fetched.status_code == 200
    assert fetched.json()["notes"] is None

    # Every Phase-3 read surface is empty: nothing derived survives deletion.
    headers = auth(PARENT_TOKEN)
    assert client.get(f"/sessions/{session_id}/events", headers=headers).json() == []
    assert (
        client.get(
            f"/sessions/{session_id}/events", params={"valid": "false"}, headers=headers
        ).json()
        == []
    )
    assert client.get(f"/sessions/{session_id}/clips", headers=headers).json() == []
    assert client.get(f"/sessions/{session_id}/balls/1/metrics", headers=headers).json() == []
    assert client.get("/reference-balls", headers=headers).json() == []

    audit = client.get("/privacy/audit", headers=auth(PARENT_TOKEN))
    assert audit.status_code == 200
    entries = audit.json()
    assert len(entries) == 1
    assert entries[0]["action"] == "privacy_delete"
    assert entries[0]["actor"] == "parent"
    assert entries[0]["entity_id"] == session_id
    detail = entries[0]["detail"]
    assert {key: detail[key] for key in expected_counts} == expected_counts
    assert "runbook" in detail["backups"]


@pytest.mark.safety
def test_deleting_one_session_leaves_guest_session_intact(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    own_id = _create_session(client, player_name="Arjun")
    guest_id = _create_session(client, player_name="Guest Kid", guest=True)
    own_store_id = _seed_artifacts(app, own_id)
    guest_store_id = _seed_artifacts(app, guest_id)

    response = client.delete(f"/privacy/sessions/{own_id}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200

    assert app.state.store.list_keys(f"sessions/{own_id}") == []
    assert not (tmp_path / "storage" / "uploads" / own_store_id).exists()
    own_counts = _row_counts(app, own_id)
    assert own_counts["ball_tags"] == 0
    assert own_counts["ball_events"] == 0
    assert len(app.state.store.list_keys(f"sessions/{guest_id}")) == 5
    # The guest session's in-flight multipart upload parts survive untouched.
    assert (tmp_path / "storage" / "uploads" / guest_store_id).is_dir()
    assert app.state.store.list_parts(guest_store_id) == {
        part_no: len(data) for part_no, data in STAGED_PARTS.items()
    }
    assert _row_counts(app, guest_id) == {
        "ball_tags": 1,
        "tag_audits": 1,
        "bounce_marks": 1,
        "blocks": 1,
        "videos": 1,
        "uploads": 3,
        "parts": 2,
        "ball_events": 1,
        "event_corrections": 1,
        "clips": 1,
        "pose_tracks": 1,
        "ball_metrics": 1,
        "reference_balls": 1,
        "ball_tracks": 1,
        "bounce_estimates": 1,
        "frame_samples": 1,
        "annotations": 1,
        "dataset_members": 1,
        "findings": 1,
        "evidence_verdicts": 1,
        "reports": 1,
        "report_llm_audits": 1,
        "pipeline_runs": 1,
        "pipeline_stages": 1,
        "bowling_ledger_entries": 1,
        "wellness_checkins": 1,
        "pain_clearances": 1,
        "alerts": 1,
        "delivery_labels": 1,
        "bowling_targets": 1,
        "coach_notes": 1,
    }
    # The guest player's baseline is untouched: only the deleted player's
    # baselines are scrubbed, and only of the deleted session's id.
    with app.state.session_factory() as db:
        guest_player_id = db.scalar(
            select(Session.player_id).where(Session.id == uuid.UUID(guest_id))
        )
        guest_baseline = db.scalars(
            select(MetricBaseline).where(MetricBaseline.player_id == guest_player_id)
        ).one()
        assert guest_baseline.payload["session_ids"] == [guest_id]


@pytest.mark.safety
def test_deletion_scrubs_session_from_metric_baseline_payloads(
    app: FastAPI, client: TestClient
) -> None:
    """Baseline aggregates survive (rows stay) but are de-attributed: the
    deleted session id is dropped from ``session_ids`` lists and a singular
    ``session_id`` becomes None; baselines that never referenced it are left
    untouched, and only those actually scrubbed are counted."""
    session_id = _create_session(client, notes=None)
    other = str(uuid.uuid4())
    with app.state.session_factory() as db:
        player_id = db.scalar(select(Session.player_id).where(Session.id == uuid.UUID(session_id)))
        assert player_id is not None
        for metric, payload in (
            ("m_list", {"session_ids": [session_id, other]}),
            ("m_singular", {"session_id": session_id}),
            ("m_unrelated", {"session_ids": [other]}),
        ):
            db.add(
                MetricBaseline(
                    player_id=player_id,
                    metric=metric,
                    zone_key="all",
                    window="rolling_7",
                    snapshot_date=date(2026, 7, 7),
                    value=1.0,
                    n=5,
                    payload=payload,
                )
            )
        db.commit()

    response = client.delete(f"/privacy/sessions/{session_id}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json()["metric_baselines_scrubbed"] == 2  # the unrelated one is untouched

    with app.state.session_factory() as db:
        by_metric = {row.metric: row.payload for row in db.scalars(select(MetricBaseline))}
    assert by_metric["m_list"]["session_ids"] == [other]  # target dropped, sibling kept
    assert by_metric["m_singular"]["session_id"] is None  # singular key de-attributed
    assert by_metric["m_unrelated"]["session_ids"] == [other]  # never referenced -> unchanged


def test_deleting_bare_session_reports_zero_counts(client: TestClient) -> None:
    session_id = _create_session(client, notes=None)
    response = client.delete(f"/privacy/sessions/{session_id}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["ball_tags_deleted"] == 0
    assert body["bounce_marks_deleted"] == 0
    assert body["objects_deleted"] == 0
    assert body["multipart_uploads_aborted"] == 0
    assert body["multipart_uploads_already_gone"] == 0
    assert body["multipart_parts_purged"] == 0
    assert body["event_corrections_deleted"] == 0
    assert body["ball_events_deleted"] == 0
    assert body["clips_deleted"] == 0
    assert body["pose_tracks_deleted"] == 0
    assert body["ball_metrics_deleted"] == 0
    assert body["reference_balls_deleted"] == 0
    assert body["ball_tracks_deleted"] == 0
    assert body["bounce_estimates_deleted"] == 0
    assert body["annotations_deleted"] == 0
    assert body["dataset_members_deleted"] == 0
    assert body["frame_samples_deleted"] == 0
    assert body["findings_deleted"] == 0
    assert body["evidence_verdicts_deleted"] == 0
    assert body["reports_deleted"] == 0
    assert body["report_llm_audits_deleted"] == 0
    assert body["pipeline_runs_deleted"] == 0
    assert body["pipeline_stages_deleted"] == 0
    assert body["bowling_ledger_entries_deleted"] == 0
    assert body["pain_clearances_deleted"] == 0
    assert body["wellness_checkins_deleted"] == 0
    assert body["alerts_deleted"] == 0
    assert body["delivery_labels_deleted"] == 0
    assert body["bowling_targets_deleted"] == 0
    assert body["coach_notes_deleted"] == 0
    assert body["metric_baselines_scrubbed"] == 0
    assert body["notes_cleared"] is False


def test_delete_unknown_session_returns_404(client: TestClient) -> None:
    missing = uuid.uuid4()
    response = client.delete(f"/privacy/sessions/{missing}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


@pytest.mark.safety
def test_export_returns_tags_with_watermark_and_audit(app: FastAPI, client: TestClient) -> None:
    session_id = _create_session(client)
    _seed_artifacts(app, session_id)

    response = client.post(
        "/privacy/exports",
        json={"session_id": session_id, "purpose": "share with academy coach"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    body = response.json()
    export = body["export"]
    assert export["session_id"] == session_id
    assert export["session_date"] == "2026-07-07"
    assert export["purpose"] == "share with academy coach"
    assert export["tags"] == [
        {
            "ball_no": 1,
            "line": "off",
            "length": "good",
            "shot": "drive",
            "footwork": "front",
            "contact": "middle",
            "outcome": "controlled_ground_shot",
            "control": True,
        }
    ]

    dumped = json.dumps(export, sort_keys=True, separators=(",", ":"))
    assert body["watermark_sha256"] == hashlib.sha256(dumped.encode()).hexdigest()
    # No identity, free text or media leaks into the export.
    assert "Arjun" not in dumped
    assert "parent note" not in dumped
    assert "c1.mp4" not in dumped

    audit = client.get("/privacy/audit", headers=auth(PARENT_TOKEN))
    entries = audit.json()
    assert len(entries) == 1
    assert entries[0]["action"] == "share_export"
    assert entries[0]["detail"] == {
        "purpose": "share with academy coach",
        "watermark_sha256": body["watermark_sha256"],
        "tag_count": 1,
    }


def test_export_unknown_session_returns_404(client: TestClient) -> None:
    response = client.post(
        "/privacy/exports",
        json={"session_id": str(uuid.uuid4()), "purpose": "coach review"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 404


def test_audit_lists_only_privacy_actions(app: FastAPI, client: TestClient) -> None:
    session_id = _create_session(client)
    factory = app.state.session_factory
    with factory() as db:
        db.add(AuditLog(actor="parent", action="camera_update", entity="camera", entity_id="C1"))
        db.commit()

    export = client.post(
        "/privacy/exports",
        json={"session_id": session_id, "purpose": "coach review"},
        headers=auth(PARENT_TOKEN),
    )
    assert export.status_code == 201

    audit = client.get("/privacy/audit", headers=auth(PARENT_TOKEN))
    entries = audit.json()
    assert [entry["action"] for entry in entries] == ["share_export"]


@pytest.mark.safety
def test_privacy_endpoints_forbidden_for_coach_and_player(client: TestClient) -> None:
    session_id = _create_session(client)
    for token in (COACH_TOKEN, PLAYER_TOKEN):
        headers = auth(token)
        assert client.delete(f"/privacy/sessions/{session_id}", headers=headers).status_code == 403
        assert (
            client.post(
                "/privacy/exports",
                json={"session_id": session_id, "purpose": "x"},
                headers=headers,
            ).status_code
            == 403
        )
        assert client.get("/privacy/audit", headers=headers).status_code == 403


def test_privacy_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/privacy/audit").status_code == 401
