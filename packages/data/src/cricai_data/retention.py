"""US-B6: retention selection, storage forecast, backup manifest & restore verification.

Mostly pure logic — no object-store I/O. The API router (and later the worker)
feeds it object listings with ages, usage history and DB snapshots; it decides
what expires, how long the disk lasts, and whether a restore drill reproduced
the backup bit-exact. Two helpers read the DB (never write it):
``derive_protected_keys`` derives ground-truth clip prefixes from tags and
``snapshot_entities`` fingerprints rows for backup manifests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_data.models import (
    Annotation,
    AppSetting,
    BallEvent,
    BallMetrics,
    BallTag,
    BallTrack,
    Base,
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
    Drill,
    DrillPlan,
    EventCorrection,
    EvidenceVerdictRecord,
    FrameSample,
    Milestone,
    ModelRun,
    ModelVersion,
    PainClearance,
    Player,
    PoseTrack,
    ReferenceBall,
    Report,
    RuleOverride,
    SafetyConfig,
    Session,
    SessionBlock,
    Video,
    WellnessCheckin,
)

ObjectClass = Literal["raw_video", "clip", "other"]

_SESSIONS_PREFIX = "sessions"
_CLIPS_SEGMENT = "balls"


@dataclass(frozen=True)
class RetentionPolicy:
    """Tiered retention limits in days; ``None`` means keep forever (US-B6).

    Defaults: raw multi-cam video 90 days, per-ball evidence clips 2 years,
    metrics forever. ``metrics_days`` applies to DB-resident metrics and is
    enforced by the worker, not by object-key selection.
    """

    raw_video_days: int | None = 90
    clip_days: int | None = 730
    metrics_days: int | None = None


@dataclass(frozen=True)
class ObjectInfo:
    """One stored object as seen by the retention selector."""

    key: str
    age_days: float


def classify_object(key: str) -> ObjectClass:
    """Classify an object key by the canonical storage layout.

    - ``sessions/{session}/balls/…`` → ``clip``
    - ``sessions/{session}/{camera}/{filename}`` → ``raw_video``
    - anything else → ``other`` (never auto-deleted)
    """
    parts = key.split("/")
    if len(parts) < 4 or parts[0] != _SESSIONS_PREFIX or not all(parts):
        return "other"
    if parts[2] == _CLIPS_SEGMENT:
        return "clip"
    if len(parts) == 4:
        return "raw_video"
    return "other"


def derive_protected_keys(db: OrmSession) -> set[str]:
    """Key prefixes shielding ground-truth evidence from retention (US-B6 SAF).

    Every :class:`BallTag` with ``ground_truth_eligible`` protects everything
    under ``sessions/{session_id}/balls/{ball_no}/`` — clips referenced by eval
    sets must survive regardless of what protections a caller supplies.
    """
    rows = db.execute(
        select(BallTag.session_id, BallTag.ball_no).where(BallTag.ground_truth_eligible.is_(True))
    ).all()
    return {
        f"{_SESSIONS_PREFIX}/{session_id}/{_CLIPS_SEGMENT}/{ball_no}/"
        for session_id, ball_no in rows
    }


def select_expired(
    objects: list[ObjectInfo],
    policy: RetentionPolicy,
    protected_keys: set[str],
    protected_prefixes: Collection[str] = frozenset(),
) -> list[str]:
    """Keys whose age exceeds their tier limit, in input order.

    Never returns protected keys (ground-truth clips referenced by model eval
    sets) nor ``other`` objects. A key is protected if it equals an entry in
    ``protected_keys`` or starts with any prefix in ``protected_prefixes``
    (see :func:`derive_protected_keys`). Boundary: ``age_days == limit``
    stays; only strictly older objects expire.
    """
    limits: dict[ObjectClass, int | None] = {
        "raw_video": policy.raw_video_days,
        "clip": policy.clip_days,
        "other": None,
    }
    expired: list[str] = []
    for obj in objects:
        if obj.key in protected_keys:
            continue
        if any(obj.key.startswith(prefix) for prefix in protected_prefixes):
            continue
        limit = limits[classify_object(obj.key)]
        if limit is not None and obj.age_days > limit:
            expired.append(obj.key)
    return expired


@dataclass(frozen=True)
class ForecastReport:
    """Storage exhaustion forecast (US-B6 dashboard: days remaining)."""

    bytes_used: int
    capacity_bytes: int
    daily_rate_bytes: float
    days_remaining: float | None


def storage_forecast(history: list[tuple[int, int]], capacity_bytes: int) -> ForecastReport:
    """Forecast disk exhaustion from ``(day_index, bytes_used)`` history.

    ``daily_rate_bytes`` is the slope of the ordinary least-squares line
    through the history: ``b = Σ(x-x̄)(y-ȳ) / Σ(x-x̄)²``. With fewer than two
    distinct day indices the rate is 0. ``days_remaining`` is
    ``(capacity - latest usage) / rate`` clamped at 0, or ``None`` when the
    rate is not positive (flat or shrinking usage has no exhaustion date).
    """
    if capacity_bytes <= 0:
        raise ValueError(f"capacity_bytes must be positive, got {capacity_bytes}")
    if not history:
        return ForecastReport(0, capacity_bytes, 0.0, None)
    bytes_used = max(history, key=lambda point: point[0])[1]
    mean_x = sum(x for x, _ in history) / len(history)
    mean_y = sum(y for _, y in history) / len(history)
    denominator = sum((x - mean_x) ** 2 for x, _ in history)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in history)
    rate = covariance / denominator if denominator else 0.0
    days_remaining = max(0.0, (capacity_bytes - bytes_used) / rate) if rate > 0 else None
    return ForecastReport(bytes_used, capacity_bytes, rate, days_remaining)


@dataclass(frozen=True)
class Manifest:
    """Content fingerprint of a backup: canonical-JSON sha256 plus counts."""

    sha256: str
    entity_counts: dict[str, int]
    object_count: int


#: Tables covered by backup manifests (US-B6). Audit/upload bookkeeping is
#: rebuilt or irrelevant after a restore, so it is deliberately excluded.
#: bounce_marks are coach-marked pitch-map ground truth (US-C5) — human input
#: that cannot be recomputed from video, so a restore drill must prove they
#: survived. Phase-3 derived tables are covered too: event_corrections is
#: versioned human ground truth (US-D4), manual/corrected ball_events and
#: coach-marked reference_balls are human input that cannot be recomputed
#: from video, and clips/pose_tracks/ball_metrics carry provenance the
#: restore drill must prove survived intact. Phase-4 labeling and registry
#: tables are covered for the same reasons: annotations are irreplaceable
#: human labels (US-F1), datasets/dataset_members pin immutable training
#: membership, model_runs/model_versions are the registry provenance chain
#: (US-F2), and frame_samples/ball_tracks/bounce_estimates carry the
#: provenance those rows reference. Phase-5 coaching/safety tables holding
#: human input or published artifacts are covered: coaching_rules,
#: rule_overrides and safety_configs are authored+approved configuration
#: (US-G2/H1), reports are published artifacts parents may have acted on
#: (US-G3), drills/drill_plans are authored practice content (US-J3), the
#: bowling ledger, wellness check-ins and pain clearances are the safety
#: record itself (US-H1/H4), and evidence_verdicts are coach judgments
#: (US-G6). findings, pipeline_runs/stages, metric_baselines and alerts are
#: machine-derived and regenerated by the pipeline; report_llm_audits is
#: audit-class bookkeeping — all deliberately excluded. Phase-6 human-input and
#: config tables are covered too: bowling_targets are coach-declared block
#: intent (US-I4), delivery_labels are bowling ground truth (US-I6, the
#: leg-spin analogue of ball_tags), coach_notes are irreplaceable human
#: commentary (US-K3), milestones are the celebrated-progress log (US-K4/G5)
#: and app_settings is the append-only review-gate/live-mode configuration
#: (US-J5/L5) — a restore drill must prove all of them survived intact.
BACKUP_ENTITIES: dict[str, type[Base]] = {
    "players": Player,
    "sessions": Session,
    "session_blocks": SessionBlock,
    "videos": Video,
    "ball_tags": BallTag,
    "bounce_marks": BounceMark,
    "ball_events": BallEvent,
    "event_corrections": EventCorrection,
    "clips": Clip,
    "pose_tracks": PoseTrack,
    "ball_metrics": BallMetrics,
    "reference_balls": ReferenceBall,
    "frame_samples": FrameSample,
    "annotations": Annotation,
    "datasets": Dataset,
    "dataset_members": DatasetMember,
    "model_runs": ModelRun,
    "model_versions": ModelVersion,
    "ball_tracks": BallTrack,
    "bounce_estimates": BounceEstimate,
    "coaching_rules": CoachingRule,
    "rule_overrides": RuleOverride,
    "safety_configs": SafetyConfig,
    "reports": Report,
    "drills": Drill,
    "drill_plans": DrillPlan,
    "bowling_ledger_entries": BowlingLedgerEntry,
    "wellness_checkins": WellnessCheckin,
    "pain_clearances": PainClearance,
    "evidence_verdicts": EvidenceVerdictRecord,
    "bowling_targets": BowlingTarget,
    "delivery_labels": DeliveryLabel,
    "coach_notes": CoachNote,
    "milestones": Milestone,
    "app_settings": AppSetting,
}


def snapshot_entities(db: OrmSession) -> dict[str, list[dict[str, Any]]]:
    """Minimal DB snapshot for :func:`backup_manifest`: per row, id + field hash.

    Each row serializes to ``{"id": …, "fields_sha256": …}`` where the hash
    covers every other column canonically — so :func:`verify_restore` catches
    edited rows without the manifest storing (or leaking) row contents.
    """
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for name, model in BACKUP_ENTITIES.items():
        attrs = sa_inspect(model).column_attrs
        table_rows: list[dict[str, Any]] = []
        for row in db.scalars(select(model)).all():
            values = {attr.key: getattr(row, attr.key) for attr in attrs}
            row_id = str(values.pop("id"))
            payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
            table_rows.append(
                {"id": row_id, "fields_sha256": hashlib.sha256(payload.encode()).hexdigest()}
            )
        snapshot[name] = table_rows
    return snapshot


def _canonical_digest(db_entities: dict[str, list[dict[str, Any]]], object_keys: list[str]) -> str:
    """Order-independent sha256: sorted minified JSON rows per table, sorted keys."""
    tables = {
        name: sorted(
            json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) for row in rows
        )
        for name, rows in db_entities.items()
    }
    payload = json.dumps(
        {"entities": tables, "objects": sorted(object_keys)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def backup_manifest(
    db_entities: dict[str, list[dict[str, Any]]], object_keys: list[str]
) -> Manifest:
    """Fingerprint a backup so a restore drill can be verified bit-exact.

    Row and key ordering never affects the digest; non-JSON scalars (UUIDs,
    datetimes) are stringified canonically.
    """
    return Manifest(
        sha256=_canonical_digest(db_entities, object_keys),
        entity_counts={name: len(rows) for name, rows in db_entities.items()},
        object_count=len(object_keys),
    )


def verify_restore(
    manifest: Manifest,
    restored_entities: dict[str, list[dict[str, Any]]],
    restored_keys: list[str],
) -> list[str]:
    """Compare a restored snapshot against its backup manifest.

    Returns human-readable discrepancies; an empty list means the restore is
    bit-exact (US-B6 restore drill).
    """
    discrepancies: list[str] = []
    for name, expected in manifest.entity_counts.items():
        if name not in restored_entities:
            discrepancies.append(f"missing entity table: {name}")
        elif len(restored_entities[name]) != expected:
            discrepancies.append(
                f"entity count mismatch for {name}: "
                f"expected {expected}, got {len(restored_entities[name])}"
            )
    discrepancies.extend(
        f"unexpected entity table: {name}"
        for name in restored_entities
        if name not in manifest.entity_counts
    )
    if len(restored_keys) != manifest.object_count:
        discrepancies.append(
            f"object count mismatch: expected {manifest.object_count}, got {len(restored_keys)}"
        )
    if _canonical_digest(restored_entities, restored_keys) != manifest.sha256:
        discrepancies.append("content checksum mismatch: restored data is not bit-exact")
    return discrepancies
