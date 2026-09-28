"""US-G2: coaching rules as versioned data — CRUD, approvals, per-player overrides.

Rules are append-only ``coaching_rules`` versions with ``author``/``rationale``
required and ``approved_by`` required non-null before a version may be enabled
(the AI's opinions are the coach's opinions, scaled). Per-player overrides
(disable / adjust) log reason + actor and write an AuditLog row. The run
endpoint assembles the session's canonical BallRecords (US-G1) and returns the
findings the active rules emit — it persists nothing; report generation owns
persistence.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from cricai_coaching.rules import RuleError, parse_rule_definition, run_rules
from cricai_data.ballrecord import session_ball_records
from cricai_data.enums import Role
from cricai_data.models import AuditLog, CoachingRule, Player, RuleOverride, Session
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/rules", tags=["rules"])

GUARDIANS = (Role.PARENT, Role.COACH)


class RuleVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_key: str
    version: int
    author: str
    approved_by: str | None
    rationale: str
    definition: dict[str, Any]
    enabled: bool
    created_at: datetime


class RuleCreate(BaseModel):
    rule_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    definition: dict[str, Any]
    author: str = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=1)
    approved_by: str | None = Field(default=None, max_length=64)
    enabled: bool = False


class OverrideCreate(BaseModel):
    player_id: uuid.UUID
    action: Literal["disable", "adjust"]
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)


class OverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rule_key: str
    player_id: uuid.UUID
    action: str
    params: dict[str, Any]
    reason: str
    actor: str
    created_at: datetime


class RunResult(BaseModel):
    session_id: uuid.UUID
    records: int
    rules_run: int
    findings: list[dict[str, Any]]


def _parse_or_422(definition: dict[str, Any]) -> None:
    try:
        parse_rule_definition(definition)
    except RuleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _versions(db: OrmSession, rule_key: str) -> list[CoachingRule]:
    return list(
        db.scalars(
            select(CoachingRule)
            .where(CoachingRule.rule_key == rule_key)
            .order_by(CoachingRule.version)
        )
    )


def _latest_or_404(db: OrmSession, rule_key: str) -> CoachingRule:
    versions = _versions(db, rule_key)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"rule not found: {rule_key}")
    return versions[-1]


def _audit(db: OrmSession, actor: str, action: str, rule_key: str, detail: dict[str, Any]) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            entity="coaching_rule",
            entity_id=rule_key,
            detail=detail,
        )
    )


@router.get("")
def list_rules(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
) -> list[RuleVersionOut]:
    """Latest version of every rule_key (US-G2: the coach reviews the catalog)."""
    rows = db.scalars(
        select(CoachingRule).order_by(CoachingRule.rule_key, CoachingRule.version)
    ).all()
    latest: dict[str, CoachingRule] = {row.rule_key: row for row in rows}
    return [RuleVersionOut.model_validate(row) for row in latest.values()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_rule_version(
    payload: RuleCreate,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
) -> RuleVersionOut:
    """Append a new rule version; versions are immutable history (US-G2)."""
    _parse_or_422(payload.definition)
    if payload.enabled and payload.approved_by is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a rule version cannot be enabled without approved_by"
        )
    existing = _versions(db, payload.rule_key)
    version = existing[-1].version + 1 if existing else 1
    row = CoachingRule(
        rule_key=payload.rule_key,
        version=version,
        author=payload.author,
        approved_by=payload.approved_by,
        rationale=payload.rationale,
        definition=payload.definition,
        enabled=payload.enabled,
    )
    db.add(row)
    _audit(
        db,
        role.value,
        "rule_version_created",
        payload.rule_key,
        {"version": version, "author": payload.author, "approved_by": payload.approved_by},
    )
    db.flush()
    return RuleVersionOut.model_validate(row)


@router.get("/overrides")
def list_overrides(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
    player_id: uuid.UUID | None = None,
    rule_key: str | None = None,
) -> list[OverrideOut]:
    """Override log (US-G2: overrides are logged), filterable by player/rule."""
    query = select(RuleOverride).order_by(RuleOverride.created_at)
    if player_id is not None:
        query = query.where(RuleOverride.player_id == player_id)
    if rule_key is not None:
        query = query.where(RuleOverride.rule_key == rule_key)
    return [OverrideOut.model_validate(row) for row in db.scalars(query)]


@router.post("/run/{session_id}")
def run_rules_for_session(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
) -> RunResult:
    """Run every active rule over the session's canonical BallRecords (US-G2).

    Applies the player's overrides; returns Finding-shaped dicts with exact
    ball IDs and per-camera clip evidence. Nothing is persisted here.
    """
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    records = session_ball_records(db, session_id)
    rules = db.scalars(select(CoachingRule)).all()
    overrides = db.scalars(
        select(RuleOverride).where(RuleOverride.player_id == session.player_id)
    ).all()
    findings = run_rules(rules, records, overrides)
    return RunResult(
        session_id=session_id,
        records=len(records),
        rules_run=len({r.rule_key for r in rules}),
        findings=findings,
    )


@router.get("/{rule_key}")
def get_rule_versions(
    rule_key: str,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
) -> list[RuleVersionOut]:
    """Full append-only version history of one rule."""
    versions = _versions(db, rule_key)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"rule not found: {rule_key}")
    return [RuleVersionOut.model_validate(row) for row in versions]


@router.post("/{rule_key}/enable")
def enable_rule(
    rule_key: str,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
) -> RuleVersionOut:
    """Enable the latest version — refused while it lacks ``approved_by``."""
    latest = _latest_or_404(db, rule_key)
    if latest.approved_by is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"rule {rule_key} v{latest.version} has no approved_by; approval is required",
        )
    latest.enabled = True
    _audit(db, role.value, "rule_enabled", rule_key, {"version": latest.version})
    db.flush()
    return RuleVersionOut.model_validate(latest)


@router.post("/{rule_key}/disable")
def disable_rule(
    rule_key: str,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
) -> RuleVersionOut:
    """Disable the latest version (retires the rule without rewriting history)."""
    latest = _latest_or_404(db, rule_key)
    latest.enabled = False
    _audit(db, role.value, "rule_disabled", rule_key, {"version": latest.version})
    db.flush()
    return RuleVersionOut.model_validate(latest)


@router.post("/{rule_key}/overrides", status_code=status.HTTP_201_CREATED)
def create_override(
    rule_key: str,
    payload: OverrideCreate,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*GUARDIANS))],
) -> OverrideOut:
    """Disable or adjust one rule for one player; reason + actor logged (US-G2)."""
    latest = _latest_or_404(db, rule_key)
    if db.get(Player, payload.player_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    if payload.action == "adjust":
        # the merged definition must still be a valid rule (US-G2)
        _parse_or_422({**latest.definition, **payload.params})
    row = RuleOverride(
        rule_key=rule_key,
        player_id=payload.player_id,
        action=payload.action,
        params=payload.params,
        reason=payload.reason,
        actor=role.value,
    )
    db.add(row)
    _audit(
        db,
        role.value,
        "rule_override_created",
        rule_key,
        {"player_id": str(payload.player_id), "action": payload.action, "reason": payload.reason},
    )
    db.flush()
    return OverrideOut.model_validate(row)
