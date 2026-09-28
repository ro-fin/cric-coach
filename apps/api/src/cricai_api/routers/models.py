"""Model registry & promotion API (US-F2): first-party experiment tracking.

- ``POST /model-runs`` records one training/eval run (an externally-run trainer or
  the ``train_detector`` job reports here); the dataset version must be frozen —
  US-F1: runs pin immutable dataset versions, same rule the worker job enforces.
  ``POST /model-runs/{id}/transition`` walks the run through its lifecycle —
  pending -> running -> succeeded/failed, illegal jumps are 409s. Metrics land on
  the succeeded transition and must be 0-1 fractions (the pinned scale of the
  promotion gate).
- ``POST /model-versions`` registers a succeeded run as a candidate version;
  (model_name, version) is immutable registry identity.
- ``POST /model-versions/{id}/promote`` advances candidate -> staging ->
  production one step at a time. Promotion to production enforces the US-F2 ACs:
  the run must reference a still-frozen dataset and carry non-empty config, every
  pinned headline metric and the eval-report artifact key; the AUTO-BLOCK rule refuses (409,
  naming the metric) any promotion where a pinned headline metric regresses by
  more than 2.0 percentage points vs the current production version of the same
  model name — a missing metric on either side blocks just as loudly, never
  silently passes. The replaced production version drops back to staging (the
  rollback path), so exactly one production version exists per model name.

Reads are open to every role (model metrics carry no player data); writes and
promotions are parent/coach.
"""

import math
import uuid
from datetime import datetime
from typing import Annotated, Any

from cricai_data.enums import ModelStage, Role, TrainingStatus
from cricai_data.models import AuditLog, Dataset, ModelRun, ModelVersion, utcnow
from cricai_vision.train import HEADLINE_METRIC_KEYS
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(tags=["models"])

WRITE_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH)
READ_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH, Role.PLAYER)

#: AUTO-BLOCK threshold: > 2.0 percentage points regression (metrics stored 0-1).
MAX_HEADLINE_REGRESSION = 0.02

_LEGAL_TRANSITIONS: dict[TrainingStatus, frozenset[TrainingStatus]] = {
    TrainingStatus.PENDING: frozenset({TrainingStatus.RUNNING}),
    TrainingStatus.RUNNING: frozenset({TrainingStatus.SUCCEEDED, TrainingStatus.FAILED}),
    TrainingStatus.SUCCEEDED: frozenset(),
    TrainingStatus.FAILED: frozenset(),
}

_NEXT_STAGE: dict[ModelStage, ModelStage] = {
    ModelStage.CANDIDATE: ModelStage.STAGING,
    ModelStage.STAGING: ModelStage.PRODUCTION,
}


class RunIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_name: str = Field(min_length=1, max_length=64)
    dataset_version: str = Field(min_length=1)
    trainer_version: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)


class RunOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: uuid.UUID
    model_name: str
    dataset_version: str
    config: dict[str, Any]
    metrics: dict[str, Any]
    report_key: str | None
    status: TrainingStatus
    trainer_version: str
    started_at: datetime | None
    finished_at: datetime | None


class TransitionIn(BaseModel):
    status: TrainingStatus
    metrics: dict[str, float] | None = None
    report_key: str | None = None


class VersionIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    run_id: uuid.UUID


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: uuid.UUID
    model_name: str
    version: str
    run_id: uuid.UUID
    stage: ModelStage
    promoted_at: datetime | None
    promoted_by: str | None


class PromoteIn(BaseModel):
    target_stage: ModelStage


def _run_out(run: ModelRun, dataset_version: str) -> RunOut:
    return RunOut(
        id=run.id,
        model_name=run.model_name,
        dataset_version=dataset_version,
        config=run.config,
        metrics=run.metrics,
        report_key=run.report_key,
        status=run.status,
        trainer_version=run.trainer_version,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _run_or_404(db: OrmSession, run_id: uuid.UUID) -> ModelRun:
    run = db.get(ModelRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model run not found")
    return run


def _fraction_or_none(value: Any) -> float | None:
    """A stored metric usable by the gate: a finite number within [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return float(value)


@router.post("/model-runs", status_code=status.HTTP_201_CREATED)
def record_run(
    payload: RunIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> RunOut:
    dataset = db.scalar(select(Dataset).where(Dataset.version == payload.dataset_version))
    if dataset is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"dataset version not found: {payload.dataset_version}"
        )
    if not dataset.frozen:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"dataset {payload.dataset_version!r} is not frozen; freeze it before recording "
            "runs (US-F1: dataset versions are immutable and referenced by every model)",
        )
    run = ModelRun(
        model_name=payload.model_name,
        dataset_id=dataset.id,
        config=payload.config,
        status=TrainingStatus.PENDING,
        trainer_version=payload.trainer_version,
    )
    db.add(run)
    db.flush()
    return _run_out(run, dataset.version)


@router.get("/model-runs")
def list_runs(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    model_name: str | None = None,
    status_filter: Annotated[TrainingStatus | None, Query(alias="status")] = None,
) -> list[RunOut]:
    query = select(ModelRun, Dataset.version).join(Dataset, ModelRun.dataset_id == Dataset.id)
    if model_name is not None:
        query = query.where(ModelRun.model_name == model_name)
    if status_filter is not None:
        query = query.where(ModelRun.status == status_filter)
    rows = db.execute(query.order_by(ModelRun.created_at, ModelRun.id)).all()
    return [_run_out(run, dataset_version) for run, dataset_version in rows]


@router.get("/model-runs/{run_id}")
def get_run(
    run_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> RunOut:
    run = _run_or_404(db, run_id)
    return _run_out(run, db.get_one(Dataset, run.dataset_id).version)


@router.post("/model-runs/{run_id}/transition")
def transition_run(
    run_id: uuid.UUID,
    payload: TransitionIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> RunOut:
    """One lifecycle step: pending -> running -> succeeded/failed (illegal jumps 409)."""
    run = _run_or_404(db, run_id)
    if payload.status not in _LEGAL_TRANSITIONS[run.status]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"illegal transition {run.status.value} -> {payload.status.value}; "
            "runs walk pending -> running -> succeeded/failed",
        )
    if payload.status is not TrainingStatus.SUCCEEDED and (
        payload.metrics is not None or payload.report_key is not None
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "metrics/report_key are recorded on the succeeded transition only",
        )
    if payload.metrics is not None:
        for key, value in payload.metrics.items():
            if _fraction_or_none(value) is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"metric {key!r} must be a finite fraction within [0, 1], got {value!r}",
                )
        run.metrics = dict(payload.metrics)
    if payload.report_key is not None:
        run.report_key = payload.report_key
    run.status = payload.status
    if payload.status is TrainingStatus.RUNNING:
        run.started_at = utcnow()
    else:
        run.finished_at = utcnow()
    db.flush()
    return _run_out(run, db.get_one(Dataset, run.dataset_id).version)


@router.post("/model-versions", status_code=status.HTTP_201_CREATED)
def register_version(
    payload: VersionIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> VersionOut:
    run = _run_or_404(db, payload.run_id)
    if run.model_name != payload.model_name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"run {payload.run_id} trained {run.model_name!r}, not {payload.model_name!r}",
        )
    if run.status is not TrainingStatus.SUCCEEDED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"only succeeded runs can be registered; run is {run.status.value}",
        )
    existing = db.scalar(
        select(ModelVersion.id).where(
            ModelVersion.model_name == payload.model_name,
            ModelVersion.version == payload.version,
        )
    )
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"model version {payload.model_name}:{payload.version} is already registered",
        )
    version = ModelVersion(
        model_name=payload.model_name,
        version=payload.version,
        run_id=payload.run_id,
        stage=ModelStage.CANDIDATE,
    )
    db.add(version)
    db.flush()
    return VersionOut.model_validate(version)


@router.get("/model-versions")
def list_versions(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    model_name: str | None = None,
    stage: ModelStage | None = None,
) -> list[VersionOut]:
    query = select(ModelVersion)
    if model_name is not None:
        query = query.where(ModelVersion.model_name == model_name)
    if stage is not None:
        query = query.where(ModelVersion.stage == stage)
    rows = db.scalars(
        query.order_by(ModelVersion.model_name, ModelVersion.created_at, ModelVersion.version)
    ).all()
    return [VersionOut.model_validate(row) for row in rows]


def _require_production_ready(run: ModelRun) -> None:
    """US-F2 AC: every production model has dataset version, config, metrics, report."""
    missing: list[str] = []
    if not run.config:
        missing.append("config")
    missing.extend(
        f"metrics[{key!r}]"
        for key in HEADLINE_METRIC_KEYS
        if _fraction_or_none(run.metrics.get(key)) is None
    )
    if run.report_key is None:
        missing.append("report_key")
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot promote to production: run is missing {', '.join(missing)} "
            "(US-F2: every production model has dataset version, config, pinned "
            "headline metrics and an eval-report artifact)",
        )


def _enforce_no_regression(candidate_run: ModelRun, production_run: ModelRun) -> None:
    """AUTO-BLOCK: any pinned headline metric regressing > 2.0 points is a 409."""
    for key in HEADLINE_METRIC_KEYS:
        candidate = _fraction_or_none(candidate_run.metrics.get(key))
        production = _fraction_or_none(production_run.metrics.get(key))
        if candidate is None or production is None:
            side = "candidate" if candidate is None else "current production"
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"promotion blocked: {side} run has no usable {key!r} metric — "
                "missing headline metrics block promotion loudly (US-F2)",
            )
        # round() absorbs float noise so a regression of exactly 2.0 points passes.
        if round(production - candidate, 6) > MAX_HEADLINE_REGRESSION:
            points = round((production - candidate) * 100, 2)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"promotion blocked: {key} regresses {points} points vs current "
                f"production ({candidate:.4f} vs {production:.4f}); the limit is "
                f"{MAX_HEADLINE_REGRESSION * 100:.1f} points (US-F2 AUTO-BLOCK)",
            )


@router.post("/model-versions/{version_id}/promote")
def promote_version(
    version_id: uuid.UUID,
    payload: PromoteIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*WRITE_ROLES))],
) -> VersionOut:
    version = db.get(ModelVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model version not found")
    # Serialize concurrent promotions of one model name: row-lock every version of
    # the model (SELECT ... FOR UPDATE; a no-op on SQLite) so the stage read, the
    # regression gate and the demote+promote below are one atomic step — exactly
    # one production version can exist per name. populate_existing refreshes rows
    # already in the identity map (including ``version``) to the locked state.
    siblings = db.scalars(
        select(ModelVersion)
        .where(ModelVersion.model_name == version.model_name)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    expected = _NEXT_STAGE.get(version.stage)
    if expected is None or payload.target_stage is not expected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"illegal promotion {version.stage.value} -> {payload.target_stage.value}; "
            "stages advance candidate -> staging -> production one step at a time",
        )
    replaced: str | None = None
    if payload.target_stage is ModelStage.PRODUCTION:
        run = db.get_one(ModelRun, version.run_id)
        dataset = db.get_one(Dataset, run.dataset_id)
        if not dataset.frozen:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"cannot promote to production: dataset {dataset.version!r} is not frozen "
                "(US-F1/US-F2: production models pin an immutable dataset version)",
            )
        _require_production_ready(run)
        current = next((row for row in siblings if row.stage is ModelStage.PRODUCTION), None)
        if current is not None:
            _enforce_no_regression(run, db.get_one(ModelRun, current.run_id))
            current.stage = ModelStage.STAGING  # rollback path; one production per name
            replaced = current.version
            # uq_model_versions_one_production is non-deferrable, so the demotion
            # must reach PostgreSQL before the successor's stage update flushes.
            db.flush()
    version.stage = payload.target_stage
    version.promoted_at = utcnow()
    version.promoted_by = role.value
    db.add(
        AuditLog(
            actor=role.value,
            action="model_promote",
            entity="model_version",
            entity_id=str(version.id),
            detail={
                "model_name": version.model_name,
                "version": version.version,
                "to_stage": payload.target_stage.value,
                "replaced_production": replaced,
            },
        )
    )
    db.flush()
    return VersionOut.model_validate(version)
