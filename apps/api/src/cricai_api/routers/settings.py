"""US-J5/US-L5: versioned app settings — review gate mode, live-mode flag, review queue.

``app_settings`` versions are append-only (the ``safety_configs`` pattern):
changing a setting is a new version with ``approved_by`` and a reason, never
an edit, and every accepted change writes an AuditLog row with the full
old->new settings. Role gates on POST: changing ``report_review.mode``
requires the COACH role (the review gate is the coach's instrument, US-J5);
flipping ``live_mode.enabled`` on requires the PARENT role (guardian opt-in,
US-L5). Reads fall back to the drift-tested canonical defaults
(``cricai_coaching.app_settings.DEFAULT_APP_SETTINGS``, version 0) when the
table is unseeded, so v1 cannot smuggle a change past the role gates either.

The coach review queue (US-J5: draft reports held with a ``review_due_at``
deadline) is served here rather than on ``routers/reports.py``, which another
story owns this phase — recorded as a placement deviation for review.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from cricai_coaching.app_settings import DEFAULT_APP_SETTINGS
from cricai_coaching.live_allowlist import LiveSurfaceViolation, assert_storable_allowlist
from cricai_coaching.review_gate import review_settings
from cricai_data.enums import ReportKind, ReportStatus, Role
from cricai_data.models import AppSetting, AuditLog, Report
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/settings", tags=["settings"])

#: ``version`` reported when serving canonical defaults from an unseeded DB.
UNSEEDED_VERSION = 0

#: The exact top-level sections a settings version must carry — no more, no
#: less. Unknown sections are rejected so a typo can never silently disable a
#: gate ("report_reviw" holding nothing was the failure mode).
REQUIRED_SECTIONS = frozenset({"report_review", "live_mode"})


def _as_utc(value: datetime | None) -> datetime | None:
    """Stored timestamps are UTC; some dialects (SQLite) round-trip them naive."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


#: Response timestamps: always timezone-aware UTC, whatever the DB returned.
UtcTimestamp = Annotated[datetime | None, AfterValidator(_as_utc)]


class AppSettingsIn(BaseModel):
    settings: dict[str, Any]
    reason: str = Field(min_length=1)


class AppSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version: int
    settings: dict[str, Any]
    approved_by: str
    reason: str
    created_at: UtcTimestamp


class ReviewQueueItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    session_id: uuid.UUID | None
    kind: ReportKind
    period_start: date
    period_end: date
    review_due_at: UtcTimestamp
    created_at: UtcTimestamp


def _latest_row(db: OrmSession) -> AppSetting | None:
    return db.scalar(select(AppSetting).order_by(AppSetting.version.desc()).limit(1))


def _active_settings(db: OrmSession) -> tuple[int, dict[str, Any]]:
    """(version, settings) that currently govern — canonical defaults when unseeded."""
    row = _latest_row(db)
    if row is None:
        return UNSEEDED_VERSION, DEFAULT_APP_SETTINGS
    return row.version, row.settings


def _validate_settings(settings: dict[str, Any]) -> None:
    """Shape-validate a posted version; raises ValueError with every problem named."""
    if set(settings) != REQUIRED_SECTIONS:
        raise ValueError(
            f"settings sections must be exactly {sorted(REQUIRED_SECTIONS)}, got {sorted(settings)}"
        )
    review_settings(settings)  # mode vocabulary + positive timeout (US-J5)
    live = settings["live_mode"]
    if not isinstance(live, dict):
        raise ValueError("live_mode must be an object")
    if not isinstance(live.get("enabled"), bool):
        raise ValueError(f"live_mode.enabled must be a boolean, got {live.get('enabled')!r}")
    allowlist = live.get("allowlist")
    if not isinstance(allowlist, list):
        raise ValueError(f"live_mode.allowlist must be a list, got {allowlist!r}")
    assert_storable_allowlist(allowlist)  # SAF: never-live keys rejected at write


def _mode_of(settings: dict[str, Any]) -> Any:
    return settings["report_review"]["mode"]


def _live_enabled(settings: dict[str, Any]) -> bool:
    return bool(settings["live_mode"]["enabled"])


def _live_allowlist(settings: dict[str, Any]) -> set[str]:
    return set(settings["live_mode"]["allowlist"])


@router.get("")
def get_active_settings(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> AppSettingsOut:
    """The governing settings: latest version, or canonical defaults (version 0)
    when the table has not been seeded."""
    row = _latest_row(db)
    if row is None:
        return AppSettingsOut(
            version=UNSEEDED_VERSION,
            settings=DEFAULT_APP_SETTINGS,
            approved_by="seed",
            reason="canonical defaults (cricai_coaching.app_settings); table unseeded",
            created_at=None,
        )
    return AppSettingsOut.model_validate(row)


@router.get("/versions")
def list_settings_versions(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> list[AppSettingsOut]:
    """Every stored version, oldest first (append-only history, US-J5)."""
    rows = db.scalars(select(AppSetting).order_by(AppSetting.version))
    return [AppSettingsOut.model_validate(row) for row in rows]


@router.get("/review-queue")
def review_queue(
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.COACH))],
) -> list[ReviewQueueItemOut]:
    """US-J5: every draft report held by the review gate, earliest deadline first.

    A draft with no ``review_due_at`` was never gate-held (auto_publish mode);
    published and blocked reports have already been decided — neither belongs
    in the coach's queue.
    """
    rows = db.scalars(
        select(Report)
        .where(Report.status == ReportStatus.DRAFT, Report.review_due_at.is_not(None))
        .order_by(Report.review_due_at, Report.created_at, Report.id)
    )
    return [ReviewQueueItemOut.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_settings_version(
    payload: AppSettingsIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> AppSettingsOut:
    """Append a new settings version (US-J5/L5 approval gates).

    Changing the review mode requires the coach; enabling live mode — or
    broadening its allowlist (finding 19) — requires the parent. A single POST
    needing both approvals cannot pass as either role — each change ships as
    its own approved version (two decisions, two audit rows). Every accepted
    change writes an AuditLog row.
    """
    try:
        _validate_settings(payload.settings)
    except (LiveSurfaceViolation, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid app settings: {exc}"
        ) from exc
    old_version, old_settings = _active_settings(db)
    mode_changed = _mode_of(payload.settings) != _mode_of(old_settings)
    if mode_changed and role is not Role.COACH:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "changing report_review.mode requires coach approval (US-J5)",
        )
    live_enabling = _live_enabled(payload.settings) and not _live_enabled(old_settings)
    if live_enabling and role is not Role.PARENT:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "enabling live_mode requires parent approval (US-L5)",
        )
    # US-L5 (finding 19): BROADENING the live allowlist — any key added versus
    # the currently active allowlist — is a guardian opt-in like enabling live
    # mode, whether or not live is enabled right now (a coach must not
    # pre-stage a wider surface for the parent's later enable click to approve
    # silently). Narrowing stays open to either guardian (fail-safe direction).
    live_broadening = bool(_live_allowlist(payload.settings) - _live_allowlist(old_settings))
    if live_broadening and role is not Role.PARENT:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "broadening live_mode.allowlist requires parent approval (US-L5)",
        )
    row = AppSetting(
        version=old_version + 1,
        settings=payload.settings,
        approved_by=role.value,
        reason=payload.reason,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        # Two concurrent appends both compute the same next version; the second
        # loses the unique(version) race. Surface a clear 409 (that unique
        # constraint is also what keeps the role-gate compares TOCTOU safe).
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "settings version conflict, retry") from exc
    db.add(
        AuditLog(
            actor=role.value,
            action="app_settings_change",
            entity="app_settings",
            entity_id=str(row.version),
            detail={
                "old_version": old_version,
                "new_version": row.version,
                "old_settings": old_settings,
                "new_settings": payload.settings,
                "review_mode_changed": mode_changed,
                "live_mode_enabled": live_enabling,
                "live_allowlist_broadened": live_broadening,
                "reason": payload.reason,
            },
        )
    )
    return AppSettingsOut.model_validate(row)
