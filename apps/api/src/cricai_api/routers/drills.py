"""US-J3: drill library and daily drill plans.

- Drill CRUD: coach-only create/edit (US-J3: drills are coach-authored data);
  ``machine_settings`` must validate against the machine envelope
  (:mod:`cricai_coaching.planner_agent`) before a drill is stored — settings
  the machine cannot actually be configured to are a 422, never library rows.
  Names are the drill identity (unique) and are not renamed by PATCH.
- ``GET /drills/plans/{player_id}/{plan_date}``: the stored plan for a day.
- ``POST /drills/plans/{player_id}/{plan_date}/generate``: builds tomorrow's
  plan from a session's stored findings via the planner agent. The remaining
  US-H1 bowling allowance and the US-H5 SafetyVerdict are derived SERVER-SIDE
  from the player's real ledger/wellness rows as of ``plan_date`` (SAF, via
  :func:`cricai_api.services.safety_state.compute_safety_state`). A client
  ``safety`` verdict is ignored entirely, and a client ``bowling_allowance_balls``
  can only RESTRICT the server allowance (``min``), never raise it; an active
  ``workload_ceiling``/``pain_flag`` verdict hard-blocks bowling regardless.
- ``PUT /drills/plans/{player_id}/{plan_date}``: coach edit/replace. Edited
  plans run the same publish-time validator against the SERVER verdict/allowance
  — a plan violating H1/H4 state is rejected (422), never stored (US-H5 AC) —
  and every edit writes an AuditLog row so future planning weights can learn
  from coach corrections (US-J3 AC).

SAF override: the phase-5 plan classified the H1 allowance and H5 verdict as
injected request inputs here; the adversarial review showed that let a parent
bypass the coach-gated ceilings, so both are now computed from server state.
"""

import uuid
from datetime import date
from typing import Annotated, Any

from cricai_coaching.analysis_agent import DEFAULT_ANALYSIS_CONFIG, rank_findings
from cricai_coaching.planner_agent import (
    PlanContext,
    PlannerError,
    build_plan,
    validate_machine_settings,
    validate_plan,
)
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_data.enums import BlockIntent, Role
from cricai_data.models import AuditLog, Drill, DrillPlan, Finding, Player, SafetyConfig, Session
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.services.safety_state import SafetyState, compute_safety_state

router = APIRouter(prefix="/drills", tags=["drills"])

#: All roles may read drills and plans; writes are role-gated per endpoint.
READ_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH, Role.PLAYER)


class DrillIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    setup: str
    machine_settings: dict[str, Any] = Field(default_factory=dict)
    ball_count: int = Field(ge=1)
    target_metric: str = Field(min_length=1, max_length=64)
    intent: BlockIntent
    enabled: bool = True


class DrillPatch(BaseModel):
    setup: str | None = None
    machine_settings: dict[str, Any] | None = None
    ball_count: int | None = Field(default=None, ge=1)
    target_metric: str | None = Field(default=None, min_length=1, max_length=64)
    intent: BlockIntent | None = None
    enabled: bool | None = None


class DrillOut(BaseModel):
    id: uuid.UUID
    name: str
    setup: str
    machine_settings: dict[str, Any]
    ball_count: int
    target_metric: str
    intent: BlockIntent
    author: str
    enabled: bool


class PlanGenerateIn(BaseModel):
    """Planner inputs: findings source. The H1 allowance and H5 verdict are
    derived server-side (SAF); ``bowling_allowance_balls`` may only restrict the
    server allowance and ``safety`` is accepted for back-compat but ignored."""

    session_id: uuid.UUID
    bowling_allowance_balls: int | None = Field(default=None, ge=0)
    bowling_request_balls: int | None = Field(default=None, ge=0)
    safety: dict[str, Any] | None = None


class PlanEditIn(BaseModel):
    """A coach's replacement blocks, revalidated against SERVER-derived H1/H5
    state; ``bowling_allowance_balls`` may only restrict and ``safety`` is
    ignored (both kept for request back-compat)."""

    blocks: list[dict[str, Any]]
    bowling_allowance_balls: int | None = Field(default=None, ge=0)
    safety: dict[str, Any] | None = None


class PlanOut(BaseModel):
    player_id: uuid.UUID
    plan_date: date
    blocks: list[dict[str, Any]]
    finding_ids: list[str]
    safety: dict[str, Any]
    safety_sha256: str | None
    created_by: str


def _drill_out(row: Drill) -> DrillOut:
    return DrillOut(
        id=row.id,
        name=row.name,
        setup=row.setup,
        machine_settings=row.machine_settings,
        ball_count=row.ball_count,
        target_metric=row.target_metric,
        intent=row.intent,
        author=row.author,
        enabled=row.enabled,
    )


def _plan_out(row: DrillPlan) -> PlanOut:
    return PlanOut(
        player_id=row.player_id,
        plan_date=row.plan_date,
        blocks=row.blocks,
        finding_ids=row.finding_ids,
        safety=row.safety,
        safety_sha256=row.safety_sha256,
        created_by=row.created_by,
    )


def _effective_allowance(state: SafetyState, client_allowance: int | None) -> int:
    """The server H1 allowance, further RESTRICTED (never raised) by any client
    value — a caller can ask for less bowling than the server permits, never
    more (SAF)."""
    server = state.bowling_allowance
    return server if client_allowance is None else min(server, client_allowance)


def _check_machine_settings(settings: dict[str, Any]) -> None:
    problems = validate_machine_settings(settings)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(problems))


def _audit(
    db: OrmSession, role: Role, action: str, entity: str, entity_id: str, **detail: Any
) -> None:
    db.add(
        AuditLog(actor=role.value, action=action, entity=entity, entity_id=entity_id, detail=detail)
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_drill(
    payload: DrillIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.COACH))],
) -> DrillOut:
    _check_machine_settings(payload.machine_settings)
    if db.scalar(select(Drill).where(Drill.name == payload.name)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"drill {payload.name!r} already exists")
    row = Drill(
        name=payload.name,
        setup=payload.setup,
        machine_settings=payload.machine_settings,
        ball_count=payload.ball_count,
        target_metric=payload.target_metric,
        intent=payload.intent,
        author=role.value,
        enabled=payload.enabled,
    )
    db.add(row)
    db.flush()
    _audit(db, role, "drill_create", "drill", str(row.id), name=row.name)
    return _drill_out(row)


@router.patch("/{drill_id}")
def edit_drill(
    drill_id: uuid.UUID,
    payload: DrillPatch,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.COACH))],
) -> DrillOut:
    row = db.get(Drill, drill_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "drill not found")
    if payload.machine_settings is not None:
        _check_machine_settings(payload.machine_settings)
    changed = sorted(payload.model_fields_set)
    for name in changed:
        value = getattr(payload, name)
        if value is not None:
            setattr(row, name, value)
    db.flush()
    _audit(db, role, "drill_edit", "drill", str(row.id), fields=changed)
    return _drill_out(row)


@router.get("")
def list_drills(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> list[DrillOut]:
    rows = db.scalars(select(Drill).order_by(Drill.name)).all()
    return [_drill_out(row) for row in rows]


def _player_or_404(db: OrmSession, player_id: uuid.UUID) -> Player:
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    return player


@router.get("/plans/{player_id}/{plan_date}")
def get_plan(
    player_id: uuid.UUID,
    plan_date: date,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> PlanOut:
    _player_or_404(db, player_id)
    row = db.scalar(
        select(DrillPlan).where(DrillPlan.player_id == player_id, DrillPlan.plan_date == plan_date)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no plan for this player and date")
    return _plan_out(row)


def _batting_split(db: OrmSession) -> dict[str, Any]:
    """The latest approved batting_split config; the code seed is the fallback
    shape reference when no ``safety_configs`` row exists (fresh test DBs)."""
    latest = db.scalars(select(SafetyConfig).order_by(SafetyConfig.version.desc())).first()
    config = latest.config if latest is not None else DEFAULT_SAFETY_CONFIG
    split = config.get("batting_split", DEFAULT_SAFETY_CONFIG["batting_split"])
    return dict(split)


def _finding_dict(row: Finding) -> dict[str, Any]:
    """One stored finding as the pinned contract-#2 dict shape."""
    return {
        "finding_id": str(row.id),
        "agent": row.agent,
        "rule_key": row.rule_key,
        "kind": row.kind,
        "severity": row.severity,
        "metric": row.metric,
        "condition": row.condition,
        "n": row.n,
        "effect_size": row.effect_size,
        "confidence": row.confidence,
        "ball_ids": row.ball_ids,
        "evidence": row.evidence,
        "text_data": row.payload.get("text_data", {}),
    }


def _drill_seam_dict(row: Drill) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "target_metric": row.target_metric,
        "intent": row.intent.value,
        "enabled": row.enabled,
        "machine_settings": row.machine_settings,
    }


def _upsert_plan(
    db: OrmSession,
    player_id: uuid.UUID,
    plan_date: date,
    *,
    blocks: list[dict[str, Any]],
    finding_ids: list[str],
    safety: dict[str, Any] | None,
    safety_sha256: str | None,
    actor: str,
) -> tuple[DrillPlan, bool]:
    row = db.scalar(
        select(DrillPlan).where(DrillPlan.player_id == player_id, DrillPlan.plan_date == plan_date)
    )
    created = row is None
    if row is None:
        row = DrillPlan(player_id=player_id, plan_date=plan_date)
        db.add(row)
    row.blocks = blocks
    row.finding_ids = finding_ids
    row.safety = safety if safety is not None else {}
    row.safety_sha256 = safety_sha256
    row.created_by = actor
    try:
        db.flush()
    except IntegrityError as exc:
        # A concurrent writer (the pipeline planner stage or another request) can
        # win the (player_id, plan_date) uq race between the select-miss above and
        # this INSERT. Surface a clear 409 rather than an opaque 500 so the caller
        # re-fetches the just-written plan and retries (finding 6, API half).
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "plan already exists for this date, retry"
        ) from exc
    return row, created


@router.post("/plans/{player_id}/{plan_date}/generate")
def generate_plan(
    player_id: uuid.UUID,
    plan_date: date,
    payload: PlanGenerateIn,
    response: Response,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> PlanOut:
    """Generate (or regenerate) one day's plan from a session's findings."""
    player = _player_or_404(db, player_id)
    session = db.get(Session, payload.session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    if session.player_id != player_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "session belongs to another player"
        )
    finding_rows = db.scalars(select(Finding).where(Finding.session_id == payload.session_id)).all()
    findings = rank_findings(
        [_finding_dict(row) for row in finding_rows], top_k=DEFAULT_ANALYSIS_CONFIG.top_k
    )
    drills = [
        _drill_seam_dict(row)
        for row in db.scalars(select(Drill).where(Drill.enabled.is_(True)).order_by(Drill.name))
    ]
    # SAF: verdict and allowance come from server state as of the plan date; the
    # client's ``safety`` is ignored and its allowance can only restrict.
    state = compute_safety_state(db, player, plan_date)
    context = PlanContext(
        split=_batting_split(db),
        bowling_allowance_balls=_effective_allowance(state, payload.bowling_allowance_balls),
        safety=state.verdict,
    )
    try:
        plan = build_plan(
            findings, drills, context, bowling_request_balls=payload.bowling_request_balls
        )
    except PlannerError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    row, created = _upsert_plan(
        db,
        player_id,
        plan_date,
        blocks=plan.blocks,
        finding_ids=plan.finding_ids,
        safety=plan.safety,
        safety_sha256=plan.safety_sha256,
        actor=role.value,
    )
    _audit(
        db,
        role,
        "plan_generate",
        "drill_plan",
        str(row.id),
        session_id=str(payload.session_id),
        finding_ids=plan.finding_ids,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _plan_out(row)


@router.put("/plans/{player_id}/{plan_date}")
def edit_plan(
    player_id: uuid.UUID,
    plan_date: date,
    payload: PlanEditIn,
    response: Response,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.COACH))],
) -> PlanOut:
    """Coach edit/replace (US-J3): same publish-time constraints, audited."""
    player = _player_or_404(db, player_id)
    # SAF: the verdict and allowance are the server's own state as of the plan
    # date; the client's ``safety`` verdict is ignored (never stored) and its
    # allowance can only restrict, so a bowling block a coach adds while pain or
    # a ceiling is active in the DB is rejected, never stored.
    state = compute_safety_state(db, player, plan_date)
    verdict = state.verdict
    violations = validate_plan(
        payload.blocks,
        PlanContext(
            split=_batting_split(db),
            bowling_allowance_balls=_effective_allowance(state, payload.bowling_allowance_balls),
            safety=verdict,
        ),
    )
    if violations:  # US-H5: constraint-violating plans are rejected, never stored
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(violations))
    finding_ids = [
        str(block["finding_id"]) for block in payload.blocks if block.get("drill_id") is not None
    ]
    row, created = _upsert_plan(
        db,
        player_id,
        plan_date,
        blocks=payload.blocks,
        finding_ids=finding_ids,
        safety=verdict,
        safety_sha256=verdict["sha256"],
        actor=role.value,
    )
    # Edits inform future planning weights (US-J3): the audit trail records them.
    _audit(
        db,
        role,
        "plan_edit",
        "drill_plan",
        str(row.id),
        n_blocks=len(payload.blocks),
        finding_ids=finding_ids,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _plan_out(row)
