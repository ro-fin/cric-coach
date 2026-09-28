"""Model CRUD on in-memory SQLite (fast; real-PG parity covered by integration tests)."""

import uuid
from datetime import date

import pytest
from cricai_data.db import create_all, make_engine, make_session_factory, session_scope
from cricai_data.enums import (
    AlertAudience,
    AnnotationSource,
    BlockIntent,
    BowlerSource,
    BowlingVariation,
    CalibrationKind,
    CameraRole,
    ClipStatus,
    Contact,
    DatasetSplit,
    DeliveryIntensity,
    EventSource,
    EvidenceVerdict,
    FindingSeverity,
    Footwork,
    LabelClass,
    Length,
    Line,
    MetricPhase,
    MilestoneKind,
    ModelStage,
    Outcome,
    ReportKind,
    ReportStatus,
    SessionType,
    Shot,
    StageStatus,
    TrainingStatus,
    VideoStatus,
)
from cricai_data.lifecycle import SessionState
from cricai_data.models import (
    EVENT_DEPENDENT_TABLES,
    Alert,
    Annotation,
    AppSetting,
    AuditLog,
    BallEvent,
    BallMetrics,
    BallTag,
    BallTrack,
    Base,
    BounceEstimate,
    BounceMark,
    BowlingLedgerEntry,
    BowlingTarget,
    Calibration,
    CameraConfig,
    ChecklistAck,
    Clip,
    CoachingRule,
    CoachNote,
    Dataset,
    DatasetMember,
    DeliveryLabel,
    Drill,
    DrillPlan,
    EventCorrection,
    EvidenceVerdictRecord,
    Finding,
    FrameSample,
    HealthCheckRecord,
    MetricBaseline,
    Milestone,
    ModelRun,
    ModelVersion,
    PainClearance,
    PipelineRun,
    PipelineStage,
    Player,
    PoseTrack,
    ReferenceBall,
    Report,
    ReportLLMAudit,
    RuleOverride,
    SafetyConfig,
    Session,
    SessionBlock,
    TagAudit,
    UploadPart,
    UploadSession,
    Video,
    WellnessCheckin,
)
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool


@pytest.fixture
def engine() -> Engine:
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(eng)
    return eng


def _player() -> Player:
    return Player(name="Arjun", birthdate=date(2014, 11, 20))


def test_make_engine_helper() -> None:
    assert make_engine("sqlite://").dialect.name == "sqlite"


def test_full_session_graph_roundtrip(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = _player()
        ack = ChecklistAck(items={"netting": True, "helmet": True}, acked_by="parent")
        session = Session(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            machine_settings={"speed_kph": 85.0, "length": "good"},
            checklist_ack=ack,
        )
        block = SessionBlock(
            session=session,
            block_no=1,
            start_s=0.0,
            end_s=900.0,
            bowler_source=BowlerSource.MACHINE,
            intent=BlockIntent.TECHNICAL,
        )
        video = Video(
            session=session,
            camera_id="C1",
            object_key="sessions/s/C1/a.mp4",
            filename="a.mp4",
            checksum_sha256="ab" * 32,
            size_bytes=3_000_000_000,  # >int32: BigInteger required
        )
        tag = BallTag(
            session=session,
            ball_no=1,
            block_id=None,
            line=Line.OUTSIDE_OFF,
            length=Length.FULL,
            shot=Shot.COVER_DRIVE,
            footwork=Footwork.FRONT,
            contact=Contact.MIDDLE,
            outcome=Outcome.CONTROLLED_GROUND_SHOT,
            control=True,
            created_by="parent",
        )
        db.add_all([player, session, block, video, tag])

    with session_scope(factory) as db:
        loaded = db.scalars(select(Session)).one()
        assert loaded.state is SessionState.CREATED  # default
        assert loaded.degraded is False
        assert loaded.missing_views == []
        assert loaded.player.handedness.value == "right"
        assert loaded.checklist_ack is not None
        assert loaded.checklist_ack.items == {"netting": True, "helmet": True}
        assert [b.block_no for b in loaded.blocks] == [1]
        assert loaded.videos[0].status is VideoStatus.PENDING
        assert loaded.videos[0].size_bytes == 3_000_000_000
        assert loaded.ball_tags[0].ground_truth_eligible is True
        assert loaded.ball_tags[0].source == "manual"


def test_video_dedupe_constraint(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = _player()
        session = Session(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.COACH,
        )
        db.add(session)
        db.flush()
        sid = session.id

    def add_video(key: str) -> None:
        with session_scope(factory) as db:
            db.add(
                Video(
                    session_id=sid,
                    camera_id="C1",
                    object_key=key,
                    filename="a.mp4",
                    checksum_sha256="cd" * 32,
                    size_bytes=10,
                )
            )

    add_video("sessions/s/C1/a.mp4")
    with pytest.raises(IntegrityError):
        add_video("sessions/s/C1/b.mp4")  # same checksum in same session


def test_tag_uniqueness_and_audit_chain(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = _player()
        session = Session(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.COACH,
        )
        tag = BallTag(
            session=session,
            ball_no=7,
            line=Line.OFF,
            length=Length.GOOD,
            shot=Shot.DEFEND,
            footwork=Footwork.BACK,
            contact=Contact.MIDDLE,
            outcome=Outcome.CONTROLLED_GROUND_SHOT,
            control=True,
            created_by="coach",
        )
        tag.audits.append(
            TagAudit(actor="coach", field="line", old_value="off", new_value="outside_off")
        )
        db.add_all([session, tag])
        db.flush()
        sid = session.id

    with session_scope(factory) as db:
        loaded = db.scalars(select(BallTag)).one()
        assert loaded.audits[0].old_value == "off"

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            BallTag(
                session_id=sid,
                ball_no=7,
                line=Line.OFF,
                length=Length.GOOD,
                shot=Shot.DEFEND,
                footwork=Footwork.BACK,
                contact=Contact.MIDDLE,
                outcome=Outcome.CONTROLLED_GROUND_SHOT,
                control=True,
                created_by="parent",
            )
        )


def test_upload_session_parts(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = _player()
        session = Session(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.MIXED,
            bowler_source=BowlerSource.HUMAN,
        )
        upload = UploadSession(
            session_id=session.id,
            camera_id="C2",
            filename="c2.mp4",
            declared_checksum="ef" * 32,
            declared_size=5_000_000_000,
        )
        db.add_all([player, session])
        db.flush()
        upload.session_id = session.id
        upload.parts.append(UploadPart(part_no=1, size_bytes=1024, checksum_sha256="11" * 32))
        db.add(upload)

    with session_scope(factory) as db:
        loaded = db.scalars(select(UploadSession)).one()
        assert loaded.completed is False
        assert [p.part_no for p in loaded.parts] == [1]


def test_camera_era_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)

    def add(era: int) -> None:
        with session_scope(factory) as db:
            db.add(
                CameraConfig(
                    camera_id="C1",
                    era_no=era,
                    position_label="side-on batting",
                    xyz_offset_m={"x": 0.0, "y": 5.0, "z": 0.0},
                    height_m=1.4,
                    fps=120,
                    resolution="1920x1080",
                )
            )

    add(1)
    add(2)
    with pytest.raises(IntegrityError):
        add(2)


def test_health_and_audit_records(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        db.add(HealthCheckRecord(passed=False, results={"C1": "dark_frame"}))
        db.add(
            AuditLog(
                actor="parent",
                action="delete",
                entity="video",
                entity_id=str(uuid.uuid4()),
                detail={"reason": "privacy request"},
            )
        )

    with session_scope(factory) as db:
        health = db.scalars(select(HealthCheckRecord)).one()
        assert health.passed is False
        assert health.session_id is None
        audit = db.scalars(select(AuditLog)).one()
        assert audit.detail == {"reason": "privacy request"}


def test_calibration_roundtrip_and_session_link(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = _player()
        calibration = Calibration(
            camera_id="C1",
            era_no=1,
            kind=CalibrationKind.EXTRINSIC,
            params={"homography": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]},
            rms=0.42,
        )
        db.add_all([player, calibration])
        db.flush()
        db.add(
            Session(
                player_id=player.id,
                session_date=date(2026, 7, 7),
                session_type=SessionType.BATTING,
                bowler_source=BowlerSource.MACHINE,
                calibration_id=calibration.id,
            )
        )

    with session_scope(factory) as db:
        session = db.scalars(select(Session)).one()
        assert session.calibration is not None
        assert session.calibration.kind is CalibrationKind.EXTRINSIC
        assert session.calibration.params["homography"][0] == [1.0, 0.0, 0.0]
        assert session.calibration.rms == 0.42
        assert session.calibration.valid is True  # fresh calibrations are trusted by default
        assert session.calibration_suspect is False  # drift not yet detected (US-C4)

        intrinsic = Calibration(
            camera_id="C1",
            era_no=1,
            kind=CalibrationKind.INTRINSIC,
            params={"camera_matrix": [[1000.0, 0.0, 960.0]]},
        )
        db.add(intrinsic)

    with session_scope(factory) as db:
        kinds = {c.kind for c in db.scalars(select(Calibration)).all()}
        assert kinds == {CalibrationKind.INTRINSIC, CalibrationKind.EXTRINSIC}
        no_rms = db.scalars(
            select(Calibration).where(Calibration.kind == CalibrationKind.INTRINSIC)
        ).one()
        assert no_rms.rms is None


def test_bounce_mark_roundtrip_and_per_camera_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = _player()
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        session_id = session.id
        db.add(
            BounceMark(
                session_id=session_id,
                ball_no=1,
                camera_id="C3",
                frame_no=118,
                px_x=612.0,
                px_y=402.5,
                pitch_x=6.4,
                pitch_y=0.12,
                line=Line.OFF,
                length=Length.GOOD,
            )
        )

    with session_scope(factory) as db:
        mark = db.scalars(select(BounceMark)).one()
        assert mark.line is Line.OFF
        assert mark.length is Length.GOOD
        assert mark.flagged_for_review is False
        assert mark.calibration_id is None
        # same ball re-marked from the OTHER camera is allowed (cross-camera agreement)
        db.add(
            BounceMark(
                session_id=session_id,
                ball_no=1,
                camera_id="C4",
                frame_no=118,
                px_x=580.0,
                px_y=410.0,
                pitch_x=6.31,
                pitch_y=0.18,
            )
        )

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        # but a duplicate click for the same (session, ball, camera) violates uniqueness
        db.add(
            BounceMark(
                session_id=session_id,
                ball_no=1,
                camera_id="C3",
                frame_no=119,
                px_x=613.0,
                px_y=403.0,
                pitch_x=6.41,
                pitch_y=0.13,
            )
        )


def _seeded_session(db: OrmSession) -> Session:
    """Create player+session inside the caller's session_scope."""
    player = _player()
    db.add(player)
    db.flush()
    session = Session(
        player_id=player.id,
        session_date=date(2026, 7, 7),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add(session)
    db.flush()
    return session


def test_ball_event_roundtrip_correction_and_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        session = _seeded_session(db)
        session_id = session.id
        event = BallEvent(
            session_id=session_id,
            ball_no=1,
            start_ms=10_000,
            release_ms=10_400,
            contact_ms=None,  # a left ball still gets an event (US-D1)
            end_ms=12_000,
            confidence=0.93,
        )
        db.add(event)
        db.flush()
        db.add(
            EventCorrection(
                event_id=event.id,
                action="adjust",
                before={"release_ms": 10_400},
                after={"release_ms": 10_385},
                actor="parent",
            )
        )

    with session_scope(factory) as db:
        event = db.scalars(select(BallEvent)).one()
        assert event.source is EventSource.AUTO
        assert event.contact_ms is None
        assert event.valid is True
        correction = db.scalars(select(EventCorrection)).one()
        assert correction.before == {"release_ms": 10_400}
        assert correction.after == {"release_ms": 10_385}

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            BallEvent(
                session_id=session_id,
                ball_no=1,
                start_ms=0,
                release_ms=1,
                end_ms=2,
                confidence=0.5,
            )
        )


def test_clip_gap_semantics_and_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        session = _seeded_session(db)
        session_id = session.id
        db.add(
            Clip(
                session_id=session_id,
                ball_no=1,
                camera_id="C1",
                object_key=f"sessions/{session_id}/balls/1/C1.mp4",
                start_ms=8500,
                end_ms=13_500,
                status=ClipStatus.CUT,
            )
        )
        db.add(
            Clip(  # missing camera recorded loudly, not silently absent (US-D2)
                session_id=session_id,
                ball_no=1,
                camera_id="C2",
                object_key=None,
                start_ms=8500,
                end_ms=13_500,
                status=ClipStatus.GAP,
                error="camera C2 had no footage for this window",
            )
        )

    with session_scope(factory) as db:
        gap = db.scalars(select(Clip).where(Clip.status == ClipStatus.GAP)).one()
        assert gap.object_key is None
        assert gap.error is not None

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            Clip(
                session_id=session_id,
                ball_no=1,
                camera_id="C1",
                start_ms=0,
                end_ms=1,
            )
        )


def test_pose_track_metrics_and_reference_rows(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        session = _seeded_session(db)
        session_id = session.id
        db.add(
            PoseTrack(
                session_id=session_id,
                ball_no=1,
                camera_id="C1",
                model_name="fake-pose",
                model_version="1",
                landmarks_key=f"sessions/{session_id}/balls/1/pose-C1.json",
                frame_count=240,
                availability=0.97,
                subject_confidence=0.99,
            )
        )
        db.add(
            BallMetrics(
                session_id=session_id,
                ball_no=1,
                phase=MetricPhase.PRE_RELEASE,
                metrics={
                    "stance_width_cm": {"value": 42.0, "unit": "cm", "confidence": 0.9},
                    "trigger_start_ms": {
                        "value": None,
                        "unit": "ms",
                        "confidence": 0.2,
                        "reason": "pose availability below threshold",
                    },
                },
            )
        )
        db.add(
            ReferenceBall(
                session_id=session_id,
                ball_no=1,
                label="model straight drive",
                marked_by="coach",
            )
        )

    with session_scope(factory) as db:
        track = db.scalars(select(PoseTrack)).one()
        assert track.availability == pytest.approx(0.97)
        metrics = db.scalars(select(BallMetrics)).one()
        assert metrics.phase is MetricPhase.PRE_RELEASE
        assert metrics.schema_version == 1
        assert metrics.metrics["trigger_start_ms"]["value"] is None
        assert metrics.metrics["trigger_start_ms"]["reason"]  # nullable-with-reason (US-E2)
        reference = db.scalars(select(ReferenceBall)).one()
        assert reference.label == "model straight drive"

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            BallMetrics(
                session_id=session_id,
                ball_no=1,
                phase=MetricPhase.PRE_RELEASE,
                metrics={},
            )
        )


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with pytest.raises(RuntimeError, match="boom"), session_scope(factory) as db:
        db.add(_player())
        raise RuntimeError("boom")
    with session_scope(factory) as db:
        assert db.scalars(select(Player)).all() == []


def test_labeling_dataset_graph_roundtrip(engine: Engine) -> None:
    """US-F1: frame -> annotation -> dataset membership with provenance intact."""
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        session = _seeded_session(db)
        session_id = session.id
        frame = FrameSample(
            session_id=session_id,
            ball_no=3,
            camera_id="C1",
            frame_no=1200,
            ts_ms=48000,
            object_key=f"sessions/{session_id}/frames/C1/1200.jpg",
            stratum={"lighting": "evening", "speed_band": "fast", "intent": "technical"},
            sampler_version="sampler-1.0.0",
        )
        db.add(frame)
        db.flush()
        db.add(
            Annotation(
                frame_id=frame.id,
                label_class=LabelClass.BALL,
                cx=0.51,
                cy=0.42,
                w=0.02,
                h=0.025,
                annotator="parent",
                source=AnnotationSource.IMPORTED,
            )
        )
        dataset = Dataset(version="2026.07-v1", notes="first labeled batch")
        db.add(dataset)
        db.flush()
        db.add(DatasetMember(dataset_id=dataset.id, frame_id=frame.id, split=DatasetSplit.TEST))

    with session_scope(factory) as db:
        stored_frame = db.scalars(select(FrameSample)).one()
        assert stored_frame.ball_no == 3  # provenance back to the ball (US-F1)
        assert stored_frame.stratum["lighting"] == "evening"
        annotation = db.scalars(select(Annotation)).one()
        assert annotation.frame_id == stored_frame.id
        assert annotation.source is AnnotationSource.IMPORTED
        member = db.scalars(select(DatasetMember)).one()
        assert member.split is DatasetSplit.TEST
        stored_dataset = db.scalars(select(Dataset)).one()
        assert stored_dataset.frozen is False
        assert stored_dataset.manifest_digest is None

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            FrameSample(
                session_id=session_id,
                ball_no=None,
                camera_id="C1",
                frame_no=1200,
                ts_ms=48000,
                object_key="dup",
                sampler_version="sampler-1.0.0",
            )
        )


def test_dataset_member_uniqueness_and_version_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        session = _seeded_session(db)
        frame = FrameSample(
            session_id=session.id,
            ball_no=None,
            camera_id="C2",
            frame_no=1,
            ts_ms=40,
            object_key="k",
            sampler_version="s1",
        )
        dataset = Dataset(version="v-unique")
        db.add_all([frame, dataset])
        db.flush()
        frame_id, dataset_id = frame.id, dataset.id
        db.add(DatasetMember(dataset_id=dataset_id, frame_id=frame_id, split=DatasetSplit.TRAIN))

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(DatasetMember(dataset_id=dataset_id, frame_id=frame_id, split=DatasetSplit.VAL))

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(Dataset(version="v-unique"))


def test_model_registry_roundtrip_and_version_uniqueness(engine: Engine) -> None:
    """US-F2: run -> version chain with pinned headline metric keys."""
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        dataset = Dataset(version="reg-v1", frozen=True, manifest_digest="ab" * 32)
        db.add(dataset)
        db.flush()
        run = ModelRun(
            model_name="ball-detector",
            dataset_id=dataset.id,
            config={"epochs": 100, "imgsz": 1280},
            metrics={"map50_ball": 0.87, "recall_ball_high_blur": 0.71},
            status=TrainingStatus.SUCCEEDED,
            trainer_version="fake-train-1",
        )
        db.add(run)
        db.flush()
        run_id = run.id
        db.add(ModelVersion(model_name="ball-detector", version="1.0.0", run_id=run_id))

    with session_scope(factory) as db:
        stored_run = db.scalars(select(ModelRun)).one()
        assert stored_run.status is TrainingStatus.SUCCEEDED
        assert stored_run.metrics["map50_ball"] == pytest.approx(0.87)
        assert stored_run.report_key is None
        version = db.scalars(select(ModelVersion)).one()
        assert version.stage is ModelStage.CANDIDATE
        assert version.promoted_at is None

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(ModelVersion(model_name="ball-detector", version="1.0.0", run_id=run_id))


def test_ball_track_and_bounce_estimate_rows(engine: Engine) -> None:
    """US-F3/F4: track pointer + auto bounce with per-ball uniqueness."""
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        session = _seeded_session(db)
        session_id = session.id
        db.add(
            BallTrack(
                session_id=session_id,
                ball_no=1,
                camera_id="C1",
                tracker_version="kalman-1.0.0",
                points_key=f"sessions/{session_id}/balls/1/track-C1.json",
                coverage=0.93,
                segments=[
                    {"kind": "pre_bounce", "start_ms": 1000, "end_ms": 1400, "confidence": 0.95}
                ],
                flags={"identity_risk": False, "long_gap": False},
                confidence=0.9,
            )
        )
        db.add(
            BounceEstimate(
                session_id=session_id,
                ball_no=1,
                pitch_x=6.2,
                pitch_y=0.15,
                line=Line.OFF,
                length=Length.GOOD,
                confidence=0.82,
                tracker_version="kalman-1.0.0",
            )
        )

    with session_scope(factory) as db:
        track = db.scalars(select(BallTrack)).one()
        assert track.coverage == pytest.approx(0.93)
        assert track.segments[0]["kind"] == "pre_bounce"
        assert track.flags == {"identity_risk": False, "long_gap": False}
        estimate = db.scalars(select(BounceEstimate)).one()
        assert estimate.line is Line.OFF
        assert estimate.length is Length.GOOD

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            BallTrack(
                session_id=session_id,
                ball_no=1,
                camera_id="C1",
                tracker_version="kalman-2.0.0",
                points_key="other",
                coverage=0.5,
                confidence=0.5,
            )
        )

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            BounceEstimate(
                session_id=session_id,
                ball_no=1,
                pitch_x=5.0,
                pitch_y=0.1,
                confidence=0.5,
                tracker_version="kalman-2.0.0",
            )
        )


def test_event_dependent_registry_covers_every_ball_keyed_table() -> None:
    """US-D4/F1: every table joining events on (session_id, ball_no) with no FK
    to ``ball_events.id`` must be in ``EVENT_DEPENDENT_TABLES`` — a missing
    entry means the renumbering and re-detection guards skip it, silently
    corrupting its ball provenance (frame_samples was the Phase-4 escapee)."""
    ball_keyed = {
        mapper.class_
        for mapper in Base.registry.mappers
        if mapper.class_ is not BallEvent
        and {"session_id", "ball_no"}.issubset(mapper.local_table.c.keys())
    }
    assert {model for model, _ in EVENT_DEPENDENT_TABLES} == ball_keyed
    labels = [label for _, label in EVENT_DEPENDENT_TABLES]
    assert len(set(labels)) == len(labels)  # guard 409s name blockers by label


def _seed_player_session(db: OrmSession) -> tuple[Player, Session]:
    player = _player()
    session = Session(
        player=player,
        session_date=date(2026, 7, 7),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add(session)
    db.flush()
    return player, session


def _seed_phase5_graph(db: OrmSession) -> None:
    """One row of every Phase-5 table, hanging off one player + session."""
    player, session = _seed_player_session(db)
    db.add(
        CoachingRule(
            rule_key="head_falls_off",
            author="coach",
            approved_by="parent",
            rationale="head stability drives contact quality",
            definition={"metric": "head_stability_score", "op": "lt", "value": 0.6},
        )
    )
    db.add(
        RuleOverride(
            rule_key="head_falls_off",
            player_id=player.id,
            action="adjust",
            params={"value": 0.5},
            reason="recovering from a growth spurt",
            actor="coach",
        )
    )
    db.add(
        SafetyConfig(
            version=1,
            config={"workload": {"balls_per_over": 6}},
            approved_by="coach",
            reason="seed",
        )
    )
    run = PipelineRun(session_id=session.id, detector_context={"detector": "yolo-1.2.0"})
    db.add(run)
    db.flush()
    db.add(PipelineStage(run_id=run.id, stage="events"))
    finding = Finding(
        session_id=session.id,
        run_id=run.id,
        agent="analysis",
        rule_key="head_falls_off",
        kind="rule",
        severity=FindingSeverity.MAJOR.value,
        metric="head_stability_score",
        condition={"line": "outside_off"},
        n=14,
        effect_size=0.8,
        confidence=0.9,
        ball_ids=[3, 7, 11],
        evidence={"3": {"C1": "clip-3-C1"}},
    )
    db.add(finding)
    db.flush()
    db.add(
        EvidenceVerdictRecord(
            finding_id=finding.id, verdict=EvidenceVerdict.CONFIRMS, actor="coach"
        )
    )
    report = Report(
        player_id=player.id,
        session_id=session.id,
        kind=ReportKind.DAILY,
        period_start=date(2026, 7, 7),
        period_end=date(2026, 7, 7),
        body={"main_correction": {"finding_id": str(finding.id)}},
        safety_sha256="ab" * 32,
        quality={"composite": 0.9},
    )
    db.add(report)
    db.flush()
    db.add(
        ReportLLMAudit(
            report_id=report.id,
            prompt_key="daily_report",
            request={"findings": [str(finding.id)]},
            response={"text": "Keep your head still through contact."},
            accepted=True,
        )
    )
    drill = Drill(
        name="single stump drive",
        setup="single stump, cone at cover",
        machine_settings={"speed_kph": 80, "length": "full"},
        ball_count=30,
        target_metric="head_stability_score",
        intent=BlockIntent.TECHNICAL,
        author="coach",
    )
    db.add(drill)
    db.flush()
    db.add(
        DrillPlan(
            player_id=player.id,
            plan_date=date(2026, 7, 8),
            blocks=[{"intent": "technical", "balls": 150, "drill_id": str(drill.id)}],
            finding_ids=[str(finding.id)],
            safety={"active": False, "codes": []},
            created_by="planner",
        )
    )
    db.add(
        BowlingLedgerEntry(
            player_id=player.id,
            entry_date=date(2026, 7, 7),
            balls=24,
            intensity=DeliveryIntensity.PACE_INTENT,
            session_id=session.id,
            source="auto_backfill",
            created_by="worker",
        )
    )
    checkin = WellnessCheckin(
        player_id=player.id,
        checkin_date=date(2026, 7, 7),
        soreness={"knee": 2},
        energy=4,
        sleep_hours=8.5,
        pain=True,
        pain_note="left knee twinge",
        created_by="parent",
    )
    db.add(checkin)
    db.flush()
    db.add(
        PainClearance(
            player_id=player.id,
            checkin_id=checkin.id,
            cleared_by="parent",
            role="parent",
            note="physio cleared after review",
        )
    )
    db.add(
        MetricBaseline(
            player_id=player.id,
            metric="control_pct",
            zone_key="outside_off:good",
            window="30d",
            snapshot_date=date(2026, 7, 7),
            value=0.61,
            n=120,
        )
    )
    db.add(Alert(audience=AlertAudience.DEVELOPER, code="drift", severity="warning"))


def test_phase5_coaching_graph_roundtrip(engine: Engine) -> None:
    """US-G2/G3/H1/H4/J1-J4/L4: one row of every Phase-5 table, defaults pinned."""
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        _seed_phase5_graph(db)

    with session_scope(factory) as db:
        rule = db.scalars(select(CoachingRule)).one()
        assert rule.version == 1  # append-only versioning starts at 1
        assert rule.enabled is True
        loaded_run = db.scalars(select(PipelineRun)).one()
        assert loaded_run.status is StageStatus.PENDING
        assert loaded_run.finished_at is None
        stage = db.scalars(select(PipelineStage)).one()
        assert stage.status is StageStatus.PENDING
        assert stage.attempt == 1
        assert stage.input_digest is None
        loaded_finding = db.scalars(select(Finding)).one()
        assert loaded_finding.severity == "major"
        assert loaded_finding.ball_ids == [3, 7, 11]
        assert loaded_finding.payload == {}
        loaded_report = db.scalars(select(Report)).one()
        assert loaded_report.kind is ReportKind.DAILY
        assert loaded_report.status is ReportStatus.DRAFT
        assert db.scalars(select(ReportLLMAudit)).one().accepted is True
        assert db.scalars(select(Drill)).one().enabled is True
        plan = db.scalars(select(DrillPlan)).one()
        assert plan.safety_sha256 is None
        ledger = db.scalars(select(BowlingLedgerEntry)).one()
        assert ledger.intensity is DeliveryIntensity.PACE_INTENT
        assert ledger.note is None
        loaded_checkin = db.scalars(select(WellnessCheckin)).one()
        assert loaded_checkin.pain is True
        assert loaded_checkin.session_id is None
        clearance = db.scalars(select(PainClearance)).one()
        assert clearance.checkin_id == loaded_checkin.id
        assert db.scalars(select(MetricBaseline)).one().payload == {}
        alert = db.scalars(select(Alert)).one()
        assert alert.audience is AlertAudience.DEVELOPER
        assert alert.detail == {}
        assert alert.acknowledged is False
        assert alert.session_id is None
        verdict = db.scalars(select(EvidenceVerdictRecord)).one()
        assert verdict.verdict is EvidenceVerdict.CONFIRMS
        assert verdict.note is None


def test_coaching_rule_and_safety_config_version_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        db.add(CoachingRule(rule_key="r", version=1, author="a", rationale="x", definition={}))
        db.add(SafetyConfig(version=1, config={}, approved_by="coach", reason="seed"))

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(CoachingRule(rule_key="r", version=1, author="b", rationale="dup", definition={}))
    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(SafetyConfig(version=1, config={}, approved_by="coach", reason="dup"))


def test_report_and_drill_plan_period_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player, _ = _seed_player_session(db)
        player_id = player.id
        db.add(
            Report(
                player_id=player_id,
                kind=ReportKind.WEEKLY,
                period_start=date(2026, 7, 6),
                period_end=date(2026, 7, 12),
                body={},
            )
        )
        db.add(DrillPlan(player_id=player_id, plan_date=date(2026, 7, 8), created_by="planner"))

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            Report(
                player_id=player_id,
                kind=ReportKind.WEEKLY,
                period_start=date(2026, 7, 6),
                period_end=date(2026, 7, 12),
                body={"other": True},
            )
        )
    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(DrillPlan(player_id=player_id, plan_date=date(2026, 7, 8), created_by="coach"))


def test_stage_attempt_baseline_slice_and_drill_name_uniqueness(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player, session = _seed_player_session(db)
        player_id = player.id
        run = PipelineRun(session_id=session.id)
        db.add(run)
        db.flush()
        run_id = run.id
        db.add(PipelineStage(run_id=run_id, stage="events", attempt=1))
        db.add(PipelineStage(run_id=run_id, stage="events", attempt=2))  # retries stack
        db.add(
            MetricBaseline(
                player_id=player_id,
                metric="control_pct",
                zone_key="outside_off:good",
                window="30d",
                snapshot_date=date(2026, 7, 7),
                value=0.61,
                n=120,
            )
        )
        db.add(
            Drill(
                name="single stump drive",
                setup="s",
                ball_count=30,
                target_metric="m",
                intent=BlockIntent.TECHNICAL,
                author="coach",
            )
        )

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(PipelineStage(run_id=run_id, stage="events", attempt=2))
    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            MetricBaseline(
                player_id=player_id,
                metric="control_pct",
                zone_key="outside_off:good",
                window="30d",
                snapshot_date=date(2026, 7, 7),
                value=0.7,
                n=10,
            )
        )
    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            Drill(
                name="single stump drive",
                setup="other",
                ball_count=10,
                target_metric="m2",
                intent=BlockIntent.DECISION,
                author="coach",
            )
        )


def _auto_backfill_entry(
    player_id: uuid.UUID, session_id: uuid.UUID, source: str
) -> BowlingLedgerEntry:
    return BowlingLedgerEntry(
        player_id=player_id,
        entry_date=date(2026, 7, 7),
        balls=24,
        intensity=DeliveryIntensity.PACE_INTENT,
        session_id=session_id,
        source=source,
        created_by="backfill_workload",
    )


def test_ledger_auto_backfill_backstop_is_postgresql_only(engine: Engine) -> None:
    """US-H1: ``uq_ledger_auto_backfill_session_intensity`` is PostgreSQL-only
    DDL (``ddl_if``) — SQLite unit DBs stay permissive so the backfill job's
    application-level delete-then-insert guard remains what unit tests
    exercise, and duplicated manual rows never trip anything on any dialect.
    The PG side is pinned by the migrations integration test."""
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player, session = _seed_player_session(db)
        db.add_all(
            [
                _auto_backfill_entry(player.id, session.id, "auto_backfill"),
                _auto_backfill_entry(player.id, session.id, "auto_backfill"),
                _auto_backfill_entry(player.id, session.id, "manual"),
                _auto_backfill_entry(player.id, session.id, "manual"),
            ]
        )

    with session_scope(factory) as db:
        assert len(db.scalars(select(BowlingLedgerEntry)).all()) == 4


def test_phase6_graph_roundtrip_and_camera_role(engine: Engine) -> None:
    """US-I1/I4/I6/K3/K4/J5: one row of every Phase-6 table plus a camera role."""
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player, session = _seed_player_session(db)
        block = SessionBlock(
            session_id=session.id,
            block_no=1,
            start_s=0.0,
            bowler_source=BowlerSource.HUMAN,
            intent=BlockIntent.SPIN_SPECIFIC,
        )
        db.add(block)
        db.add(
            CameraConfig(
                camera_id="C5",
                position_label="wide bowling-side",
                role=CameraRole.BOWLING_SIDE,
                xyz_offset_m={"x": 0.0, "y": 0.0, "z": 0.0},
                height_m=2.0,
                fps=120,
                resolution="1920x1080",
            )
        )
        db.flush()
        db.add(
            BowlingTarget(
                session_id=session.id,
                block_id=block.id,
                line=Line.OFF,
                length=Length.GOOD,
                description="top of off stump",
                created_by="coach",
            )
        )
        db.add(
            DeliveryLabel(
                session_id=session.id,
                ball_no=1,
                variation_intent=BowlingVariation.GOOGLY,
                variation_detected=BowlingVariation.LEG_BREAK,
                labeler="coach",
            )
        )
        db.add(
            CoachNote(
                player_id=player.id,
                session_id=session.id,
                ball_no=1,
                body="lovely loop on that googly",
                author="coach",
                visibility="shared",
            )
        )
        db.add(
            Milestone(
                player_id=player.id,
                kind=MilestoneKind.PERSONAL_BEST,
                metric="target_accuracy_pct",
                value=72.0,
                context={"n": 60},
                achieved_on=date(2026, 7, 7),
            )
        )
        db.add(
            AppSetting(
                version=1,
                settings={"report_review": {"mode": "auto_publish"}},
                approved_by="bootstrap",
                reason="seed",
            )
        )

    with session_scope(factory) as db:
        label = db.scalars(select(DeliveryLabel)).one()
        assert label.variation_intent is BowlingVariation.GOOGLY
        assert label.variation_detected is BowlingVariation.LEG_BREAK
        assert label.source == "manual"  # column default applied on insert
        camera = db.scalars(select(CameraConfig).where(CameraConfig.camera_id == "C5")).one()
        assert camera.role is CameraRole.BOWLING_SIDE
        note = db.scalars(select(CoachNote)).one()
        assert note.visibility == "shared"
        assert db.scalars(select(BowlingTarget)).one().line is Line.OFF
        assert db.scalars(select(Milestone)).one().kind is MilestoneKind.PERSONAL_BEST


def test_phase6_uniqueness_constraints(engine: Engine) -> None:
    """delivery_labels (session, ball), milestones (player, kind, metric, day)
    and app_settings.version are each unique (US-I6/K4/J5)."""
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player, session = _seed_player_session(db)
        player_id, session_id = player.id, session.id
        db.add(
            DeliveryLabel(
                session_id=session_id,
                ball_no=1,
                variation_intent=BowlingVariation.LEG_BREAK,
                labeler="coach",
            )
        )
        db.add(
            Milestone(
                player_id=player_id,
                kind=MilestoneKind.VOLUME,
                metric="balls_bowled",
                value=100.0,
                achieved_on=date(2026, 7, 7),
            )
        )
        db.add(
            AppSetting(version=1, settings={}, approved_by="bootstrap", reason="seed"),
        )

    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            DeliveryLabel(
                session_id=session_id,
                ball_no=1,
                variation_intent=BowlingVariation.GOOGLY,
                labeler="coach",
            )
        )
    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(
            Milestone(
                player_id=player_id,
                kind=MilestoneKind.VOLUME,
                metric="balls_bowled",
                value=200.0,
                achieved_on=date(2026, 7, 7),
            )
        )
    with pytest.raises(IntegrityError), session_scope(factory) as db:
        db.add(AppSetting(version=1, settings={}, approved_by="bootstrap", reason="dup"))
