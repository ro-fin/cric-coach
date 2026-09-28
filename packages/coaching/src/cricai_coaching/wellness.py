"""US-H4: wellness check-in validation, pain state machine, escalation, absence.

The 30-second post-session check-in (soreness body map, energy, sleep, "any
pain when bowling?") is captured as :class:`cricai_data.models.WellnessCheckin`
rows. This module is the pure-functional layer over those rows:

- :func:`validate_checkin` — body-map key whitelist, energy 1..5, sleep 0..14.
- :func:`evaluate_wellness` — the pain state machine: ``pain=True`` suppresses
  bowling recommendations until an ADULT (parent|coach) clearance row exists
  for that specific check-in; clearances recorded under any other role are
  ignored (defense in depth — the API already forbids them).
- Escalation (US-H4): ``>= pain_escalation_count`` pain reports inside a
  ``pain_escalation_window_days`` window flags escalated prominence for the
  weekly report; :func:`pain_cluster` scans ANY window for report generators.
- Absence visibility: a missing check-in is an explicit state
  (``no_checkin=True``, :func:`checkin_gaps`), never silently fine.

Thresholds default to ``DEFAULT_SAFETY_CONFIG["wellness"]``; runtime callers
pass the latest ``safety_configs`` version's ``wellness`` section instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, cast

from cricai_data.models import PainClearance, WellnessCheckin

from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG

#: Roles allowed to clear a pain flag (US-H4: "until an adult clears the flag").
ADULT_CLEARANCE_ROLES: frozenset[str] = frozenset({"parent", "coach"})

#: Whitelisted soreness body-map keys — the check-in UI's tappable regions.
SORENESS_BODY_KEYS: frozenset[str] = frozenset(
    {
        "neck",
        "shoulder_left",
        "shoulder_right",
        "back_upper",
        "back_lower",
        "side_left",
        "side_right",
        "elbow_left",
        "elbow_right",
        "wrist_left",
        "wrist_right",
        "hand_left",
        "hand_right",
        "hip_left",
        "hip_right",
        "groin",
        "hamstring_left",
        "hamstring_right",
        "quad_left",
        "quad_right",
        "knee_left",
        "knee_right",
        "calf_left",
        "calf_right",
        "shin_left",
        "shin_right",
        "ankle_left",
        "ankle_right",
        "foot_left",
        "foot_right",
    }
)

#: Soreness level scale: 0 (none) .. 3 (sore enough to mention twice).
SORENESS_LEVEL_MIN = 0
SORENESS_LEVEL_MAX = 3

#: Energy emoji scale bounds (1 = flat, 5 = bouncing).
ENERGY_MIN = 1
ENERGY_MAX = 5

#: Plausible sleep bounds; outside this range is a data-entry error, not sleep.
SLEEP_HOURS_MIN = 0.0
SLEEP_HOURS_MAX = 14.0


def validate_checkin(
    soreness: Mapping[str, object],
    energy: int | None,
    sleep_hours: float | None,
) -> list[str]:
    """Validate check-in fields; return every problem found (empty = valid).

    Soreness keys must come from :data:`SORENESS_BODY_KEYS` and levels must be
    integers (bools rejected) in 0..3; energy is 1..5; sleep_hours is 0..14.
    """
    problems: list[str] = []
    for key in sorted(soreness):
        level = soreness[key]
        if key not in SORENESS_BODY_KEYS:
            problems.append(f"unknown soreness body key: {key!r}")
        if isinstance(level, bool) or not isinstance(level, int):
            problems.append(f"soreness[{key!r}] must be an integer level, got {level!r}")
        elif not SORENESS_LEVEL_MIN <= level <= SORENESS_LEVEL_MAX:
            problems.append(
                f"soreness[{key!r}] must be {SORENESS_LEVEL_MIN}..{SORENESS_LEVEL_MAX}, got {level}"
            )
    if energy is not None and not ENERGY_MIN <= energy <= ENERGY_MAX:
        problems.append(f"energy must be {ENERGY_MIN}..{ENERGY_MAX}, got {energy}")
    if sleep_hours is not None and not SLEEP_HOURS_MIN <= sleep_hours <= SLEEP_HOURS_MAX:
        problems.append(
            f"sleep_hours must be {SLEEP_HOURS_MIN}..{SLEEP_HOURS_MAX}, got {sleep_hours}"
        )
    return problems


def assert_valid_checkin(
    soreness: Mapping[str, object],
    energy: int | None,
    sleep_hours: float | None,
) -> None:
    """Raise ``ValueError`` listing every validation problem (US-H4)."""
    problems = validate_checkin(soreness, energy, sleep_hours)
    if problems:
        raise ValueError("invalid wellness check-in: " + "; ".join(problems))


def adult_cleared_checkin_ids(clearances: Sequence[PainClearance]) -> frozenset[uuid.UUID]:
    """Check-in ids cleared by an ADULT role; other roles never clear (US-H4)."""
    return frozenset(c.checkin_id for c in clearances if c.role in ADULT_CLEARANCE_ROLES)


def pain_cluster(
    pain_dates: Sequence[date],
    config: Mapping[str, Any] | None = None,
) -> bool:
    """True when ANY ``window_days`` window holds >= ``count`` pain reports.

    Report generators (weekly report, US-H4) call this over the pain-report
    dates of their period; clearing a flag never erases the history it counts.
    """
    wellness_config = _wellness_config(config)
    window_days = int(wellness_config["pain_escalation_window_days"])
    count = int(wellness_config["pain_escalation_count"])
    ordered = sorted(pain_dates)
    for i, start in enumerate(ordered):
        in_window = sum(1 for d in ordered[i:] if (d - start).days < window_days)
        if in_window >= count:
            return True
    return False


def checkin_gaps(
    checkins: Sequence[WellnessCheckin],
    start: date,
    end: date,
) -> tuple[date, ...]:
    """Dates in [start, end] with no check-in — absence is visible, never silent."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    covered = {c.checkin_date for c in checkins}
    span = (end - start).days
    return tuple(
        day
        for day in (start + timedelta(days=offset) for offset in range(span + 1))
        if day not in covered
    )


@dataclass(frozen=True)
class WellnessState:
    """The pain state machine's output for one player as of one date (US-H4)."""

    as_of: date
    checked_in: bool  # a check-in exists dated ``as_of``
    no_checkin: bool  # explicit absence state — never silently fine
    last_checkin_date: date | None
    days_since_checkin: int | None
    pain_active: bool  # any pain check-in without an adult clearance
    bowling_suppressed: bool  # == pain_active (US-H4 suppression rule)
    open_pain_checkin_ids: tuple[uuid.UUID, ...]
    escalation: bool  # >= count pain reports in the trailing window ending as_of
    pain_reports_in_window: int


def evaluate_wellness(
    checkins: Sequence[WellnessCheckin],
    clearances: Sequence[PainClearance],
    as_of: date,
    config: Mapping[str, Any] | None = None,
) -> WellnessState:
    """Run the US-H4 pain state machine over a player's rows.

    Check-ins dated after ``as_of`` are ignored so the state is reproducible
    for any historical date. ``escalation`` counts pain reports in the trailing
    ``pain_escalation_window_days`` window ending at ``as_of`` (weekly-report
    generators scan arbitrary windows via :func:`pain_cluster`).
    """
    wellness_config = _wellness_config(config)
    window_days = int(wellness_config["pain_escalation_window_days"])
    escalation_count = int(wellness_config["pain_escalation_count"])

    visible = [c for c in checkins if c.checkin_date <= as_of]
    cleared = adult_cleared_checkin_ids(clearances)
    open_pain = sorted(
        (c for c in visible if c.pain and c.id not in cleared),
        key=lambda c: (c.checkin_date, str(c.id)),
    )
    pain_active = bool(open_pain)

    window_start = as_of - timedelta(days=window_days - 1)
    pain_reports_in_window = sum(
        1 for c in visible if c.pain and window_start <= c.checkin_date <= as_of
    )

    last_checkin_date = max((c.checkin_date for c in visible), default=None)
    checked_in = last_checkin_date == as_of
    days_since = None if last_checkin_date is None else (as_of - last_checkin_date).days
    return WellnessState(
        as_of=as_of,
        checked_in=checked_in,
        no_checkin=not checked_in,
        last_checkin_date=last_checkin_date,
        days_since_checkin=days_since,
        pain_active=pain_active,
        bowling_suppressed=pain_active,
        open_pain_checkin_ids=tuple(c.id for c in open_pain),
        escalation=pain_reports_in_window >= escalation_count,
        pain_reports_in_window=pain_reports_in_window,
    )


def _wellness_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config is None:
        return cast("Mapping[str, Any]", DEFAULT_SAFETY_CONFIG["wellness"])
    return config
