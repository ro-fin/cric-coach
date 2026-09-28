#!/usr/bin/env python3
"""Export a session's sampled frames as Label Studio tasks (US-F1).

Usage:
    uv run scripts/export_label_tasks.py --session-id UUID --out tasks.json
    uv run scripts/export_label_tasks.py --session-id UUID --out tasks.json \
        --image-url-prefix "/data/local-files/?d="

The export half of the US-F1 labeling round trip (the write half is
``POST /annotations/import``): every frame the ``sample_frames`` job stored for
the session becomes one task embedding its full provenance under
``data.cricai`` — which the import endpoint requires — and any existing boxes
ride along as pre-labels with their exact normalized coordinates under
``meta.norm``, so untouched labels round-trip losslessly. ``data.image``
defaults to the frame's object key; ``--image-url-prefix`` prepends the
operator's Label Studio serving root (e.g. ``/data/local-files/?d=``).

Refuses (exit 2, loud) an unknown session, a session with no sampled frames
(run the ``sample_frames`` job first), a frame image missing from the object
store, and corrupt stored boxes. Reads DB/object-store config from the same
``CRICAI_*`` environment as the worker
(:class:`cricai_worker.context.WorkerContext`).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from cricai_data.labelio import LabelIOError, to_label_studio_task
from cricai_data.models import Annotation, FrameSample, Session
from cricai_worker.context import WorkerContext
from sqlalchemy import select


class ExportTasksError(RuntimeError):
    """Anything that must stop the export with a loud message."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a session's sampled frames as Label Studio tasks (US-F1)."
    )
    parser.add_argument("--session-id", required=True, help="UUID of the session to export")
    parser.add_argument("--out", type=Path, required=True, help="tasks JSON file to write")
    parser.add_argument(
        "--image-url-prefix",
        default=None,
        help="prefix mapping object keys to the labeling tool's serving root "
        '(e.g. "/data/local-files/?d="); default: the raw object key',
    )
    return parser


def export_label_tasks(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    out: Path,
    *,
    image_url_prefix: str | None = None,
) -> int:
    """Write the Label Studio tasks file; returns the number of tasks."""
    with ctx.session_factory() as db:
        if db.get(Session, session_id) is None:
            raise ExportTasksError(f"session not found: {session_id}")
        frames = db.scalars(
            select(FrameSample)
            .where(FrameSample.session_id == session_id)
            .order_by(FrameSample.camera_id, FrameSample.frame_no)
        ).all()
        if not frames:
            raise ExportTasksError(
                f"session {session_id} has no sampled frames: run the sample_frames job first"
            )
        tasks: list[dict[str, Any]] = []
        for frame in frames:
            if not ctx.store.exists(frame.object_key):
                raise ExportTasksError(f"frame image missing from store: {frame.object_key}")
            annotations = db.scalars(
                select(Annotation)
                .where(Annotation.frame_id == frame.id)
                .order_by(Annotation.created_at, Annotation.id)
            ).all()
            image_url = (
                None if image_url_prefix is None else f"{image_url_prefix}{frame.object_key}"
            )
            tasks.append(to_label_studio_task(frame, annotations, image_url=image_url))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tasks, indent=2) + "\n")
    return len(tasks)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        session_id = uuid.UUID(args.session_id)
    except ValueError:
        print(f"error: --session-id is not a UUID: {args.session_id}", file=sys.stderr)
        return 2
    try:
        count = export_label_tasks(
            WorkerContext.from_env(),
            session_id,
            args.out,
            image_url_prefix=args.image_url_prefix,
        )
    except (ExportTasksError, LabelIOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"exported {count} labeling tasks for session {session_id} to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
