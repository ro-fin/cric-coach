#!/usr/bin/env python3
"""One-command detector train/eval (US-F2): dataset version -> run + registry.

Usage:
    uv run scripts/train_detector.py --model-name ball-detector \
        --dataset-version v1 --dataset-dir datasets/v1
    uv run scripts/train_detector.py --model-name ball-detector \
        --dataset-version v1 --dataset-dir datasets/v1 \
        --config train.json --model-version 1.2.0 --work-dir runs/

Thin wrapper: parses args + the optional JSON config file, builds the env-driven
WorkerContext (CRICAI_DATABASE_URL / CRICAI_STORAGE_ROOT) and delegates to the
``cricai_worker.train_detector`` job, which records the run pending -> running ->
succeeded/failed, uploads the eval report + weights to the object store and
registers a candidate model version. ``--trainer fake`` (the default and only
trainer today) is the seeded deterministic trainer; the real ultralytics trainer
slots in behind the same flag at the real-footage milestone. Exits 2 with the
error message on validation failures (bad config or reserved trainer/seed keys
in it, unknown/unfrozen dataset, a dataset dir that is not the export of the
named version, already-registered version).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cricai_vision.train import FakeTrainer, TrainingError
from cricai_worker.context import WorkerContext
from cricai_worker.train_detector import TrainingSpec, run_training

#: Config keys the CLI owns: they record what actually ran (--trainer/--seed).
_RESERVED_CONFIG_KEYS = frozenset({"trainer", "seed"})


def _load_config(path: Path | None) -> dict[str, Any]:
    """The training config: a JSON object file, or {} when omitted."""
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise TrainingError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TrainingError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise TrainingError(f"{path}: config must be a JSON object, got {type(raw).__name__}")
    reserved = sorted(_RESERVED_CONFIG_KEYS & raw.keys())
    if reserved:
        raise TrainingError(
            f"{path}: config file sets reserved provenance keys {reserved}; pass "
            "--trainer/--seed instead (the recorded config must match what actually ran)"
        )
    return raw


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train + evaluate a detector and register a candidate version (US-F2)."
    )
    parser.add_argument("--model-name", required=True, help="registry model name")
    parser.add_argument(
        "--dataset-version", required=True, help="frozen dataset version to train on"
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        required=True,
        help="exported training-format directory of that dataset version",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="optional JSON object of trainer options"
    )
    parser.add_argument(
        "--trainer",
        choices=["fake"],
        default="fake",
        help="trainer implementation (real ultralytics arrives with the real-footage milestone)",
    )
    parser.add_argument("--seed", type=int, default=7, help="fake-trainer determinism seed")
    parser.add_argument(
        "--model-version",
        default=None,
        help="registry version to register (default: derived from the run id)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("runs"),
        help="where the trainer writes weights before upload",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        # CLI provenance keys last: nothing may shadow what actually ran.
        config = {**_load_config(args.config), "trainer": args.trainer, "seed": args.seed}
        if not args.dataset_dir.is_dir():
            raise TrainingError(f"dataset directory not found: {args.dataset_dir}")
        spec = TrainingSpec(
            model_name=args.model_name,
            dataset_version=args.dataset_version,
            dataset_dir=args.dataset_dir,
            config=config,
            model_version=args.model_version,
        )
        summary = run_training(
            WorkerContext.from_env(),
            spec,
            FakeTrainer(output_dir=args.work_dir, seed=args.seed),
        )
    except (TrainingError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"run {summary.run_id}: {summary.model_name} {summary.model_version} "
        f"({summary.status}, candidate) on dataset {summary.dataset_version} "
        f"map50_ball={summary.metrics['map50_ball']} report={summary.report_key}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
