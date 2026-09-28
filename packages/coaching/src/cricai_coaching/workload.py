"""Pure workload math over bowling-ledger entries and the batting split (US-H1/H2).

Everything here is a pure function over ``BowlingLedgerEntry``-shaped data plus
a config dict shaped like
:data:`cricai_coaching.safety_config.DEFAULT_SAFETY_CONFIG` — no I/O, no
denormalized weekly columns to drift (plan scope decision).

Pinned decisions (SAF-tested, ``docs/safety_workload.md``):

- A rolling-7 window ending on day ``D`` covers ``[D-6, D]`` inclusive.
- Age changes ON the birthday; each entry is banded by the player's age at
  that entry's date (US-H1 birthday-boundary AC).
- When a birthday falls inside a window, the effective ceiling is the MINIMUM
  ceiling across the band at the window end and the bands of every counted
  entry — conservative: overs bowled under the stricter band keep that band's
  ceiling in force until those entries roll out of the window.
- REACHING the ceiling raises ``WORKLOAD_CEILING`` (the US-H1 Gherkin: a live
  count *reaching* 16 overs warns), i.e. ``weighted_overs >= ceiling``; the
  remaining allowance is then 0 and the planner must not schedule bowling.
- A bowling day is a date whose entries carry positive weighted balls;
  throwdowns (weight 0) are arm throws, not a bowling action — they never
  create bowling days and never count toward the overs ceiling.
- An intensity missing from ``intensity_weights`` counts fully (weight 1.0):
  the safe failure mode is overcounting, never undercounting.
- Age above every configured band ⇒ no ceiling (``ceiling_overs`` and
  ``remaining_balls`` are ``None``); day-pattern rules still apply.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, Protocol

from cricai_data.enums import BlockIntent, SafetyCode

#: ``bowling_ledger_entries.source`` vocabulary (single definition; the API
#: router and the backfill worker both import these).
SOURCE_MANUAL = "manual"
SOURCE_AUTO_BACKFILL = "auto_backfill"

#: Rolling window length in days (US-H1: "in any rolling 7").
ROLLING_WINDOW_DAYS = 7

#: Weight applied when an intensity is missing from ``intensity_weights``:
#: count fully — overcounting is the safe failure mode.
DEFAULT_INTENSITY_WEIGHT = 1.0


class LedgerEntryLike(Protocol):
    """Structural shape of one ledger entry (ORM row or plain test double)."""

    @property
    def entry_date(self) -> date: ...

    @property
    def balls(self) -> int: ...

    @property
    def intensity(self) -> object: ...  # DeliveryIntensity or its str value


@dataclass(frozen=True)
class WindowSummary:
    """One rolling-7 window's workload state (US-H1).

    ``violations`` uses :class:`cricai_data.enums.SafetyCode`;
    ``remaining_balls`` is the weighted-ball budget left before the ceiling
    (floored to a whole ball, 0 when the ceiling is reached, ``None`` when no
    band ceiling applies).
    """

    window_start: date
    window_end: date
    weighted_balls: float
    weighted_overs: float
    bowling_days: tuple[date, ...]
    consecutive_day_pairs: int
    band_max_age: int | None
    ceiling_overs: float | None
    violations: tuple[SafetyCode, ...]
    remaining_balls: int | None


@dataclass(frozen=True)
class IntentReconciliation:
    """Plan-vs-actual for one block intent (US-H2)."""

    intent: str
    planned_balls: int
    actual_balls: int
    deviation_pct: float
    flagged: bool


@dataclass(frozen=True)
class SplitReconciliation:
    """One day's batting-split reconciliation (US-H2)."""

    intents: tuple[IntentReconciliation, ...]
    planned_total: int
    actual_total: int
    flagged_intents: tuple[str, ...]
    fun_block_intact: bool


def rolling_window(end: date) -> tuple[date, date]:
    """Inclusive [start, end] of the rolling-7 window ending on ``end`` (US-H1)."""
    return end - timedelta(days=ROLLING_WINDOW_DAYS - 1), end


def age_on(birthdate: date, on: date) -> int:
    """Age in whole years on ``on``; the band switches ON the birthday (US-H1)."""
    years = on.year - birthdate.year
    if (on.month, on.day) < (birthdate.month, birthdate.day):
        years -= 1
    return years


def band_for_age(config: Mapping[str, Any], age: int) -> Mapping[str, Any] | None:
    """The age band covering ``age`` (bands are inclusive ``max_age`` upper bounds).

    Returns ``None`` when ``age`` exceeds every configured band — no ceiling
    applies (US-H1 defaults only cover juniors).
    """
    bands: list[Mapping[str, Any]] = sorted(
        config["workload"]["age_bands"], key=lambda band: int(band["max_age"])
    )
    for band in bands:
        if age <= int(band["max_age"]):
            return band
    return None


def intensity_weight(config: Mapping[str, Any], intensity: object) -> float:
    """Ceiling weight for one intensity; unknown intensities count fully (US-H1)."""
    weights: Mapping[str, Any] = config["workload"]["intensity_weights"]
    return float(weights.get(str(intensity), DEFAULT_INTENSITY_WEIGHT))


def weighted_balls(entries: Iterable[LedgerEntryLike], config: Mapping[str, Any]) -> float:
    """Total intensity-weighted balls across ``entries`` (US-H1 overs math)."""
    return sum(entry.balls * intensity_weight(config, entry.intensity) for entry in entries)


def _effective_ceiling(
    counted_dates: Iterable[date],
    *,
    birthdate: date,
    end: date,
    config: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The governing band: minimum ceiling over the end-date band and every
    counted entry's band (conservative birthday-boundary rule, US-H1)."""
    candidates = [band_for_age(config, age_on(birthdate, day)) for day in counted_dates]
    candidates.append(band_for_age(config, age_on(birthdate, end)))
    present = [band for band in candidates if band is not None]
    if not present:
        return None
    return min(present, key=lambda band: float(band["weekly_overs_ceiling"]))


def summarize_window(
    entries: Iterable[LedgerEntryLike],
    *,
    birthdate: date,
    end: date,
    config: Mapping[str, Any],
) -> WindowSummary:
    """Evaluate the rolling-7 window ending on ``end`` (US-H1).

    Emits ``WORKLOAD_CEILING`` when weighted overs reach the effective band
    ceiling and ``DAY_PATTERN_VIOLATION`` when bowling days exceed
    ``max_bowling_days_per_rolling_7`` or consecutive-day pairs exceed
    ``max_consecutive_day_pairs_per_rolling_7``.
    """
    start, _ = rolling_window(end)
    workload = config["workload"]
    balls_per_over = int(workload["balls_per_over"])

    total_weighted = 0.0
    counted_dates: set[date] = set()
    for entry in entries:
        if not start <= entry.entry_date <= end:
            continue
        contribution = entry.balls * intensity_weight(config, entry.intensity)
        total_weighted += contribution
        if contribution > 0:
            counted_dates.add(entry.entry_date)

    band = _effective_ceiling(counted_dates, birthdate=birthdate, end=end, config=config)
    ceiling_overs = None if band is None else float(band["weekly_overs_ceiling"])
    band_max_age = None if band is None else int(band["max_age"])

    weighted_overs = total_weighted / balls_per_over
    bowling_days = tuple(sorted(counted_dates))
    consecutive_day_pairs = sum(
        1 for previous, current in pairwise(bowling_days) if (current - previous).days == 1
    )

    violations: list[SafetyCode] = []
    remaining_balls: int | None = None
    if ceiling_overs is not None:
        if weighted_overs >= ceiling_overs:
            violations.append(SafetyCode.WORKLOAD_CEILING)
        remaining_balls = int(max(0.0, ceiling_overs * balls_per_over - total_weighted))
    too_many_days = len(bowling_days) > int(workload["max_bowling_days_per_rolling_7"])
    too_many_pairs = consecutive_day_pairs > int(
        workload["max_consecutive_day_pairs_per_rolling_7"]
    )
    if too_many_days or too_many_pairs:
        violations.append(SafetyCode.DAY_PATTERN_VIOLATION)

    return WindowSummary(
        window_start=start,
        window_end=end,
        weighted_balls=total_weighted,
        weighted_overs=weighted_overs,
        bowling_days=bowling_days,
        consecutive_day_pairs=consecutive_day_pairs,
        band_max_age=band_max_age,
        ceiling_overs=ceiling_overs,
        violations=tuple(violations),
        remaining_balls=remaining_balls,
    )


def summarize_windows(
    entries: Sequence[LedgerEntryLike],
    *,
    birthdate: date,
    end: date,
    days: int,
    config: Mapping[str, Any],
) -> tuple[WindowSummary, ...]:
    """Rolling-7 summaries for the ``days`` windows ending ``end``, ``end-1``, …
    (newest first) — the shape the US-H1 summary endpoint serves."""
    return tuple(
        summarize_window(
            entries, birthdate=birthdate, end=end - timedelta(days=offset), config=config
        )
        for offset in range(days)
    )


def actual_balls_by_intent(blocks: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Aggregate a day's tagged blocks (``{intent, balls, …}``) per intent (US-H2)."""
    totals: dict[str, int] = {}
    for block in blocks:
        intent = str(block["intent"])
        totals[intent] = totals.get(intent, 0) + int(block.get("balls") or 0)
    return totals


def fun_block_intact(blocks: Iterable[Mapping[str, Any]]) -> bool:
    """US-H2 kid-first rule: a fun block is present (balls > 0) and no fun
    block was ever converted into a drill (``drill_id`` must stay null)."""
    fun_blocks = [block for block in blocks if str(block["intent"]) == BlockIntent.FUN]
    if not fun_blocks:
        return False
    if any(block.get("drill_id") is not None for block in fun_blocks):
        return False
    return any(int(block.get("balls") or 0) > 0 for block in fun_blocks)


def _intent_order(intents: Iterable[str]) -> list[str]:
    """Deterministic intent order: canonical ``BlockIntent`` order, then extras."""
    remaining = set(intents)
    ordered = [intent.value for intent in BlockIntent if intent.value in remaining]
    return ordered + sorted(remaining - set(ordered))


def _deviation_pct(planned: int, actual: int) -> float:
    """Per-intent deviation as % of plan; any balls in an unplanned intent are
    definitionally 100% off plan (conservative, US-H2)."""
    if planned > 0:
        return (actual - planned) / planned * 100.0
    return 100.0 if actual > 0 else 0.0


def reconcile_batting_split(
    blocks: Iterable[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    plan: Mapping[str, int] | None = None,
) -> SplitReconciliation:
    """Reconcile a day's tagged blocks against the batting split (US-H2).

    ``plan`` overrides the config default split (per-phase-of-season plans);
    intents whose absolute deviation exceeds ``deviation_alert_pct`` are
    flagged. ``fun_block_intact`` reports the kid-first protection predicate.
    """
    split = config["batting_split"]
    planned_split = split["blocks"] if plan is None else plan
    planned: dict[str, int] = {str(key): int(value) for key, value in planned_split.items()}
    actual = actual_balls_by_intent(blocks)
    alert_pct = float(split["deviation_alert_pct"])

    rows: list[IntentReconciliation] = []
    for intent in _intent_order(set(planned) | set(actual)):
        planned_balls = planned.get(intent, 0)
        actual_balls = actual.get(intent, 0)
        deviation = _deviation_pct(planned_balls, actual_balls)
        rows.append(
            IntentReconciliation(
                intent=intent,
                planned_balls=planned_balls,
                actual_balls=actual_balls,
                deviation_pct=deviation,
                flagged=abs(deviation) > alert_pct,
            )
        )
    return SplitReconciliation(
        intents=tuple(rows),
        planned_total=sum(planned.values()),
        actual_total=sum(actual.values()),
        flagged_intents=tuple(row.intent for row in rows if row.flagged),
        fun_block_intact=fun_block_intact(blocks),
    )


def _band_raises(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    old_bands = {int(band["max_age"]): band for band in old["workload"]["age_bands"]}
    new_bands = {int(band["max_age"]): band for band in new["workload"]["age_bands"]}
    found: list[str] = []
    for max_age, band in sorted(new_bands.items()):
        old_band = old_bands.get(max_age)
        if old_band is None:
            found.append(f"workload.age_bands[max_age={max_age}]: band added")
        elif float(band["weekly_overs_ceiling"]) > float(old_band["weekly_overs_ceiling"]):
            found.append(
                f"workload.age_bands[max_age={max_age}].weekly_overs_ceiling: "
                f"{old_band['weekly_overs_ceiling']} -> {band['weekly_overs_ceiling']}"
            )
    found.extend(
        f"workload.age_bands[max_age={max_age}]: band removed (ceiling lifted)"
        for max_age in sorted(set(old_bands) - set(new_bands))
    )
    return found


def _weight_raises(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    old_weights: Mapping[str, Any] = old["workload"]["intensity_weights"]
    new_weights: Mapping[str, Any] = new["workload"]["intensity_weights"]
    found: list[str] = []
    for key in sorted(set(old_weights) | set(new_weights)):
        old_weight = float(old_weights.get(key, DEFAULT_INTENSITY_WEIGHT))
        new_weight = float(new_weights.get(key, DEFAULT_INTENSITY_WEIGHT))
        if new_weight < old_weight:
            found.append(
                f"workload.intensity_weights.{key}: {old_weight} -> {new_weight}"
                " (lower weight loosens the ceiling)"
            )
    return found


#: Scalar limits where a MOVE IN THE GIVEN DIRECTION loosens safety and
#: therefore requires coach approval (US-H1: "raising any ceiling").
_SCALAR_LIMITS: tuple[tuple[tuple[str, str], str], ...] = (
    (("workload", "balls_per_over"), "up"),
    (("workload", "max_bowling_days_per_rolling_7"), "up"),
    (("workload", "max_consecutive_day_pairs_per_rolling_7"), "up"),
    (("batting_split", "daily_balls"), "up"),
    (("batting_split", "deviation_alert_pct"), "up"),
    (("wellness", "pain_escalation_count"), "up"),
    (("wellness", "pain_escalation_window_days"), "down"),
)


def detect_ceiling_raises(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[str, ...]:
    """Every way ``new`` loosens a safety limit relative to ``old`` (US-H1).

    Any non-empty result requires coach approval; a parent may only lower.
    Loosening moves: raising a band ceiling, adding/removing a band, raising
    ``balls_per_over``/day-pattern limits, lowering an intensity weight,
    raising batting-split volume/alert thresholds, and weakening pain
    escalation (higher count or shorter window).
    """
    found = _band_raises(old, new) + _weight_raises(old, new)
    for (section, key), direction in _SCALAR_LIMITS:
        old_value = float(old[section][key])
        new_value = float(new[section][key])
        loosened = new_value > old_value if direction == "up" else new_value < old_value
        if loosened:
            found.append(f"{section}.{key}: {old[section][key]} -> {new[section][key]}")
    return tuple(found)


def _check_workload_shape(config: Mapping[str, Any], problems: list[str]) -> None:
    workload = config["workload"]
    for key in (
        "age_bands",
        "balls_per_over",
        "max_bowling_days_per_rolling_7",
        "max_consecutive_day_pairs_per_rolling_7",
        "intensity_weights",
    ):
        if key not in workload:
            problems.append(f"workload.{key}: missing")
    bands = workload.get("age_bands")
    if isinstance(bands, list) and bands:
        for index, band in enumerate(bands):
            if not isinstance(band, Mapping):
                problems.append(f"workload.age_bands[{index}]: must be a mapping")
                continue
            for band_key in ("max_age", "weekly_overs_ceiling"):
                if band_key not in band:
                    problems.append(f"workload.age_bands[{index}].{band_key}: missing")
    elif "age_bands" in workload:
        problems.append("workload.age_bands: must be a non-empty list")
    if "balls_per_over" in workload and int(workload["balls_per_over"]) < 1:
        problems.append("workload.balls_per_over: must be >= 1")
    weights = workload.get("intensity_weights")
    if weights is not None and not isinstance(weights, Mapping):
        problems.append("workload.intensity_weights: must be a mapping")


def _check_section_keys(
    config: Mapping[str, Any], section: str, keys: tuple[str, ...], problems: list[str]
) -> None:
    problems.extend(f"{section}.{key}: missing" for key in keys if key not in config[section])


def validate_safety_config(config: Mapping[str, Any]) -> None:
    """Reject configs the workload engine cannot evaluate (US-H1).

    Raises ``ValueError`` naming every missing key/shape problem so the API
    can surface all of them at once. Extra keys are allowed (append-only
    evolution); values beyond structural minimums are policy, not shape.
    """
    problems: list[str] = []
    for section in ("workload", "batting_split", "wellness"):
        if not isinstance(config.get(section), Mapping):
            problems.append(f"{section}: missing or not a mapping")
    if not problems:
        _check_workload_shape(config, problems)
        _check_section_keys(
            config,
            "batting_split",
            ("daily_balls", "blocks", "fun_block_protected", "deviation_alert_pct"),
            problems,
        )
        blocks = config["batting_split"].get("blocks")
        if blocks is not None and not isinstance(blocks, Mapping):
            problems.append("batting_split.blocks: must be a mapping")
        _check_section_keys(
            config,
            "wellness",
            ("pain_escalation_count", "pain_escalation_window_days"),
            problems,
        )
    if problems:
        raise ValueError("; ".join(problems))
