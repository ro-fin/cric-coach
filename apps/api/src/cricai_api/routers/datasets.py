"""Labeling datasets & provenance API (US-F1).

Thin HTTP surface over :mod:`cricai_data.datasets`:

- ``POST /datasets`` — create a mutable dataset version;
- ``GET /datasets`` — versions with per-split frame + per-class label counts;
- ``POST /datasets/{id}/members`` / ``DELETE .../members/{frame_id}`` —
  membership mutations (409 on frozen datasets: versions are immutable);
- ``POST /datasets/{id}/freeze`` — pins the manifest digest; a test split
  sharing sessions with train/val is the US-F1 leakage AC and returns 409
  with the offending sessions;
- ``POST /annotations/import`` — the write half of the US-F1 labeling round
  trip: a Label Studio export file becomes ``annotations`` rows, replacing
  each imported frame's labels wholesale (the file is the source of truth —
  "fix the file, never hand-patch the DB"). Provenance must match the stored
  frame and frames pinned by a frozen dataset's digest refuse imports (409):
  fixes go into a new version;
- ``GET /frames/{frame_id}/provenance`` — frame -> session/ball/camera/
  annotations/dataset memberships (every label traces back to its ball).

Roles: every endpoint is PARENT/COACH. This is ML-ops plumbing, not player
content — and frame provenance names sessions of every player including
guests, which players must never see (US-L3), so there is no age-appropriate
player read here.
"""

import uuid
from typing import Annotated, Any

from cricai_data.datasets import (
    DatasetError,
    SplitLeakageError,
    add_members,
    create_dataset,
    dataset_counts,
    freeze_dataset,
    remove_members,
)
from cricai_data.enums import AnnotationSource, DatasetSplit, LabelClass, Role
from cricai_data.labelio import (
    FrameProvenance,
    ImportedBox,
    LabelIOError,
    from_label_studio,
)
from cricai_data.models import Annotation, Dataset, DatasetMember, FrameSample
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(tags=["datasets"])

WRITE_ROLES = (Role.PARENT, Role.COACH)


class DatasetCreateIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    notes: str | None = None


class SplitCountsOut(BaseModel):
    frames: int
    classes: dict[str, int]


class DatasetOut(BaseModel):
    id: uuid.UUID
    version: str
    notes: str | None
    frozen: bool
    manifest_digest: str | None
    counts: dict[str, SplitCountsOut]


class MemberIn(BaseModel):
    frame_id: uuid.UUID
    split: DatasetSplit


class MembersAddedOut(BaseModel):
    added: int


class FreezeOut(BaseModel):
    version: str
    frozen: bool
    manifest_digest: str


class LabelImportOut(BaseModel):
    """What one Label Studio import did (all-or-nothing across the file)."""

    frames: int  # tasks imported
    annotations: int  # boxes now stored for those frames
    replaced: int  # pre-existing rows deleted by the wholesale replace


class AnnotationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label_class: LabelClass
    cx: float
    cy: float
    w: float
    h: float
    annotator: str
    source: AnnotationSource


class MembershipOut(BaseModel):
    dataset_version: str
    split: DatasetSplit


class FrameProvenanceOut(BaseModel):
    """Everything a label traces back to (US-F1 provenance AC)."""

    frame_id: uuid.UUID
    session_id: uuid.UUID
    ball_no: int | None
    camera_id: str
    frame_no: int
    ts_ms: int
    object_key: str
    stratum: dict[str, Any]
    sampler_version: str
    annotations: list[AnnotationOut]
    datasets: list[MembershipOut]


def _dataset_or_404(db: OrmSession, dataset_id: uuid.UUID) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dataset not found")
    return dataset


def _dataset_out(db: OrmSession, dataset: Dataset) -> DatasetOut:
    counts = dataset_counts(db, dataset)
    return DatasetOut(
        id=dataset.id,
        version=dataset.version,
        notes=dataset.notes,
        frozen=dataset.frozen,
        manifest_digest=dataset.manifest_digest,
        counts={
            split: SplitCountsOut(frames=c.frames, classes=c.classes) for split, c in counts.items()
        },
    )


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
def create_dataset_version(
    payload: DatasetCreateIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> DatasetOut:
    try:
        dataset = create_dataset(db, version=payload.version, notes=payload.notes)
    except DatasetError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _dataset_out(db, dataset)


@router.get("/datasets")
def list_dataset_versions(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> list[DatasetOut]:
    datasets = db.scalars(select(Dataset).order_by(Dataset.created_at, Dataset.version)).all()
    return [_dataset_out(db, dataset) for dataset in datasets]


@router.post("/datasets/{dataset_id}/members")
def add_dataset_members(
    dataset_id: uuid.UUID,
    members: list[MemberIn],
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> MembersAddedOut:
    dataset = _dataset_or_404(db, dataset_id)
    try:
        added = add_members(db, dataset, [(m.frame_id, m.split) for m in members])
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DatasetError as exc:  # frozen or duplicate membership
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return MembersAddedOut(added=added)


@router.delete("/datasets/{dataset_id}/members/{frame_id}", status_code=204)
def remove_dataset_member(
    dataset_id: uuid.UUID,
    frame_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> None:
    dataset = _dataset_or_404(db, dataset_id)
    try:
        remove_members(db, dataset, [frame_id])
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DatasetError as exc:  # frozen: immutable version
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/datasets/{dataset_id}/freeze")
def freeze_dataset_version(
    dataset_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> FreezeOut:
    dataset = _dataset_or_404(db, dataset_id)
    try:
        digest = freeze_dataset(db, dataset)
    except SplitLeakageError as exc:
        # US-F1 AC: the test split must be session-disjoint from train/val.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": str(exc),
                "violations": [
                    {"session_id": v.session_id, "splits": list(v.splits)} for v in exc.violations
                ],
            },
        ) from exc
    except DatasetError as exc:  # already frozen, or empty
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return FreezeOut(version=dataset.version, frozen=dataset.frozen, manifest_digest=digest)


@router.post("/annotations/import")
def import_annotations(
    payload: list[dict[str, Any]],
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> LabelImportOut:
    """Persist a Label Studio export: each task's boxes replace its frame's rows.

    Replacement (not append) keeps re-imports of an edited file idempotent.
    Any malformed task, unknown frame, provenance mismatch, or frozen-pinned
    frame rejects the WHOLE file — nothing persists (fix the file and re-import,
    never hand-patch the DB). Frame-level hard-case tags (``blur``/``feed_exit``)
    parse fine but have no schema home yet, so they are not persisted.
    """
    try:
        frames = from_label_studio(payload)
    except LabelIOError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    seen: set[uuid.UUID] = set()
    stored = replaced = 0
    for imported in frames:
        frame = _frame_for_import(db, imported.provenance, seen)
        replaced += _replace_annotations(db, frame, imported.boxes)
        stored += len(imported.boxes)
    db.flush()
    return LabelImportOut(frames=len(frames), annotations=stored, replaced=replaced)


def _frame_for_import(db: OrmSession, prov: FrameProvenance, seen: set[uuid.UUID]) -> FrameSample:
    """Resolve one task's frame; every rejection is loud and rolls the file back."""
    try:
        frame_id = uuid.UUID(prov.frame_id)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{prov.frame_id!r} is not a valid frame id"
        ) from exc
    if frame_id in seen:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"duplicate task for frame {frame_id}"
        )
    seen.add(frame_id)
    frame = db.get(FrameSample, frame_id)
    if frame is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"frame not found: {frame_id}")
    # ts_ms/ball_no are pixel identity (the freeze digest pins them): a sampler
    # rewrite at the same session/camera/frame_no re-extracts different pixels,
    # so a file exported before the rewrite must be rejected, not guessed at.
    if (str(frame.session_id), frame.camera_id, frame.frame_no, frame.ts_ms, frame.ball_no) != (
        prov.session_id,
        prov.camera_id,
        prov.frame_no,
        prov.ts_ms,
        prov.ball_no,
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"provenance mismatch for frame {frame_id}: the file's "
            f"session/camera/frame_no/ts_ms/ball_no does not match the stored frame "
            f"(stale export over re-extracted pixels?); refusing to import",
        )
    frozen_versions = db.scalars(
        select(Dataset.version)
        .join(DatasetMember, DatasetMember.dataset_id == Dataset.id)
        .where(DatasetMember.frame_id == frame_id, Dataset.frozen.is_(True))
        .order_by(Dataset.version)
    ).all()
    if frozen_versions:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"frame {frame_id} is pinned by frozen dataset "
            f"{', '.join(frozen_versions)}: labels are part of the freeze digest; "
            "fixes go into a new dataset version",
        )
    return frame


def _replace_annotations(db: OrmSession, frame: FrameSample, boxes: tuple[ImportedBox, ...]) -> int:
    existing = db.scalars(select(Annotation).where(Annotation.frame_id == frame.id)).all()
    for row in existing:
        db.delete(row)
    for item in boxes:
        db.add(
            Annotation(
                frame_id=frame.id,
                label_class=item.box.label_class,
                cx=item.box.cx,
                cy=item.box.cy,
                w=item.box.w,
                h=item.box.h,
                annotator=item.annotator,
                source=item.source,
            )
        )
    return len(existing)


@router.get("/frames/{frame_id}/provenance")
def frame_provenance(
    frame_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> FrameProvenanceOut:
    frame = db.get(FrameSample, frame_id)
    if frame is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "frame not found")
    annotations = db.scalars(
        select(Annotation).where(Annotation.frame_id == frame_id).order_by(Annotation.created_at)
    ).all()
    memberships = db.execute(
        select(Dataset.version, DatasetMember.split)
        .join(DatasetMember, DatasetMember.dataset_id == Dataset.id)
        .where(DatasetMember.frame_id == frame_id)
        .order_by(Dataset.version)
    ).all()
    return FrameProvenanceOut(
        frame_id=frame.id,
        session_id=frame.session_id,
        ball_no=frame.ball_no,
        camera_id=frame.camera_id,
        frame_no=frame.frame_no,
        ts_ms=frame.ts_ms,
        object_key=frame.object_key,
        stratum=frame.stratum,
        sampler_version=frame.sampler_version,
        annotations=[AnnotationOut.model_validate(a) for a in annotations],
        datasets=[
            MembershipOut(dataset_version=version, split=split) for version, split in memberships
        ],
    )
