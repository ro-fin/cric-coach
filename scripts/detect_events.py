#!/usr/bin/env python3
"""Detect ball events from a motion-energy envelope and write them to the DB (US-D1).

Usage:
    uv run scripts/detect_events.py --session-id UUID --energy-file energy.json --fps 120
    uv run scripts/detect_events.py --session-id UUID --energy-file energy.csv --fps 120 \
        --onsets-file onsets.csv

Thin wrapper: parses args + input files (JSON array or CSV of numbers), builds the
env-driven WorkerContext (CRICAI_DATABASE_URL / CRICAI_STORAGE_ROOT) and delegates
to the ``cricai_worker.detect_events`` job, which is idempotent - re-runs replace
prior auto events per the EVENT REPLACE RULE, never duplicate. Exits 2 with the
error message on validation failures (bad inputs, unknown session).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from cricai_vision.events import EventDetectionError
from cricai_worker.context import WorkerContext
from cricai_worker.detect_events import run_detection


def _parse_numbers(path: Path) -> list[float]:
    """Load a JSON array ([0.1, ...]) or CSV (comma/newline separated) of numbers."""
    try:
        text = path.read_text().strip()
    except OSError as exc:
        raise EventDetectionError(f"cannot read {path}: {exc}") from exc
    if text.startswith("["):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EventDetectionError(f"{path}: invalid JSON: {exc}") from exc
    else:
        raw = [item for line in text.splitlines() for item in line.split(",") if item.strip()]
    try:
        values = [float(item) for item in raw]
    except (TypeError, ValueError) as exc:
        raise EventDetectionError(f"{path}: expected numbers: {exc}") from exc
    if not values:
        raise EventDetectionError(f"{path}: no samples found")
    return values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect ball events from motion energy (+ optional audio onsets) (US-D1)."
    )
    parser.add_argument("--session-id", required=True, help="UUID of the session to detect for")
    parser.add_argument(
        "--energy-file",
        type=Path,
        required=True,
        help="JSON array or CSV of per-frame motion energy on the reference camera",
    )
    parser.add_argument("--fps", type=float, required=True, help="frame rate of the energy stream")
    parser.add_argument(
        "--onsets-file",
        type=Path,
        default=None,
        help="optional JSON array or CSV of audio onset times in ms",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        session_id = uuid.UUID(args.session_id)
    except ValueError:
        print(f"error: --session-id is not a UUID: {args.session_id}", file=sys.stderr)
        return 2
    try:
        energy = _parse_numbers(args.energy_file)
        onsets = (
            [round(value) for value in _parse_numbers(args.onsets_file)]
            if args.onsets_file is not None
            else None
        )
        summary = run_detection(
            WorkerContext.from_env(),
            session_id,
            energy,
            args.fps,
            audio_onsets_ms=onsets,
        )
    except (EventDetectionError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"session {summary.session_id}: {summary.detected} auto events "
        f"(replaced {summary.replaced}, preserved {summary.preserved}) "
        f"detector {summary.detector_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
