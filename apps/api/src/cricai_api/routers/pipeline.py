"""US-J1/L1: session pipeline runs — trigger, status/trace, resume, re-derive.

- ``POST /pipeline/sessions/{id}/runs``: parent-only trigger. Runs the DAG
  synchronously in-process (the worker seam ``cricai_worker.pipeline`` — tests
  and the LAN box share it; RQ enqueueing uses the same job function). A held
  per-session advisory lock means another worker owns the session: 409, the
  caller requeues.
- ``POST /pipeline/sessions/{id}/resume``: same runner — completed stages
  short-circuit via input digests, so only lost/failed work re-executes.
- ``GET /pipeline/sessions/{id}/runs`` and ``GET /pipeline/runs/{run_id}``:
  run status plus the full per-attempt stage trace (US-J1 "full run trace").
- ``POST /pipeline/sessions/{id}/rederive``: parent-only re-derive cascade
  (Phase-3 debt). Without ``confirm_manual_invalidation`` it 409s with the
  structured blocker list when manual rows exist; with it, manual rows are
  deleted too and the deletion audited.

Tests may pin the stage wiring by setting ``app.state.pipeline_registry`` to a
``{stage: "module:function"}`` mapping (the runner's fake-injection seam).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from cricai_data.enums import Role, StageStatus
from cricai_data.models import PipelineRun, PipelineStage, Session
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import LOCKED, STAGES, PipelineOutcome, run_pipeline
from cricai_worker.rederive import RederiveBlockedError, SessionBusyError, rederive_session
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

#: Traces are an operator surface: guardian roles only (US-L3).
READ_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH)


class TriggerIn(BaseModel):
    """Run parameters; ``detector_context`` pins model versions in the trace."""

    detector_context: dict[str, Any] = Field(default_factory=dict)


class RederiveIn(BaseModel):
    confirm_manual_invalidation: bool = False
    start_run: bool = True  # re-enqueue the DAG right after the cascade


class StageOutcomeOut(BaseModel):
    stage: str
    status: StageStatus
    attempts: int
    resumed: bool
    error: str | None
    digest: str | None


class RunOut(BaseModel):
    run_id: uuid.UUID
    session_id: uuid.UUID
    status: str
    stages: list[StageOutcomeOut]


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    status: StageStatus
    started_at: datetime
    finished_at: datetime | None


class StageAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stage: str
    attempt: int
    status: StageStatus
    input_digest: str | None
    output: dict[str, Any] | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class RunDetailOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    status: StageStatus
    detector_context: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None
    stages: list[StageAttemptOut]


class RederiveOut(BaseModel):
    session_id: uuid.UUID
    counts: dict[str, Any]
    manual_invalidation: bool
    run: RunOut | None  # the fresh DAG run when start_run was requested
    run_locked: bool  # true when the fresh run was skipped (lock held)


def _session_or_404(db: OrmSession, session_id: uuid.UUID) -> Session:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _session_not_found(session_id: uuid.UUID) -> HTTPException:
    """404 for the mutating endpoints, whose runner reports an unknown session
    via ``LookupError`` (no ``get_db`` session is held across the in-process
    run, so its writes never share a connection with an open request session)."""
    return HTTPException(status.HTTP_404_NOT_FOUND, f"session {session_id} not found")


def _worker_ctx(request: Request) -> WorkerContext:
    """The API process doubles as the in-process runner (LAN box, tests)."""
    return WorkerContext(
        session_factory=request.app.state.session_factory,
        store=request.app.state.store,
    )


def _registry(request: Request) -> dict[str, str] | None:
    registry: dict[str, str] | None = getattr(request.app.state, "pipeline_registry", None)
    return registry


def _run_out(outcome: PipelineOutcome) -> RunOut:
    if outcome.run_id is None:  # locked: another worker holds the session
        raise HTTPException(status.HTTP_409_CONFLICT, "pipeline already running for this session")
    return RunOut(
        run_id=outcome.run_id,
        session_id=outcome.session_id,
        status=outcome.status,
        stages=[
            StageOutcomeOut(
                stage=s.stage,
                status=s.status,
                attempts=s.attempts,
                resumed=s.resumed,
                error=s.error,
                digest=s.digest,
            )
            for s in outcome.stages
        ],
    )


def _execute_run(request: Request, session_id: uuid.UUID, payload: TriggerIn) -> RunOut:
    try:
        outcome = run_pipeline(
            _worker_ctx(request),
            session_id,
            registry=_registry(request),
            detector_context=payload.detector_context,
        )
    except LookupError as exc:
        raise _session_not_found(session_id) from exc
    return _run_out(outcome)  # locked -> 409 inside _run_out


@router.post("/sessions/{session_id}/runs", status_code=status.HTTP_201_CREATED)
def trigger_run(
    session_id: uuid.UUID,
    payload: TriggerIn,
    request: Request,
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> RunOut:
    """Run the session DAG now (US-J1); 409 when another worker holds it."""
    return _execute_run(request, session_id, payload)


@router.post("/sessions/{session_id}/resume", status_code=status.HTTP_201_CREATED)
def resume_run(
    session_id: uuid.UUID,
    payload: TriggerIn,
    request: Request,
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> RunOut:
    """Resume-from-failure (US-L1): succeeded stages short-circuit by digest."""
    return _execute_run(request, session_id, payload)


@router.get("/sessions/{session_id}/runs")
def list_runs(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> list[RunSummaryOut]:
    _session_or_404(db, session_id)
    runs = db.scalars(
        select(PipelineRun)
        .where(PipelineRun.session_id == session_id)
        .order_by(PipelineRun.started_at)
    ).all()
    return [RunSummaryOut.model_validate(run) for run in runs]


def _stage_order(row: PipelineStage) -> tuple[int, int]:
    index = STAGES.index(row.stage) if row.stage in STAGES else len(STAGES)
    return (index, row.attempt)


@router.get("/runs/{run_id}")
def run_detail(
    run_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> RunDetailOut:
    """Full trace: every stage attempt with timings, digests and errors."""
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pipeline run not found")
    rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == run_id)).all()
    return RunDetailOut(
        id=run.id,
        session_id=run.session_id,
        status=run.status,
        detector_context=run.detector_context,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stages=[StageAttemptOut.model_validate(row) for row in sorted(rows, key=_stage_order)],
    )


@router.post("/sessions/{session_id}/rederive")
def rederive(
    session_id: uuid.UUID,
    payload: RederiveIn,
    request: Request,
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> RederiveOut:
    """Delete machine-derived dependents and re-run the DAG (Phase-3 debt)."""
    try:
        summary = rederive_session(
            _worker_ctx(request),
            session_id,
            actor=role.value,
            confirm_manual_invalidation=payload.confirm_manual_invalidation,
        )
    except LookupError as exc:
        raise _session_not_found(session_id) from exc
    except RederiveBlockedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "blockers": exc.blockers},
        ) from exc
    except SessionBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    run: RunOut | None = None
    run_locked = False
    if payload.start_run:
        outcome = run_pipeline(_worker_ctx(request), session_id, registry=_registry(request))
        if outcome.status == LOCKED:
            run_locked = True
        else:
            run = _run_out(outcome)
    return RederiveOut(
        session_id=session_id,
        counts=summary.counts(),
        manual_invalidation=summary.manual_invalidation,
        run=run,
        run_locked=run_locked,
    )
