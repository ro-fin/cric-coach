"""Dataset assembly & versioning (US-F1): create, add members, check, freeze.

A dataset version is a named set of sampled frames with train/val/test split
assignments. Two acceptance criteria shape every function here:

1. **Session-disjoint test split.** The test split must share NO session with
   train or val — a ball from a labeled session leaking into test would let
   models memorize the exact net/lighting/ball of their eval data.
   :func:`split_violations` finds every offending session and
   :func:`freeze_dataset` refuses to freeze while any exist (train and val may
   share sessions; only test must be disjoint).
2. **Frozen versions are immutable.** Freezing computes ``manifest_digest``
   (SHA-256 over the canonical membership *and label* JSON,
   :func:`dataset_manifest` — every member's split, frame identity including
   pixel provenance ``ball_no``/``ts_ms``/``object_key``, and full annotation
   content) and flips ``frozen``; every mutation path — including a second
   freeze — refuses frozen datasets loudly with :class:`FrozenDatasetError`.
   Fixes go into a new version, so every trained model's dataset reference
   stays reproducible: a label or frame edited behind the freeze surfaces as a
   digest mismatch, never as a silent export drift.

Callers own the transaction: functions ``flush`` so errors surface here, but
never ``commit``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from cricai_data.enums import DatasetSplit
from cricai_data.models import Annotation, Dataset, DatasetMember, FrameSample


class DatasetError(ValueError):
    """Invalid dataset operation (duplicate version/member, empty freeze, ...)."""


class FrozenDatasetError(DatasetError):
    """Mutation attempted on a frozen (immutable) dataset version."""


@dataclass(frozen=True)
class SplitViolation:
    """One session leaking between the test split and train/val (US-F1 AC)."""

    session_id: str
    splits: tuple[str, ...]  # every split the session appears in, sorted


class SplitLeakageError(DatasetError):
    """Freeze refused: the test split shares sessions with train/val."""

    def __init__(self, violations: Sequence[SplitViolation]) -> None:
        self.violations = tuple(violations)
        sessions = ", ".join(v.session_id for v in self.violations)
        super().__init__(
            f"test split shares {len(self.violations)} session(s) with train/val: {sessions}"
        )


@dataclass(frozen=True)
class SplitCounts:
    """Per-split composition: member frames + annotation count per class."""

    frames: int
    classes: dict[str, int]


def create_dataset(db: OrmSession, *, version: str, notes: str | None = None) -> Dataset:
    """Create a new (mutable) dataset version; duplicate versions are refused."""
    if not version.strip():
        raise DatasetError("dataset version must be non-empty")
    if db.scalar(select(Dataset).where(Dataset.version == version)) is not None:
        raise DatasetError(f"dataset version already exists: {version}")
    dataset = Dataset(version=version, notes=notes)
    db.add(dataset)
    db.flush()
    return dataset


def _require_mutable(dataset: Dataset) -> None:
    if dataset.frozen:
        raise FrozenDatasetError(
            f"dataset {dataset.version} is frozen (digest {dataset.manifest_digest}); "
            "fixes go into a new version"
        )


def add_members(
    db: OrmSession,
    dataset: Dataset,
    members: Sequence[tuple[uuid.UUID, DatasetSplit]],
) -> int:
    """Add frames with split assignments; unknown frames and duplicates are loud."""
    _require_mutable(dataset)
    existing = set(
        db.scalars(select(DatasetMember.frame_id).where(DatasetMember.dataset_id == dataset.id))
    )
    for frame_id, split in members:
        if frame_id in existing:
            raise DatasetError(f"frame {frame_id} is already a member of {dataset.version}")
        if db.get(FrameSample, frame_id) is None:
            raise LookupError(f"frame not found: {frame_id}")
        db.add(DatasetMember(dataset_id=dataset.id, frame_id=frame_id, split=split))
        existing.add(frame_id)
    db.flush()
    return len(members)


def remove_members(db: OrmSession, dataset: Dataset, frame_ids: Sequence[uuid.UUID]) -> int:
    """Remove members; a frame that is not a member is a loud error."""
    _require_mutable(dataset)
    for frame_id in frame_ids:
        member = db.scalar(
            select(DatasetMember).where(
                DatasetMember.dataset_id == dataset.id, DatasetMember.frame_id == frame_id
            )
        )
        if member is None:
            raise LookupError(f"frame {frame_id} is not a member of {dataset.version}")
        db.delete(member)
    db.flush()
    return len(frame_ids)


def dataset_members(db: OrmSession, dataset: Dataset) -> list[tuple[FrameSample, DatasetSplit]]:
    """Membership with frames, in canonical (frame-id) order."""
    rows = db.execute(
        select(FrameSample, DatasetMember.split)
        .join(DatasetMember, DatasetMember.frame_id == FrameSample.id)
        .where(DatasetMember.dataset_id == dataset.id)
    ).all()
    return sorted(((frame, split) for frame, split in rows), key=lambda item: str(item[0].id))


def split_violations(db: OrmSession, dataset: Dataset) -> list[SplitViolation]:
    """Sessions the test split shares with train/val (must be empty to freeze)."""
    splits_by_session: dict[str, set[DatasetSplit]] = {}
    for frame, split in dataset_members(db, dataset):
        splits_by_session.setdefault(str(frame.session_id), set()).add(split)
    return [
        SplitViolation(session_id=session_id, splits=tuple(sorted(s.value for s in splits)))
        for session_id, splits in sorted(splits_by_session.items())
        if DatasetSplit.TEST in splits and splits & {DatasetSplit.TRAIN, DatasetSplit.VAL}
    ]


def dataset_manifest(db: OrmSession, dataset: Dataset) -> dict[str, Any]:
    """Canonical membership + label manifest — the exact bytes the freeze digest pins.

    Deterministic regardless of insertion order: members are sorted by frame id
    and each member's annotations by content, so the digest changes iff the
    membership, a split assignment, a member frame's identity — including its
    pixel provenance ``ball_no``/``ts_ms``/``object_key`` (a sampler rewrite at
    the same key moves ``ts_ms``) — or any label content changes. Floats are
    JSON-encoded with ``repr``, which round-trips exactly.
    """
    annotations = _annotations_by_frame(db, dataset)
    return {
        "dataset_version": dataset.version,
        "members": [
            {
                "frame_id": str(frame.id),
                "split": split.value,
                "session_id": str(frame.session_id),
                "ball_no": frame.ball_no,
                "camera_id": frame.camera_id,
                "frame_no": frame.frame_no,
                "ts_ms": frame.ts_ms,
                "object_key": frame.object_key,
                "annotations": annotations.get(frame.id, []),
            }
            for frame, split in dataset_members(db, dataset)
        ],
    }


def _annotations_by_frame(
    db: OrmSession, dataset: Dataset
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Member frames' label content, sorted canonically (never by row id/ctime)."""
    rows = db.scalars(
        select(Annotation)
        .join(DatasetMember, DatasetMember.frame_id == Annotation.frame_id)
        .where(DatasetMember.dataset_id == dataset.id)
    )
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row.frame_id, []).append(
            {
                "label_class": row.label_class.value,
                "cx": row.cx,
                "cy": row.cy,
                "w": row.w,
                "h": row.h,
                "annotator": row.annotator,
                "source": row.source.value,
            }
        )
    for entries in out.values():
        entries.sort(key=_annotation_sort_key)
    return out


def _annotation_sort_key(entry: dict[str, Any]) -> tuple[str, float, float, float, float, str, str]:
    return (
        entry["label_class"],
        entry["cx"],
        entry["cy"],
        entry["w"],
        entry["h"],
        entry["annotator"],
        entry["source"],
    )


def manifest_digest(manifest: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON encoding of a manifest."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def freeze_dataset(db: OrmSession, dataset: Dataset) -> str:
    """Freeze a version: enforce split disjointness, pin the digest, flip frozen.

    Refuses (loudly) an already-frozen dataset, an empty membership, and any
    test-split session leakage (:class:`SplitLeakageError` carries the exact
    violations so the caller can surface them).
    """
    _require_mutable(dataset)
    manifest = dataset_manifest(db, dataset)
    if not manifest["members"]:
        raise DatasetError(f"cannot freeze empty dataset {dataset.version}")
    violations = split_violations(db, dataset)
    if violations:
        raise SplitLeakageError(violations)
    dataset.manifest_digest = manifest_digest(manifest)
    dataset.frozen = True
    db.flush()
    return dataset.manifest_digest


def dataset_counts(db: OrmSession, dataset: Dataset) -> dict[str, SplitCounts]:
    """Per-split frame + per-class annotation counts (all splits, zeros kept)."""
    counts = {split.value: SplitCounts(frames=0, classes={}) for split in DatasetSplit}
    frame_rows = db.execute(
        select(DatasetMember.split, func.count())
        .where(DatasetMember.dataset_id == dataset.id)
        .group_by(DatasetMember.split)
    ).all()
    for split, frames in frame_rows:
        counts[split.value] = SplitCounts(frames=frames, classes=counts[split.value].classes)
    class_rows = db.execute(
        select(DatasetMember.split, Annotation.label_class, func.count())
        .join(Annotation, Annotation.frame_id == DatasetMember.frame_id)
        .where(DatasetMember.dataset_id == dataset.id)
        .group_by(DatasetMember.split, Annotation.label_class)
    ).all()
    for split, label_class, boxes in class_rows:
        counts[split.value].classes[label_class.value] = boxes
    return counts
