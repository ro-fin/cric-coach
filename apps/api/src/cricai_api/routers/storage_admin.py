"""US-B6: storage administration — usage samples, forecast, retention, backups (Parent only).

Retention callers provide object ages (`{key: age_days}`); scanning real mtimes
is the worker's job. Ground-truth clip prefixes are derived server-side from
tags and unioned with caller-provided protections — a caller can never expose
eval-set evidence to deletion. Applying retention deletes via the object store
and writes one ``retention_delete`` audit row per key (deletions logged).
Backup manifests fingerprint the DB + object keys for quarterly restore drills.
"""

from datetime import date
from typing import Annotated

from cricai_data.enums import Role
from cricai_data.models import AuditLog, StorageUsageSample
from cricai_data.retention import (
    Manifest,
    ObjectInfo,
    RetentionPolicy,
    backup_manifest,
    derive_protected_keys,
    select_expired,
    snapshot_entities,
    storage_forecast,
    verify_restore,
)
from cricai_data.storage import FsObjectStore, StorageError, sha256_hex
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db, get_store

router = APIRouter(prefix="/storage", tags=["storage"])


class PolicyIn(BaseModel):
    """Retention limits in days; ``null`` = keep forever (US-B6 defaults)."""

    raw_video_days: int | None = Field(default=90, ge=0)
    clip_days: int | None = Field(default=730, ge=0)
    metrics_days: int | None = Field(default=None, ge=0)

    def to_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            raw_video_days=self.raw_video_days,
            clip_days=self.clip_days,
            metrics_days=self.metrics_days,
        )


class RetentionIn(BaseModel):
    ages: dict[str, float] = Field(description="object key → age in days")
    policy: PolicyIn = Field(default_factory=PolicyIn)
    protected_keys: set[str] = Field(
        default_factory=set,
        description="extra protections unioned with server-derived ground-truth prefixes",
    )


class UsageSampleIn(BaseModel):
    sampled_on: date
    bytes_used: int | None = Field(
        default=None, ge=0, description="omit to record measured usage under sessions/"
    )


class UsageSampleOut(BaseModel):
    sampled_on: date
    bytes_used: int


class ForecastOut(BaseModel):
    bytes_used: int
    capacity_bytes: int
    daily_rate_bytes: float
    days_remaining: float | None
    insufficient_history: bool


class PreviewOut(BaseModel):
    expired_keys: list[str]


class ApplyOut(BaseModel):
    deleted: int


class ManifestDocument(BaseModel):
    """Manifest JSON persisted in the object store (round-trips a Manifest)."""

    sha256: str
    entity_counts: dict[str, int]
    object_count: int


class ManifestOut(ManifestDocument):
    manifest_key: str


class VerifyIn(BaseModel):
    manifest_key: str


class VerifyOut(BaseModel):
    ok: bool
    discrepancies: list[str]


def _measured_bytes(store: FsObjectStore) -> int:
    return sum(store.size(key) for key in store.list_keys("sessions"))


def _expired_keys(db: OrmSession, payload: RetentionIn) -> list[str]:
    objects = [ObjectInfo(key=key, age_days=age) for key, age in payload.ages.items()]
    return select_expired(
        objects, payload.policy.to_policy(), payload.protected_keys, derive_protected_keys(db)
    )


@router.post("/usage-samples")
def record_usage_sample(
    payload: UsageSampleIn,
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> UsageSampleOut:
    """Record one day's usage; upserts on ``sampled_on`` (US-B6 forecast feed).

    ``bytes_used`` defaults to the measured store usage under ``sessions/``.
    """
    bytes_used = payload.bytes_used if payload.bytes_used is not None else _measured_bytes(store)
    sample = db.scalar(
        select(StorageUsageSample).where(StorageUsageSample.sampled_on == payload.sampled_on)
    )
    if sample is None:
        db.add(StorageUsageSample(sampled_on=payload.sampled_on, bytes_used=bytes_used))
    else:
        sample.bytes_used = bytes_used
    return UsageSampleOut(sampled_on=payload.sampled_on, bytes_used=bytes_used)


@router.get("/forecast")
def get_forecast(
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
    capacity_bytes: Annotated[int, Query(ge=1)],
) -> ForecastOut:
    """Days-remaining forecast from recorded usage samples (US-B6 dashboard).

    Day indices come from ``sampled_on`` ordinal deltas so gaps between samples
    weigh correctly. With fewer than two samples the history is a single
    measured point: rate 0, ``days_remaining`` null, ``insufficient_history``
    true — honest, not fabricated.
    """
    samples = db.scalars(select(StorageUsageSample).order_by(StorageUsageSample.sampled_on)).all()
    insufficient_history = len(samples) < 2
    if insufficient_history:
        history = [(0, _measured_bytes(store))]
    else:
        origin = samples[0].sampled_on.toordinal()
        history = [(s.sampled_on.toordinal() - origin, s.bytes_used) for s in samples]
    report = storage_forecast(history, capacity_bytes)
    return ForecastOut(
        bytes_used=report.bytes_used,
        capacity_bytes=report.capacity_bytes,
        daily_rate_bytes=report.daily_rate_bytes,
        days_remaining=report.days_remaining,
        insufficient_history=insufficient_history,
    )


@router.post("/retention/preview")
def preview_retention(
    payload: RetentionIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> PreviewOut:
    """Dry run: which keys the policy would delete. Touches nothing."""
    return PreviewOut(expired_keys=_expired_keys(db, payload))


@router.post("/retention/apply")
def apply_retention(
    payload: RetentionIn,
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> ApplyOut:
    """Delete expired objects; one audit row per key (AC: deletions logged)."""
    deleted = 0
    for key in _expired_keys(db, payload):
        removed = store.delete(key)
        deleted += removed
        db.add(
            AuditLog(
                actor=role.value,
                action="retention_delete",
                entity="object",
                # audit_log.entity_id is String(64); a sha256 hex digest is
                # exactly 64 chars while object keys can be longer.
                entity_id=sha256_hex(key.encode()),
                detail={"key": key, "deleted": removed},
            )
        )
    return ApplyOut(deleted=deleted)


@router.post("/backup/manifest", status_code=status.HTTP_201_CREATED)
def create_backup_manifest(
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> ManifestOut:
    """Fingerprint DB entities + session objects and persist the manifest.

    The manifest lands in the store under ``backups/manifest-{sha12}.json`` and
    an audit row records the run (US-B6 nightly backup wiring; see
    ``docs/runbooks/backup_restore.md``).
    """
    manifest = backup_manifest(snapshot_entities(db), store.list_keys("sessions"))
    document = ManifestDocument(
        sha256=manifest.sha256,
        entity_counts=manifest.entity_counts,
        object_count=manifest.object_count,
    )
    manifest_key = f"backups/manifest-{manifest.sha256[:12]}.json"
    store.put(manifest_key, document.model_dump_json().encode())
    db.add(
        AuditLog(
            actor=role.value,
            action="backup_manifest",
            entity="manifest",
            entity_id=manifest.sha256,
            detail={
                "manifest_key": manifest_key,
                "entity_counts": manifest.entity_counts,
                "object_count": manifest.object_count,
            },
        )
    )
    return ManifestOut(manifest_key=manifest_key, **document.model_dump())


@router.post("/backup/verify")
def verify_backup(
    payload: VerifyIn,
    db: Annotated[OrmSession, Depends(get_db)],
    store: Annotated[FsObjectStore, Depends(get_store)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> VerifyOut:
    """Restore drill: compare CURRENT db/store against a stored manifest.

    Run against a restored instance; an empty discrepancy list means the
    restore is bit-exact (US-B6 quarterly drill).
    """
    try:
        raw = store.get(payload.manifest_key)
    except StorageError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "manifest not found") from exc
    document = ManifestDocument.model_validate_json(raw)
    manifest = Manifest(
        sha256=document.sha256,
        entity_counts=document.entity_counts,
        object_count=document.object_count,
    )
    discrepancies = verify_restore(manifest, snapshot_entities(db), store.list_keys("sessions"))
    db.add(
        AuditLog(
            actor=role.value,
            action="backup_verify",
            entity="manifest",
            entity_id=manifest.sha256,
            detail={
                "manifest_key": payload.manifest_key,
                "ok": not discrepancies,
                "discrepancies": discrepancies,
            },
        )
    )
    return VerifyOut(ok=not discrepancies, discrepancies=discrepancies)
