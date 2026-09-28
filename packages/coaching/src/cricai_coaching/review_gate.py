"""US-J5: coach review gate — pure decision functions over ``app_settings``.

The approval-gate mode (US-J5 AC: "delays player-visible reports until coach
action, or configurable timeout with default-publish") is a versioned setting:
``app_settings.settings['report_review']`` carries ``mode`` (a
:class:`~cricai_data.enums.ReviewMode`) and ``timeout_hours``. This module is
the single place that turns those settings into decisions:

- :func:`should_hold` — the ``reports.review_due_at`` a report drafted *now*
  must carry (``None`` under ``auto_publish``);
- :func:`timeout_expired` — the sweep predicate (has a held report's deadline
  passed with no coach action?);
- :func:`review_due_at_for` — the DB-reading composition, so wiring the gate
  into ``cricai_worker.generate_report`` is a two-line call:

      from cricai_coaching.review_gate import review_due_at_for
      report.review_due_at = review_due_at_for(db)

Everything but :func:`active_app_settings`/:func:`review_due_at_for` is pure;
all timestamps are timezone-aware UTC (naive input is a hard error — a naive
"now" silently compared against an aware deadline was the failure mode).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from cricai_data.enums import ReviewMode
from cricai_data.models import AppSetting, utcnow
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_coaching.app_settings import DEFAULT_APP_SETTINGS

__all__ = [
    "active_app_settings",
    "review_due_at_for",
    "review_settings",
    "should_hold",
    "timeout_expired",
]


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware UTC, got naive {value!r}")


def active_app_settings(db: Session) -> dict[str, Any]:
    """The governing settings: latest ``app_settings`` version, defaults when unseeded.

    Production is seeded at v1 by the Phase-6 migration; un-migrated test DBs
    fall back to ``cricai_coaching.app_settings.DEFAULT_APP_SETTINGS`` (the
    drift-tested code mirror of that seed). Always returns a deep copy so a
    caller can never mutate the canonical constant or the ORM row through it.
    """
    row = db.scalar(select(AppSetting).order_by(AppSetting.version.desc()).limit(1))
    if row is None:
        return copy.deepcopy(DEFAULT_APP_SETTINGS)
    return copy.deepcopy(dict(row.settings))


def review_settings(settings: Mapping[str, Any]) -> tuple[ReviewMode, float]:
    """Parse and validate the ``report_review`` section: ``(mode, timeout_hours)``.

    Settings are validated at write time by the settings API, but the gate
    re-validates on read (US-J5): a corrupt stored value must fail loudly here,
    never be silently coerced into publishing or holding a child's report.
    """
    section = settings.get("report_review")
    if not isinstance(section, Mapping):
        raise ValueError("app settings have no 'report_review' section")
    mode_raw = section.get("mode")
    try:
        mode = ReviewMode(str(mode_raw))
    except ValueError as exc:
        raise ValueError(f"unknown report_review.mode: {mode_raw!r}") from exc
    timeout = section.get("timeout_hours")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ValueError(f"report_review.timeout_hours must be a positive number, got {timeout!r}")
    return mode, float(timeout)


def should_hold(settings: Mapping[str, Any], *, now: datetime | None = None) -> datetime | None:
    """The ``review_due_at`` a report drafted now must carry, or ``None``.

    ``auto_publish`` -> ``None`` (no hold; the report publishes through the
    normal gate immediately). ``coach_gate`` -> ``now + timeout_hours``: the
    report stays DRAFT until a coach acts or the sweep auto-publishes at the
    deadline (US-J5 AC "configurable timeout with default-publish + notice").
    """
    mode, timeout_hours = review_settings(settings)
    if mode is ReviewMode.AUTO_PUBLISH:
        return None
    at = now if now is not None else utcnow()
    _require_aware(at, "now")
    return at + timedelta(hours=timeout_hours)


def timeout_expired(review_due_at: datetime | None, now: datetime) -> bool:
    """True when a held report's review deadline has passed (sweep predicate).

    ``None`` (never held) is never expired. Both timestamps must be
    timezone-aware — dialect-specific naive round-trips (SQLite) are the
    caller's to coerce, explicitly, before deciding a child's report ships.
    """
    _require_aware(now, "now")
    if review_due_at is None:
        return False
    _require_aware(review_due_at, "review_due_at")
    return now >= review_due_at


def review_due_at_for(db: Session, *, now: datetime | None = None) -> datetime | None:
    """:func:`should_hold` applied to the governing DB settings (integration seam)."""
    return should_hold(active_app_settings(db), now=now)
