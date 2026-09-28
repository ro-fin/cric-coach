"""US-H1/H2: bowling ledger CRUD with audits, rolling-7 summaries, safety-config versions.

Ledger writes are guardian-only (parent/coach) and every mutation writes an
AuditLog row (old→new). Only manual entries can be deleted — auto-backfill
rows belong to the ``cricai_worker.backfill_workload`` job, which regenerates
them. Safety-config versions are append-only; RAISING any ceiling (detected by
``cricai_coaching.workload.detect_ceiling_raises`` against the previous
version) requires the coach role — a parent may only lower. When the table is
empty (un-migrated test DBs; production is seeded at v1), reads fall back to
the canonical defaults and the first POSTed version is raise-compared against
those same defaults, so v1 cannot smuggle a raised ceiling past the gate.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_coaching.workload import (
    SOURCE_MANUAL,
    detect_ceiling_raises,
    summarize_windows,
    validate_safety_config,
)
from cricai_data.enums import DeliveryIntensity, Role, SafetyCode
from cricai_data.models import AuditLog, BowlingLedgerEntry, Player, SafetyConfig, Session
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/workload", tags=["workload"])

#: ``version`` reported when serving canonical defaults from an unseeded DB.
UNSEEDED_VERSION = 0


class LedgerEntryIn(BaseModel):
    entry_date: date
    balls: int = Field(ge=1)
    intensity: DeliveryIntensity
    session_id: uuid.UUID | None = None
    note: str | None = None


class LedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    entry_date: date
    balls: int
    intensity: DeliveryIntensity
    session_id: uuid.UUID | None
    source: str
    note: str | None
    created_by: str
    created_at: datetime


class WindowSummaryOut(BaseModel):
    window_start: date
    window_end: date
    weighted_balls: float
    weighted_overs: float
    bowling_days: list[date]
    consecutive_day_pairs: int
    band_max_age: int | None
    ceiling_overs: float | None
    violations: list[SafetyCode]
    remaining_balls: int | None


class SafetyConfigIn(BaseModel):
    config: dict[str, Any]
    reason: str = Field(min_length=1)


class SafetyConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    config: dict[str, Any]
    approved_by: str
    reason: str
    created_at: datetime | None


def _get_player_or_404(db: OrmSession, player_id: uuid.UUID) -> Player:
    player = db.get(Player, player_id)
    if player is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    return player


def _entry_detail(entry: BowlingLedgerEntry) -> dict[str, Any]:
    """JSON-safe snapshot of a ledger row for AuditLog old→new details.

    The free-text ``note`` is egress PII (privacy-delete classifies it so); the
    audit keeps only its presence and a sha256, never the verbatim text, so the
    deletion round-trip stays complete (US-L3, forward-fix)."""
    note = entry.note
    return {
        "player_id": str(entry.player_id),
        "entry_date": entry.entry_date.isoformat(),
        "balls": entry.balls,
        "intensity": entry.intensity.value,
        "session_id": None if entry.session_id is None else str(entry.session_id),
        "source": entry.source,
        "note_present": note is not None,
        "note_sha256": None if note is None else hashlib.sha256(note.encode("utf-8")).hexdigest(),
    }


def _latest_config_row(db: OrmSession) -> SafetyConfig | None:
    return db.scalar(select(SafetyConfig).order_by(SafetyConfig.version.desc()).limit(1))


def _active_config(db: OrmSession) -> tuple[int, dict[str, Any]]:
    """(version, config) that currently governs — canonical defaults when unseeded."""
    row = _latest_config_row(db)
    if row is None:
        return UNSEEDED_VERSION, DEFAULT_SAFETY_CONFIG
    return row.version, row.config


@router.post("/players/{player_id}/ledger", status_code=status.HTTP_201_CREATED)
def create_ledger_entry(
    player_id: uuid.UUID,
    payload: LedgerEntryIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> LedgerEntryOut:
    """Record a manual bowling-ledger entry (US-H1: manual corrections audited)."""
    _get_player_or_404(db, player_id)
    if payload.session_id is not None:
        session = db.get(Session, payload.session_id)
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
        if session.player_id != player_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "session belongs to a different player")
    entry = BowlingLedgerEntry(
        player_id=player_id,
        entry_date=payload.entry_date,
        balls=payload.balls,
        intensity=payload.intensity,
        session_id=payload.session_id,
        source=SOURCE_MANUAL,
        note=payload.note,
        created_by=role.value,
    )
    db.add(entry)
    db.flush()
    db.add(
        AuditLog(
            actor=role.value,
            action="ledger_create",
            entity="bowling_ledger_entry",
            entity_id=str(entry.id),
            detail={"old": None, "new": _entry_detail(entry)},
        )
    )
    return LedgerEntryOut.model_validate(entry)


@router.get("/players/{player_id}/ledger")
def list_ledger_entries(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> list[LedgerEntryOut]:
    """List a player's ledger entries, optionally restricted to [start, end]."""
    _get_player_or_404(db, player_id)
    query = select(BowlingLedgerEntry).where(BowlingLedgerEntry.player_id == player_id)
    if start is not None:
        query = query.where(BowlingLedgerEntry.entry_date >= start)
    if end is not None:
        query = query.where(BowlingLedgerEntry.entry_date <= end)
    query = query.order_by(BowlingLedgerEntry.entry_date, BowlingLedgerEntry.created_at)
    return [LedgerEntryOut.model_validate(entry) for entry in db.scalars(query)]


@router.delete("/ledger/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ledger_entry(
    entry_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> None:
    """Delete a MANUAL entry (audited). Auto-backfill rows are job-owned: 409."""
    entry = db.get(BowlingLedgerEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ledger entry not found")
    if entry.source != SOURCE_MANUAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "only manual entries can be deleted; auto-backfill rows are regenerated"
            " by the backfill job",
        )
    db.add(
        AuditLog(
            actor=role.value,
            action="ledger_delete",
            entity="bowling_ledger_entry",
            entity_id=str(entry.id),
            detail={"old": _entry_detail(entry), "new": None},
        )
    )
    db.delete(entry)


@router.get("/players/{player_id}/summary")
def rolling_summary(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    end: Annotated[date | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=90)] = 1,
) -> list[WindowSummaryOut]:
    """Rolling-7 windows ending on ``end`` (default today, UTC), newest first:
    weighted overs, violations and the remaining allowance (US-H1)."""
    player = _get_player_or_404(db, player_id)
    end_date = end if end is not None else datetime.now(tz=UTC).date()
    _version, config = _active_config(db)
    entries = list(
        db.scalars(select(BowlingLedgerEntry).where(BowlingLedgerEntry.player_id == player_id))
    )
    summaries = summarize_windows(
        entries, birthdate=player.birthdate, end=end_date, days=days, config=config
    )
    return [
        WindowSummaryOut(
            window_start=summary.window_start,
            window_end=summary.window_end,
            weighted_balls=summary.weighted_balls,
            weighted_overs=summary.weighted_overs,
            bowling_days=list(summary.bowling_days),
            consecutive_day_pairs=summary.consecutive_day_pairs,
            band_max_age=summary.band_max_age,
            ceiling_overs=summary.ceiling_overs,
            violations=list(summary.violations),
            remaining_balls=summary.remaining_balls,
        )
        for summary in summaries
    ]


@router.get("/safety-config")
def get_latest_safety_config(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> SafetyConfigOut:
    """The governing config: latest version, or canonical defaults (version 0)
    when the table has not been seeded."""
    row = _latest_config_row(db)
    if row is None:
        return SafetyConfigOut(
            version=UNSEEDED_VERSION,
            config=DEFAULT_SAFETY_CONFIG,
            approved_by="seed",
            reason="canonical defaults (docs/safety_workload.md); table unseeded",
            created_at=None,
        )
    return SafetyConfigOut.model_validate(row)


@router.get("/safety-config/versions")
def list_safety_config_versions(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> list[SafetyConfigOut]:
    """Every stored version, oldest first (append-only history, US-H1)."""
    rows = db.scalars(select(SafetyConfig).order_by(SafetyConfig.version))
    return [SafetyConfigOut.model_validate(row) for row in rows]


@router.post("/safety-config", status_code=status.HTTP_201_CREATED)
def create_safety_config(
    payload: SafetyConfigIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> SafetyConfigOut:
    """Append a new config version (US-H1 approval gate).

    Raising ANY ceiling relative to the previous version requires the coach
    role; a parent may only lower. Every accepted change writes an AuditLog
    row carrying the full old→new configs and the detected raises.
    """
    try:
        validate_safety_config(payload.config)
        old_version, old_config = _active_config(db)
        raises = detect_ceiling_raises(old_config, payload.config)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid safety config: {exc}"
        ) from exc
    if raises and role is not Role.COACH:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"raising a ceiling requires coach approval: {'; '.join(raises)}",
        )
    row = SafetyConfig(
        version=old_version + 1,
        config=payload.config,
        approved_by=role.value,
        reason=payload.reason,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        # Two concurrent appends both compute the same next version; the second
        # loses the unique(version) race. Surface a clear 409 (that unique
        # constraint is also what keeps the raise-compare TOCTOU safe).
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "config version conflict, retry") from exc
    db.add(
        AuditLog(
            actor=role.value,
            action="safety_config_change",
            entity="safety_config",
            entity_id=str(row.version),
            detail={
                "old_version": old_version,
                "new_version": row.version,
                "old_config": old_config,
                "new_config": payload.config,
                "ceiling_raises": list(raises),
                "reason": payload.reason,
            },
        )
    )
    return SafetyConfigOut.model_validate(row)
