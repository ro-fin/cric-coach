"""One-command detector train/eval job (US-F2): dataset version -> run + registry.

Resolves a **frozen** dataset version (US-F1 immutability: training on an
unfrozen dataset is refused), verifies ``dataset_dir`` really is the export of
that version (the data.yaml header ``scripts/export_dataset.py`` writes pins
version + manifest digest), drives the injected
:class:`~cricai_vision.train.TrainerProtocol` over the exported dataset
directory, and records the whole experiment first-party:

* a ``model_runs`` row walking pending -> running -> succeeded/failed, each
  transition committed separately so a crash mid-train leaves an honest
  ``running``/``failed`` row instead of a phantom success;
* the eval-report artifact (pinned headline keys, per-class rows, dataset
  version + digest, config echo) and the weights at
  ``models/{model_name}/runs/{run_id}/...`` in the object store;
* a candidate ``model_versions`` row — promotion happens through the models
  API's gated endpoint, never here.

Runs are append-only experiment history: re-running the command records a new
run rather than mutating an old one, and an explicit ``model_version`` that is
already registered fails fast **before** any training starts (the
(model_name, version) pair is immutable registry identity).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cricai_data.db import session_scope
from cricai_data.enums import ModelStage, TrainingStatus
from cricai_data.models import Dataset, ModelRun, ModelVersion, utcnow
from cricai_vision.train import RunResult, TrainerProtocol, TrainingError, build_eval_report
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cricai_worker.context import WorkerContext

#: Object-store keys embed the model name, so it must be path-safe.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: First data.yaml line scripts/export_dataset.py writes: pins version + digest.
#: Versions are free-form (the datasets API imposes no charset, and the export
#: writes them verbatim), so the version is everything up to the trailing
#: digest suffix — greedy ``.+`` anchors on the last "(manifest digest ...)".
_DATA_YAML_HEADER_RE = re.compile(
    r"^# cricAI dataset (?P<version>.+) \(manifest digest (?P<digest>[0-9a-f]*)\)$"
)


def run_key(model_name: str, run_id: uuid.UUID, filename: str) -> str:
    """Pinned object-store key of one run artifact (US-F2 storage layout)."""
    return f"models/{model_name}/runs/{run_id}/{filename}"


@dataclass(frozen=True)
class TrainingSpec:
    """What to train: registry identity, dataset provenance, trainer config."""

    model_name: str
    dataset_version: str
    dataset_dir: Path
    config: Mapping[str, Any]
    #: Registry version to register; None derives one from the run id.
    model_version: str | None = None


@dataclass(frozen=True)
class TrainingRunSummary:
    """What one training invocation recorded."""

    run_id: str
    model_name: str
    dataset_version: str
    model_version: str
    status: str
    metrics: dict[str, float]
    report_key: str
    weights_key: str


def _export_header(dataset_dir: Path) -> re.Match[str]:
    """The provenance header of the dir's data.yaml (version + manifest digest)."""
    data_yaml = dataset_dir / "data.yaml"
    try:
        lines = data_yaml.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:  # corrupt/binary files decode-fail
        raise TrainingError(
            f"cannot read {data_yaml}: {exc} — train on a scripts/export_dataset.py "
            "export so the dataset version is verifiable"
        ) from exc
    match = _DATA_YAML_HEADER_RE.match(lines[0]) if lines else None
    if match is None:
        raise TrainingError(
            f"{data_yaml} has no cricAI export header pinning its dataset version; "
            "re-export the dataset with scripts/export_dataset.py"
        )
    return match


def _verify_export_provenance(spec: TrainingSpec, dataset: Dataset) -> None:
    """dataset_dir must be the export of the version the run records (US-F2)."""
    header = _export_header(spec.dataset_dir)
    if header["version"] != spec.dataset_version:
        raise TrainingError(
            f"{spec.dataset_dir} exports dataset {header['version']!r}, not "
            f"{spec.dataset_version!r} — the recorded provenance must match the data "
            "actually trained on"
        )
    if header["digest"] != (dataset.manifest_digest or ""):
        raise TrainingError(
            f"{spec.dataset_dir} was exported at manifest digest {header['digest']!r} "
            f"but dataset {spec.dataset_version!r} is pinned at "
            f"{dataset.manifest_digest!r} — re-export the frozen version"
        )


def _create_pending_run(ctx: WorkerContext, spec: TrainingSpec, trainer_version: str) -> uuid.UUID:
    """Validate inputs and record the pending run (first observable transition)."""
    with session_scope(ctx.session_factory) as db:
        dataset = db.scalar(select(Dataset).where(Dataset.version == spec.dataset_version))
        if dataset is None:
            raise LookupError(f"dataset version not found: {spec.dataset_version}")
        if not dataset.frozen:
            raise TrainingError(
                f"dataset {spec.dataset_version!r} is not frozen; freeze it before training "
                "(US-F1: dataset versions are immutable and referenced by every model)"
            )
        _verify_export_provenance(spec, dataset)
        if spec.model_version is not None:
            existing = db.scalar(
                select(ModelVersion.id).where(
                    ModelVersion.model_name == spec.model_name,
                    ModelVersion.version == spec.model_version,
                )
            )
            if existing is not None:
                raise TrainingError(
                    f"model version {spec.model_name}:{spec.model_version} is already "
                    "registered; registry identity is immutable — pick a new version"
                )
        run = ModelRun(
            model_name=spec.model_name,
            dataset_id=dataset.id,
            config=dict(spec.config),
            status=TrainingStatus.PENDING,
            trainer_version=trainer_version,
        )
        db.add(run)
        db.flush()
        return run.id


def _mark(ctx: WorkerContext, run_id: uuid.UUID, status: TrainingStatus) -> None:
    """Commit one status transition so progress/failure is always observable."""
    with session_scope(ctx.session_factory) as db:
        run = db.get_one(ModelRun, run_id)  # created above; never deleted mid-job
        run.status = status
        if status is TrainingStatus.RUNNING:
            run.started_at = utcnow()
        else:
            run.finished_at = utcnow()


def _publish(
    ctx: WorkerContext,
    run_id: uuid.UUID,
    spec: TrainingSpec,
    result: RunResult,
    trainer_version: str,
) -> tuple[str, str]:
    """Upload weights + eval report; returns (report_key, weights_key)."""
    report = build_eval_report(
        model_name=spec.model_name,
        dataset_version=spec.dataset_version,
        trainer_version=trainer_version,
        config=spec.config,
        result=result,
    )
    weights_key = run_key(spec.model_name, run_id, result.weights_path.name)
    ctx.store.put(weights_key, result.weights_path.read_bytes())
    report_key = run_key(spec.model_name, run_id, "eval-report.json")
    payload = {**report, "artifacts": {"weights_key": weights_key}}
    ctx.store.put(report_key, json.dumps(payload, sort_keys=True).encode("utf-8"))
    return report_key, weights_key


def run_training(
    ctx: WorkerContext, spec: TrainingSpec, trainer: TrainerProtocol
) -> TrainingRunSummary:
    """Train + eval + record: ModelRun row, report/weights artifacts, candidate version."""
    if not _MODEL_NAME_RE.match(spec.model_name):
        raise TrainingError(
            f"model_name must match {_MODEL_NAME_RE.pattern} (it becomes an object key), "
            f"got {spec.model_name!r}"
        )
    run_id = _create_pending_run(ctx, spec, trainer.version)
    _mark(ctx, run_id, TrainingStatus.RUNNING)
    version = spec.model_version if spec.model_version is not None else run_id.hex[:12]
    try:
        result = trainer.train(spec.dataset_dir, spec.config)
        report_key, weights_key = _publish(ctx, run_id, spec, result, trainer.version)
        with session_scope(ctx.session_factory) as db:
            run = db.get_one(ModelRun, run_id)  # created above; never deleted mid-job
            run.metrics = dict(result.metrics)
            run.report_key = report_key
            run.status = TrainingStatus.SUCCEEDED
            run.finished_at = utcnow()
            # Same commit as the success mark: a succeeded run and its candidate
            # registration land atomically (no succeeded-but-unregistered limbo).
            db.add(
                ModelVersion(
                    model_name=spec.model_name,
                    version=version,
                    run_id=run_id,
                    stage=ModelStage.CANDIDATE,
                )
            )
    except IntegrityError as exc:
        # The duplicate-identity pre-check raced a concurrent registration across
        # the whole training window; session_scope already rolled the success
        # commit back, so the fresh session below can still mark the run FAILED.
        _mark(ctx, run_id, TrainingStatus.FAILED)
        raise TrainingError(
            f"model version {spec.model_name}:{version} was registered while this run "
            "trained; the run is marked failed — pick a new version and re-run"
        ) from exc
    except Exception:
        _mark(ctx, run_id, TrainingStatus.FAILED)  # honest history: never a phantom success
        raise
    return TrainingRunSummary(
        run_id=str(run_id),
        model_name=spec.model_name,
        dataset_version=spec.dataset_version,
        model_version=version,
        status=TrainingStatus.SUCCEEDED.value,
        metrics=dict(result.metrics),
        report_key=report_key,
        weights_key=weights_key,
    )
