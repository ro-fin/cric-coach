"""SQLAlchemy 2 typed ORM models — Phase 1 schema (Epics A & B, US-L3).

Enum-valued columns store the canonical string values from ``cricai_data.enums``;
JSON columns are portable across PostgreSQL (production) and SQLite (fast unit
tests). Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
    Footwork,
    Handedness,
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


def utcnow() -> datetime:
    return datetime.now(UTC)


def _str_enum(enum_cls: type[enum.StrEnum]) -> Enum:
    """Portable enum column: VARCHAR storing canonical values, typed on load."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


_ENUM_TYPES = (
    Line,
    Length,
    Shot,
    Footwork,
    Contact,
    Outcome,
    BowlerSource,
    SessionType,
    BlockIntent,
    Handedness,
    VideoStatus,
    SessionState,
    CalibrationKind,
    EventSource,
    ClipStatus,
    MetricPhase,
    DatasetSplit,
    LabelClass,
    AnnotationSource,
    ModelStage,
    TrainingStatus,
    DeliveryIntensity,
    ReportKind,
    ReportStatus,
    StageStatus,
    AlertAudience,
    EvidenceVerdict,
    CameraRole,
    BowlingVariation,
    MilestoneKind,
)

_TYPE_MAP: dict[Any, Any] = {dict[str, Any]: JSON, list[str]: JSON}
for _enum_cls in _ENUM_TYPES:
    _TYPE_MAP[_enum_cls] = _str_enum(_enum_cls)


class Base(DeclarativeBase):
    type_annotation_map = _TYPE_MAP


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    birthdate: Mapped[date] = mapped_column(Date)
    handedness: Mapped[Handedness] = mapped_column(default=Handedness.RIGHT)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)

    sessions: Mapped[list[Session]] = relationship(back_populates="player")


class CameraConfig(Base):
    """Camera registry (US-A1): one row per camera per placement era."""

    __tablename__ = "camera_configs"
    __table_args__ = (UniqueConstraint("camera_id", "era_no", name="uq_camera_era"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[str] = mapped_column(String(8))  # C1..C8
    era_no: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    position_label: Mapped[str] = mapped_column(String(64))
    # US-I1: what this camera looks at (drives per-stage camera consumption).
    # Nullable — rows registered before Phase 6 stay NULL until re-registered.
    role: Mapped[CameraRole | None] = mapped_column(nullable=True)
    xyz_offset_m: Mapped[dict[str, Any]] = mapped_column()  # {"x":…, "y":…, "z":…}
    height_m: Mapped[float] = mapped_column(Float)
    fps: Mapped[int] = mapped_column(Integer)
    resolution: Mapped[str] = mapped_column(String(16))  # e.g. 1920x1080
    lens: Mapped[str] = mapped_column(String(64), default="")
    mount: Mapped[str] = mapped_column(String(64), default="")
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    fov_reference_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Calibration(Base):
    """Camera calibration record per placement era (US-C1/C2)."""

    __tablename__ = "calibrations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[str] = mapped_column(String(8))  # C1..C8
    era_no: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[CalibrationKind] = mapped_column()
    params: Mapped[dict[str, Any]] = mapped_column()  # K/dist or homography + frame contract
    rms: Mapped[float | None] = mapped_column(Float, nullable=True)  # reprojection / held-out RMS
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BounceMark(Base):
    """Manual bounce-point click mapped to pitch coordinates (US-C5).

    Raw pixel + computed pitch xy are stored so zone classes can be re-derived
    from configuration without re-clicking; ``line``/``length`` cache the
    derivation current at write time.
    """

    __tablename__ = "bounce_marks"
    __table_args__ = (
        UniqueConstraint("session_id", "ball_no", "camera_id", name="uq_bounce_ball_camera"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    camera_id: Mapped[str] = mapped_column(String(8))  # C1..C8; clicks come from C3/C4
    frame_no: Mapped[int] = mapped_column(Integer)
    px_x: Mapped[float] = mapped_column(Float)
    px_y: Mapped[float] = mapped_column(Float)
    pitch_x: Mapped[float] = mapped_column(Float)
    pitch_y: Mapped[float] = mapped_column(Float)
    line: Mapped[Line | None] = mapped_column(nullable=True)
    length: Mapped[Length | None] = mapped_column(nullable=True)
    calibration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calibrations.id"), nullable=True
    )
    flagged_for_review: Mapped[bool] = mapped_column(Boolean, default=False)  # >15cm disagreement
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChecklistAck(Base):
    """Safety checklist acknowledgment (US-A5) — required before machine sessions."""

    __tablename__ = "checklist_acks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    items: Mapped[dict[str, Any]] = mapped_column()  # {item_id: true, …}
    acked_by: Mapped[str] = mapped_column(String(64))
    acked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    session_date: Mapped[date] = mapped_column(Date)
    session_type: Mapped[SessionType] = mapped_column()
    bowler_source: Mapped[BowlerSource] = mapped_column()
    machine_settings: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # UNTRUSTED input (US-G4)
    state: Mapped[SessionState] = mapped_column(default=SessionState.CREATED)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    missing_views: Mapped[list[str]] = mapped_column(default=list)
    expected_cameras: Mapped[list[str]] = mapped_column(default=list)
    calibration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calibrations.id"), nullable=True
    )
    calibration_suspect: Mapped[bool] = mapped_column(Boolean, default=False)  # US-C4 drift flag
    checklist_ack_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("checklist_acks.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    player: Mapped[Player] = relationship(back_populates="sessions")
    checklist_ack: Mapped[ChecklistAck | None] = relationship()
    calibration: Mapped[Calibration | None] = relationship()
    blocks: Mapped[list[SessionBlock]] = relationship(
        back_populates="session", order_by="SessionBlock.block_no"
    )
    videos: Mapped[list[Video]] = relationship(back_populates="session")
    ball_tags: Mapped[list[BallTag]] = relationship(
        back_populates="session", order_by="BallTag.ball_no"
    )


class SessionBlock(Base):
    """Practice block (US-B3): balls inherit block context by timestamp."""

    __tablename__ = "session_blocks"
    __table_args__ = (UniqueConstraint("session_id", "block_no", name="uq_session_block_no"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    block_no: Mapped[int] = mapped_column(Integer)
    start_s: Mapped[float] = mapped_column(Float)  # seconds from session start
    end_s: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = open
    bowler_source: Mapped[BowlerSource] = mapped_column()
    machine_settings: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    intent: Mapped[BlockIntent] = mapped_column()

    session: Mapped[Session] = relationship(back_populates="blocks")


class Video(Base):
    """One uploaded camera file (US-B2). DB stores the object key, never bytes."""

    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("session_id", "checksum_sha256", name="uq_video_session_checksum"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    camera_id: Mapped[str] = mapped_column(String(8))
    object_key: Mapped[str] = mapped_column(String(255), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    claimed_fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    claimed_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    claimed_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    codec: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[VideoStatus] = mapped_column(default=VideoStatus.PENDING)
    probe: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[Session] = relationship(back_populates="videos")


class UploadSession(Base):
    """Resumable multipart upload bookkeeping (US-B2)."""

    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    camera_id: Mapped[str] = mapped_column(String(8))
    filename: Mapped[str] = mapped_column(String(255))
    declared_checksum: Mapped[str] = mapped_column(String(64))
    declared_size: Mapped[int] = mapped_column(BigInteger)
    store_upload_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    claimed_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    claimed_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    parts: Mapped[list[UploadPart]] = relationship(
        back_populates="upload", order_by="UploadPart.part_no"
    )


class UploadPart(Base):
    __tablename__ = "upload_parts"
    __table_args__ = (UniqueConstraint("upload_id", "part_no", name="uq_upload_part"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("upload_sessions.id"))
    part_no: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str] = mapped_column(String(64))

    upload: Mapped[UploadSession] = relationship(back_populates="parts")


class BallTag(Base):
    """Manual per-ball tag (US-B4) — schema-identical to future automatic output."""

    __tablename__ = "ball_tags"
    __table_args__ = (UniqueConstraint("session_id", "ball_no", name="uq_tag_session_ball"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    block_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("session_blocks.id"), nullable=True
    )
    line: Mapped[Line] = mapped_column()
    length: Mapped[Length] = mapped_column()
    shot: Mapped[Shot] = mapped_column()
    footwork: Mapped[Footwork] = mapped_column()
    contact: Mapped[Contact] = mapped_column()
    outcome: Mapped[Outcome] = mapped_column()
    control: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    ground_truth_eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[Session] = relationship(back_populates="ball_tags")
    audits: Mapped[list[TagAudit]] = relationship(back_populates="tag", order_by="TagAudit.at")


class TagAudit(Base):
    """Full edit history for tags (US-B4: who, when, old→new)."""

    __tablename__ = "tag_audits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ball_tags.id"))
    actor: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tag: Mapped[BallTag] = relationship(back_populates="audits")


class HealthCheckRecord(Base):
    """Pre-session health check results (US-A4), stored for later debugging."""

    __tablename__ = "health_checks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    passed: Mapped[bool] = mapped_column(Boolean)
    results: Mapped[dict[str, Any]] = mapped_column()


class StorageUsageSample(Base):
    """Daily storage usage snapshot powering the days-remaining forecast (US-B6)."""

    __tablename__ = "storage_usage_samples"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sampled_on: Mapped[date] = mapped_column(Date, unique=True)
    bytes_used: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BallEvent(Base):
    """One detected/entered ball on a session's timeline (US-D1/D4).

    Timestamps are video-timeline milliseconds on the reference camera (C1).
    ``contact_ms`` stays NULL for a left ball — the event exists regardless.
    """

    __tablename__ = "ball_events"
    __table_args__ = (UniqueConstraint("session_id", "ball_no", name="uq_event_session_ball"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    release_ms: Mapped[int] = mapped_column(Integer)
    contact_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    source: Mapped[EventSource] = mapped_column(default=EventSource.AUTO)
    detector_version: Mapped[str] = mapped_column(String(64), default="")
    valid: Mapped[bool] = mapped_column(Boolean, default=True)  # False = rejected (US-D4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventCorrection(Base):
    """Versioned ground-truth trail of every human event correction (US-D4)."""

    __tablename__ = "event_corrections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ball_events.id"))
    action: Mapped[str] = mapped_column(String(16))  # accept|adjust|reject|add
    before: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    actor: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Clip(Base):
    """One per-ball per-camera clip (US-D2). GAP rows record missing cameras loudly."""

    __tablename__ = "clips"
    __table_args__ = (
        UniqueConstraint("session_id", "ball_no", "camera_id", name="uq_clip_ball_camera"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    camera_id: Mapped[str] = mapped_column(String(8))
    object_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[ClipStatus] = mapped_column(default=ClipStatus.PENDING)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PoseTrack(Base):
    """Per-ball pose landmark payload pointer (US-E1). Landmarks live in the object store."""

    __tablename__ = "pose_tracks"
    __table_args__ = (
        UniqueConstraint("session_id", "ball_no", "camera_id", name="uq_pose_ball_camera"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    camera_id: Mapped[str] = mapped_column(String(8))
    model_name: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64))
    landmarks_key: Mapped[str] = mapped_column(String(255))
    frame_count: Mapped[int] = mapped_column(Integer)
    availability: Mapped[float] = mapped_column(Float)  # landmark coverage in window (US-E1)
    subject_confidence: Mapped[float] = mapped_column(Float)  # batter-selection confidence
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BallMetrics(Base):
    """One phase's metric set for one ball (US-E2/E3/E4).

    ``metrics`` values are {value, unit, confidence, reason (when value is null),
    proxy (bool, US-E3)} — nullable-with-reason, never silent zeros.
    """

    __tablename__ = "ball_metrics"
    __table_args__ = (
        UniqueConstraint("session_id", "ball_no", "phase", name="uq_metrics_ball_phase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    phase: Mapped[MetricPhase] = mapped_column()
    metrics: Mapped[dict[str, Any]] = mapped_column()
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReferenceBall(Base):
    """Coach-marked model-example ball for future comparisons (US-E5)."""

    __tablename__ = "reference_balls"
    __table_args__ = (UniqueConstraint("session_id", "ball_no", name="uq_reference_session_ball"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(120))
    marked_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """System-wide audit trail (US-L3: deletions, exports; US-H1: threshold changes)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FrameSample(Base):
    """One sampled frame for labeling (US-F1) with provenance back to its ball.

    ``stratum`` records the diversity tags the sampler balanced over (lighting,
    machine speed band, block intent) so dataset composition is auditable.
    """

    __tablename__ = "frame_samples"
    __table_args__ = (
        UniqueConstraint("session_id", "camera_id", "frame_no", name="uq_frame_session_cam_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    camera_id: Mapped[str] = mapped_column(String(8))
    frame_no: Mapped[int] = mapped_column(Integer)
    ts_ms: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(255))
    stratum: Mapped[dict[str, Any]] = mapped_column(default=dict)
    sampler_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Annotation(Base):
    """One labeled box on a sampled frame (US-F1). Coordinates are normalized
    cx/cy/w/h in [0, 1] (YOLO convention)."""

    __tablename__ = "annotations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    frame_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("frame_samples.id"))
    label_class: Mapped[LabelClass] = mapped_column()
    cx: Mapped[float] = mapped_column(Float)
    cy: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    annotator: Mapped[str] = mapped_column(String(64))
    source: Mapped[AnnotationSource] = mapped_column(default=AnnotationSource.MANUAL)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dataset(Base):
    """One immutable dataset version (US-F1). Freezing computes ``manifest_digest``
    over the full membership; frozen datasets refuse membership changes."""

    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version: Mapped[str] = mapped_column(String(64), unique=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    manifest_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatasetMember(Base):
    """Frame membership + split assignment inside one dataset version (US-F1)."""

    __tablename__ = "dataset_members"
    __table_args__ = (UniqueConstraint("dataset_id", "frame_id", name="uq_dataset_member_frame"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    frame_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("frame_samples.id"))
    split: Mapped[DatasetSplit] = mapped_column()


class ModelRun(Base):
    """One training/eval run (US-F2). ``metrics`` uses the pinned headline keys
    (map50_ball, map50_bat, map50_stumps, recall_ball_high_blur, ...);
    ``report_key`` points at the eval-report artifact in the object store."""

    __tablename__ = "model_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_name: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    report_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[TrainingStatus] = mapped_column(default=TrainingStatus.PENDING)
    trainer_version: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelVersion(Base):
    """Registry entry tying a model version to its run (US-F2). Promotion moves
    ``stage`` candidate -> staging -> production under the regression gate."""

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("model_name", "version", name="uq_model_name_version"),
        # DB backstop for "exactly one production version per model name":
        # concurrent promotions can both pass the API's read-then-write gate,
        # so the second commit must die here. PostgreSQL-only DDL (partial
        # unique index); SQLite unit DBs exercise the application-level gate.
        # NOTE: a demote-and-replace promotion must flush the demotion before
        # setting the successor to production, or the index trips mid-flush.
        Index(
            "uq_model_versions_one_production",
            "model_name",
            unique=True,
            postgresql_where=text("stage = 'production'"),
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_runs.id"))
    stage: Mapped[ModelStage] = mapped_column(default=ModelStage.CANDIDATE)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BallTrack(Base):
    """Per-ball per-camera tracked trajectory pointer (US-F3). Points live in the
    object store under ``sessions/{sid}/balls/{n}/track-{camera}.json``;
    ``segments``/``flags`` carry per-segment confidence and identity/gap flags."""

    __tablename__ = "ball_tracks"
    __table_args__ = (
        UniqueConstraint("session_id", "ball_no", "camera_id", name="uq_track_ball_camera"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    camera_id: Mapped[str] = mapped_column(String(8))
    tracker_version: Mapped[str] = mapped_column(String(64))
    points_key: Mapped[str] = mapped_column(String(255))
    coverage: Mapped[float] = mapped_column(Float)  # fraction of flight tracked (US-F3)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    flags: Mapped[dict[str, Any]] = mapped_column(default=dict)
    confidence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BounceEstimate(Base):
    """Auto bounce point in pitch coordinates (US-F4). The manual
    ``bounce_marks`` row always wins; auto rows never touch manual ones."""

    __tablename__ = "bounce_estimates"
    __table_args__ = (UniqueConstraint("session_id", "ball_no", name="uq_bounce_estimate_ball"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    pitch_x: Mapped[float] = mapped_column(Float)
    pitch_y: Mapped[float] = mapped_column(Float)
    line: Mapped[Line | None] = mapped_column(nullable=True)
    length: Mapped[Length | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    tracker_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CoachingRule(Base):
    """One version of a coaching rule (US-G2). Rules are data, not code: the
    DSL ``definition`` is parsed and validated by ``cricai_coaching.rules``.
    Versions are append-only; ``enabled`` retires a rule without rewriting
    history. Changing thresholds requires a new version with ``approved_by``."""

    __tablename__ = "coaching_rules"
    __table_args__ = (UniqueConstraint("rule_key", "version", name="uq_rule_key_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rule_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    author: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale: Mapped[str] = mapped_column(Text)
    definition: Mapped[dict[str, Any]] = mapped_column()
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuleOverride(Base):
    """Per-player rule adjustment (US-G2): disable or re-parameterize one rule
    for one player, with the reason and actor logged."""

    __tablename__ = "rule_overrides"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rule_key: Mapped[str] = mapped_column(String(64))
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    action: Mapped[str] = mapped_column(String(32))  # disable|adjust
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SafetyConfig(Base):
    """Append-only workload/safety threshold versions (US-H1). The latest
    version wins; raising any ceiling requires ``approved_by`` with coach role
    (enforced by the API) and every change writes an AuditLog row."""

    __tablename__ = "safety_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    config: Mapped[dict[str, Any]] = mapped_column()
    approved_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Finding(Base):
    """One machine-derived coaching finding (US-G2 rules, US-J2 probes).

    Findings are regenerable: they reference balls via ``ball_ids`` JSON, not
    ``(session_id, ball_no)`` columns, so they deliberately stay out of
    ``EVENT_DEPENDENT_TABLES`` — staleness is handled by the re-derive cascade.
    ``severity`` stores ``FindingSeverity`` values as plain strings.
    """

    __tablename__ = "findings"
    # Hot path: report/analysis/drills/privacy/re-derive all load a session's
    # findings; PostgreSQL does not auto-index FK columns.
    __table_args__ = (Index("ix_findings_session_id", "session_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=True)
    agent: Mapped[str] = mapped_column(String(32))
    rule_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(32))  # FindingSeverity values
    metric: Mapped[str] = mapped_column(String(64))
    condition: Mapped[dict[str, Any]] = mapped_column(default=dict)
    n: Mapped[int] = mapped_column(Integer)
    effect_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    ball_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    """One coaching report (US-G3 daily, US-G5 weekly/monthly). ``body`` is the
    pinned Report-body-v1 JSON artifact; ``safety_sha256`` hash-verifies the
    verbatim safety text (US-H5); ``quality`` carries the US-L4 QualityScore."""

    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "kind", "period_start", "period_end", name="uq_report_player_kind_period"
        ),
        # Hot path: report listing/upsert filters by player + kind (US-G3/G5).
        Index("ix_reports_player_id_kind", "player_id", "kind"),
        # Hot path (US-J5): the coach review queue and the publish sweep both
        # filter status=DRAFT with a review_due_at, ordered by the deadline.
        Index("ix_reports_status_review_due_at", "status", "review_due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    kind: Mapped[ReportKind] = mapped_column()
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[ReportStatus] = mapped_column(default=ReportStatus.DRAFT)
    body: Mapped[dict[str, Any]] = mapped_column()
    safety_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    # US-J5 review gate: in coach_gate mode a generated report stays DRAFT with a
    # deadline; the publish sweep auto-publishes at this time unless a coach
    # acted. NULL under auto_publish mode (published immediately).
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReportLLMAudit(Base):
    """Full request/response audit of every LLM wording call (US-G4). Rejected
    responses (template/claim/safety validation) keep ``accepted=False`` with
    the reason so the fallback path is auditable."""

    __tablename__ = "report_llm_audits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reports.id"))
    prompt_key: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict[str, Any]] = mapped_column()
    response: Mapped[dict[str, Any]] = mapped_column()
    accepted: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Drill(Base):
    """One drill in the library (US-J3). ``machine_settings`` must validate
    against the bowling machine's envelope before a plan can reference it."""

    __tablename__ = "drills"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    setup: Mapped[str] = mapped_column(Text)
    machine_settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ball_count: Mapped[int] = mapped_column(Integer)
    target_metric: Mapped[str] = mapped_column(String(64))
    intent: Mapped[BlockIntent] = mapped_column()
    author: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DrillPlan(Base):
    """One planned practice day (US-J3). ``blocks`` is the DrillPlan-blocks
    contract; ``safety``/``safety_sha256`` embed the hash-verified safety
    verdict active at planning time (US-H5)."""

    __tablename__ = "drill_plans"
    __table_args__ = (UniqueConstraint("player_id", "plan_date", name="uq_plan_player_date"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    plan_date: Mapped[date] = mapped_column(Date)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    finding_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    safety: Mapped[dict[str, Any]] = mapped_column(default=dict)
    safety_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BowlingLedgerEntry(Base):
    """One day's bowling volume for one player at one intensity (US-H1).

    The ledger is entry-based: rolling-7 windows, age bands and violations are
    computed pure-functionally from entries + config — no denormalized weekly
    columns to drift. ``source`` records auto-backfill vs manual entry.
    """

    __tablename__ = "bowling_ledger_entries"
    __table_args__ = (
        # Hot path: every safety evaluation / workload summary scans one
        # player's rolling window by date (US-H1).
        Index("ix_ledger_player_id_entry_date", "player_id", "entry_date"),
        # DB backstop for the backfill job's idempotency invariant (US-H1):
        # ``backfill_workload._process_session`` delete-and-recreates AT MOST
        # ONE ``source='auto_backfill'`` row per session (single
        # ``entry_date == session_date``, single derived intensity), but that
        # guarantee is application-level only — two overlapping runs under
        # READ COMMITTED each delete 0/stale rows and both insert, silently
        # double-counting a child's bowling workload. This index makes the
        # second commit die loudly instead. Manual rows are never affected.
        # PostgreSQL-only DDL (partial unique index); SQLite unit DBs keep
        # exercising the application-level delete-then-insert guard.
        Index(
            "uq_ledger_auto_backfill_session_intensity",
            "session_id",
            "intensity",
            unique=True,
            postgresql_where=text("source = 'auto_backfill'"),
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    entry_date: Mapped[date] = mapped_column(Date)
    balls: Mapped[int] = mapped_column(Integer)
    intensity: Mapped[DeliveryIntensity] = mapped_column()
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # auto_backfill|manual
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WellnessCheckin(Base):
    """One wellness check-in (US-H4). ``soreness`` is a structured body-map;
    ``pain=True`` suppresses bowling recommendations until an adult clears it."""

    __tablename__ = "wellness_checkins"
    # Hot path: wellness state / safety evaluation reads one player's recent
    # check-ins by date (US-H4).
    __table_args__ = (Index("ix_wellness_player_id_checkin_date", "player_id", "checkin_date"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    checkin_date: Mapped[date] = mapped_column(Date)
    soreness: Mapped[dict[str, Any]] = mapped_column(default=dict)
    energy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sleep_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    pain: Mapped[bool] = mapped_column(Boolean, default=False)
    pain_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PainClearance(Base):
    """An adult clearing a pain flag (US-H4) — who, in what role, and why."""

    __tablename__ = "pain_clearances"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    checkin_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wellness_checkins.id"))
    cleared_by: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))  # parent|coach
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineRun(Base):
    """One DAG run over a session (US-J1, US-L1). ``detector_context`` pins the
    model/detector versions the run used so traces are reproducible."""

    __tablename__ = "pipeline_runs"
    # Hot path: the runner and run-report verdict lookup select a session's
    # runs (US-J1); PostgreSQL does not auto-index FK columns.
    __table_args__ = (Index("ix_pipeline_runs_session_id", "session_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    status: Mapped[StageStatus] = mapped_column(default=StageStatus.PENDING)
    detector_context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PipelineStage(Base):
    """One stage attempt inside a pipeline run (US-J1/L1). ``input_digest``
    lets resume-from-failure short-circuit completed stages; timings feed the
    stage SLO record (US-L1)."""

    __tablename__ = "pipeline_stages"
    __table_args__ = (
        UniqueConstraint("run_id", "stage", "attempt", name="uq_stage_run_stage_attempt"),
        # Hot path: resume/trace loads all stage attempts of a run (US-J1).
        Index("ix_pipeline_stages_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.id"))
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[StageStatus] = mapped_column(default=StageStatus.PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MetricBaseline(Base):
    """Frozen nightly baseline snapshot for one metric slice (US-J4). Progress
    comparisons read snapshots, never recompute history in place."""

    __tablename__ = "metric_baselines"
    __table_args__ = (
        UniqueConstraint(
            "player_id",
            "metric",
            "zone_key",
            "window",
            "snapshot_date",
            name="uq_baseline_slice_snapshot",
        ),
        # Hot path: progress snapshots read one player's baselines per metric
        # across zones/windows/snapshot dates (US-J4).
        Index("ix_baselines_player_id_metric", "player_id", "metric"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    metric: Mapped[str] = mapped_column(String(64))
    zone_key: Mapped[str] = mapped_column(String(64))
    window: Mapped[str] = mapped_column(String(32))
    snapshot_date: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    n: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    """One routed alert (US-L4): honesty/quality banners to parents, drift and
    canary failures to developers. Acknowledgement is per-row, not per-code."""

    __tablename__ = "alerts"
    # Hot path: the alert inbox lists per-audience unacknowledged rows (US-L4).
    __table_args__ = (Index("ix_alerts_audience_acknowledged", "audience", "acknowledged"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    audience: Mapped[AlertAudience] = mapped_column()
    code: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceVerdictRecord(Base):
    """A coach's verdict on one finding's evidence (US-G6). ``not_supported``
    verdicts feed rule/probe quality review — human input, never regenerable."""

    __tablename__ = "evidence_verdicts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"))
    verdict: Mapped[EvidenceVerdict] = mapped_column()
    actor: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BowlingTarget(Base):
    """A declared line/length target for a bowling block (US-I4).

    Targets are the coach's stated intent for a block; the accuracy scorecard
    scores deliveries against them. Block-scoped targets carry a ``block_id``;
    a session-wide target leaves it NULL.
    """

    __tablename__ = "bowling_targets"
    __table_args__ = (
        # Hot path (US-I4): targets are listed and scored per session, and the
        # privacy cascade deletes by session (PostgreSQL does not auto-index FKs).
        Index("ix_bowling_targets_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    block_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("session_blocks.id"), nullable=True
    )
    line: Mapped[Line] = mapped_column()
    length: Mapped[Length] = mapped_column()
    description: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryLabel(Base):
    """Per-ball leg-spin variation label (US-I6) — the bowling analogue of a
    ``BallTag``: human ground truth, backup-covered, guard-protected via
    ``(session_id, ball_no)``.

    ``variation_intent`` is what the bowler/coach meant to bowl (ground truth,
    never overwritten by model output); ``variation_detected`` is the honest V1
    classifier prediction (NULL until the classifier runs). ``source`` records
    whether the intent came from a human or a model pre-label; manual labels
    always win.
    """

    __tablename__ = "delivery_labels"
    __table_args__ = (
        UniqueConstraint("session_id", "ball_no", name="uq_delivery_label_session_ball"),
        # Hot path (US-I6): label list/export and the privacy cascade read by
        # session (the uq btree also leads on session_id; this pins the phase-5
        # explicit-index discipline for the FK column).
        Index("ix_delivery_labels_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    ball_no: Mapped[int] = mapped_column(Integer)
    variation_intent: Mapped[BowlingVariation] = mapped_column()
    variation_detected: Mapped[BowlingVariation | None] = mapped_column(nullable=True)
    labeler: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual|model
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CoachNote(Base):
    """One free-text coaching note (US-K3). ``body`` is UNTRUSTED human input:
    it never leaves the LAN (egress strict-PII set) and is HTML-escaped at
    render time. Session-linked notes (``session_id`` set) are footage-adjacent
    and privacy-deleted with the session; player-scoped notes survive."""

    __tablename__ = "coach_notes"
    __table_args__ = (
        # Hot path (US-K3): the notes list filters by player_id with an
        # optional session_id narrowing — player_id leads for that query shape
        # (PostgreSQL does not auto-index FK columns).
        Index("ix_coach_notes_player_id_session_id", "player_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    ball_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text)  # UNTRUSTED input (US-K3/US-G4)
    author: Mapped[str] = mapped_column(String(64))
    visibility: Mapped[str] = mapped_column(String(16), default="coach_only")  # coach_only|shared
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Milestone(Base):
    """One celebrated progress landmark (US-K4/G5). Append-only log; the weekly/
    monthly rollup writes personal bests, bowling volume landmarks and practice
    streaks. ``context`` carries the supporting numbers (kid-mode SAF-linted at
    render). The uq key makes a given (kind, metric, day) landmark idempotent."""

    __tablename__ = "milestones"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "kind", "metric", "achieved_on", name="uq_milestone_player_kind_metric_day"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    kind: Mapped[MilestoneKind] = mapped_column()
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    achieved_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    """Append-only versioned app configuration (US-J5/L5), like ``safety_configs``.

    The latest version wins. ``settings`` carries the report-review gate mode
    (US-J5) and the live-mode allow-list (US-L5); the v1 seed is mirrored in
    ``cricai_coaching.app_settings.DEFAULT_APP_SETTINGS`` with a drift test
    pinning migration == code. Changing a setting is a new version with
    ``approved_by``, never an edit.
    """

    __tablename__ = "app_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column()
    approved_by: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


#: Downstream tables joining ball events on ``(session_id, ball_no)`` with no FK
#: to ``BallEvent.id``. Canonical single source of truth (Phase 3, US-D1/D4):
#: the corrections API's renumbering guard and the worker's re-detection guard
#: both import this list, so a table added here is guard-protected everywhere at
#: once — and both guard test suites parametrize over these labels, so a new
#: entry without a matching test seeder fails loudly (KeyError), never silently.
EVENT_DEPENDENT_TABLES: tuple[tuple[type[Any], str], ...] = (
    (BallTag, "ball tags"),
    (BounceMark, "bounce marks"),
    (Clip, "clips"),
    (PoseTrack, "pose tracks"),
    (BallMetrics, "ball metrics"),
    (ReferenceBall, "reference balls"),
    (BallTrack, "ball tracks"),
    (BounceEstimate, "bounce estimates"),
    # Frame provenance (US-F1): annotations/dataset_members hang off frame id,
    # so guarding frames protects them transitively. NULL ball_no rows never
    # match the guards' ball_no equality — ball-less frames don't pin numbers.
    (FrameSample, "frame samples"),
    # Phase 6 ball-numbered human input (US-I6/US-K3): delivery labels are
    # bowling ground truth (like ball_tags); a coach note pinned to a ball_no is
    # human commentary that must not be silently re-associated. Notes with a
    # NULL ball_no (player/session-level) never match the guards' equality.
    (DeliveryLabel, "delivery labels"),
    (CoachNote, "coach notes"),
)
