#!/usr/bin/env python3
"""Export a FROZEN dataset version to a YOLO directory layout (US-F1).

Usage:
    uv run scripts/export_dataset.py --version v1 --out ./datasets/v1

Produces the standard ultralytics-style tree::

    out/
      data.yaml            # class names in pinned index order + split dirs
      images/{split}/{session}-{camera}-{frame:06d}.jpg
      labels/{split}/{session}-{camera}-{frame:06d}.txt

Refuses (exit 2, loud) an unknown version, an unfrozen dataset (versions are
immutable and models must reference frozen ones only), a manifest digest that
no longer matches the stored membership (something mutated rows behind the
freeze), a frame image missing from the object store, and corrupt stored
boxes. Reads DB/object-store config from the same ``CRICAI_*`` environment as
the worker (:class:`cricai_worker.context.WorkerContext`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cricai_data.datasets import dataset_manifest, dataset_members, manifest_digest
from cricai_data.labelio import LabelIOError, to_yolo, yolo_class_names
from cricai_data.models import Annotation, Dataset
from cricai_data.storage import StorageError
from cricai_worker.context import WorkerContext
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession


class ExportError(RuntimeError):
    """Anything that must stop the export with a loud message."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a frozen dataset version to a YOLO directory layout (US-F1)."
    )
    parser.add_argument("--version", required=True, help="dataset version to export")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    return parser


def _frozen_dataset(db: OrmSession, version: str) -> Dataset:
    dataset = db.scalar(select(Dataset).where(Dataset.version == version))
    if dataset is None:
        raise ExportError(f"dataset version not found: {version}")
    if not dataset.frozen:
        raise ExportError(
            f"refusing to export unfrozen dataset {version}: freeze it first "
            "(models must train on immutable versions)"
        )
    recomputed = manifest_digest(dataset_manifest(db, dataset))
    if recomputed != dataset.manifest_digest:
        raise ExportError(
            f"manifest digest mismatch for {version}: stored {dataset.manifest_digest}, "
            f"recomputed {recomputed} — membership mutated after freeze"
        )
    return dataset


def _data_yaml(digest: str, version: str) -> str:
    # No `path` key on purpose: ultralytics resolves `path: .` against the
    # TRAINER'S CWD (Path('.').exists() is always True, so the yaml-relative
    # fallback never applies). Omitting the key makes the root fall back to
    # this file's own directory — correct from any CWD — while keeping the
    # bytes host/directory-independent (US-F2 reproducibility: the trainer's
    # dataset_digest hashes every exported byte; never an absolute path here).
    lines = [
        f"# cricAI dataset {version} (manifest digest {digest})",
        "train: images/train",
        "val: images/val",
        "test: images/test",
        "names:",
    ]
    lines.extend(f"  {index}: {name}" for index, name in enumerate(yolo_class_names()))
    return "".join(f"{line}\n" for line in lines)


def export_dataset(ctx: WorkerContext, version: str, out: Path) -> dict[str, int]:
    """Write the YOLO tree; returns exported frame counts per split."""
    counts: dict[str, int] = {}
    with ctx.session_factory() as db:
        dataset = _frozen_dataset(db, version)
        for frame, split in dataset_members(db, dataset):
            stem = f"{frame.session_id}-{frame.camera_id}-{frame.frame_no:06d}"
            try:
                image = ctx.store.get(frame.object_key)
            except StorageError as exc:
                raise ExportError(f"frame image missing from store: {exc}") from exc
            annotations = db.scalars(
                select(Annotation)
                .where(Annotation.frame_id == frame.id)
                .order_by(Annotation.created_at, Annotation.id)
            ).all()
            image_dir = out / "images" / split.value
            label_dir = out / "labels" / split.value
            image_dir.mkdir(parents=True, exist_ok=True)
            label_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(frame.object_key).suffix or ".jpg"
            (image_dir / f"{stem}{suffix}").write_bytes(image)
            (label_dir / f"{stem}.txt").write_text(to_yolo(annotations))
            counts[split.value] = counts.get(split.value, 0) + 1
        out.mkdir(parents=True, exist_ok=True)
        digest = dataset.manifest_digest or ""
        (out / "data.yaml").write_text(_data_yaml(digest, version))
    return counts


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        counts = export_dataset(WorkerContext.from_env(), args.version, args.out)
    except (ExportError, LabelIOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    per_split = ", ".join(f"{split}={n}" for split, n in sorted(counts.items()))
    print(f"exported dataset {args.version} to {args.out}: {per_split}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
