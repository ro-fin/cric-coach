"""US-H1/H2 unit tests: rolling windows, age bands, overs math, split reconciliation.

The release-gating boundary matrix lives in ``test_workload_saf.py``; this file
proves the pure machinery (every line and branch of ``cricai_coaching.workload``).
"""

import copy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_coaching.workload import (
    SOURCE_AUTO_BACKFILL,
    SOURCE_MANUAL,
    actual_balls_by_intent,
    age_on,
    band_for_age,
    detect_ceiling_raises,
    fun_block_intact,
    intensity_weight,
    reconcile_batting_split,
    rolling_window,
    summarize_window,
    summarize_windows,
    validate_safety_config,
    weighted_balls,
)
from cricai_data.enums import DeliveryIntensity, SafetyCode

CONFIG = DEFAULT_SAFETY_CONFIG

#: Turns 12 on 2026-07-15; age 11 through 2026-07-14.
BIRTHDATE = date(2014, 7, 15)
END = date(2026, 7, 10)  # age 11 all window


@dataclass(frozen=True)
class Entry:
    """Minimal LedgerEntryLike double (BowlingLedgerEntry-shaped)."""

    entry_date: date
    balls: int
    intensity: DeliveryIntensity | str = DeliveryIntensity.PACE_INTENT


def _copy(config: dict[str, Any]) -> dict[str, Any]:
    """Deep copy for targeted mutation in tests."""
    return copy.deepcopy(config)


def test_source_vocabulary_is_pinned() -> None:
    assert SOURCE_MANUAL == "manual"
    assert SOURCE_AUTO_BACKFILL == "auto_backfill"


def test_rolling_window_is_seven_days_inclusive() -> None:
    start, end = rolling_window(date(2026, 7, 10))
    assert (start, end) == (date(2026, 7, 4), date(2026, 7, 10))


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (date(2026, 7, 14), 11),  # day before 12th birthday
        (date(2026, 7, 15), 12),  # ON the birthday: band switches
        (date(2026, 7, 16), 12),
        (date(2026, 6, 30), 11),  # earlier month
        (date(2026, 8, 1), 12),  # later month
    ],
)
def test_age_on_switches_on_the_birthday(on: date, expected: int) -> None:
    assert age_on(BIRTHDATE, on) == expected


@pytest.mark.parametrize(
    ("age", "expected_max_age"),
    [(10, 11), (11, 11), (12, 13), (13, 13)],
)
def test_band_for_age_inclusive_upper_bounds(age: int, expected_max_age: int) -> None:
    band = band_for_age(CONFIG, age)
    assert band is not None
    assert band["max_age"] == expected_max_age


def test_band_for_age_above_all_bands_is_none() -> None:
    assert band_for_age(CONFIG, 14) is None


def test_band_for_age_sorts_unsorted_bands() -> None:
    config = _copy(CONFIG)
    config["workload"]["age_bands"] = list(reversed(config["workload"]["age_bands"]))
    band = band_for_age(config, 10)
    assert band is not None
    assert band["max_age"] == 11


@pytest.mark.parametrize(
    ("intensity", "expected"),
    [
        (DeliveryIntensity.SPIN, 1.0),
        (DeliveryIntensity.PACE_INTENT, 1.0),
        (DeliveryIntensity.THROWDOWN, 0.0),
        ("spin", 1.0),  # plain string value
        ("unknown_style", 1.0),  # missing weight counts fully (safe overcount)
    ],
)
def test_intensity_weight(intensity: DeliveryIntensity | str, expected: float) -> None:
    assert intensity_weight(CONFIG, intensity) == expected


def test_weighted_balls_mixes_intensities() -> None:
    entries = [
        Entry(END, 30, DeliveryIntensity.PACE_INTENT),
        Entry(END, 12, DeliveryIntensity.SPIN),
        Entry(END, 50, DeliveryIntensity.THROWDOWN),
    ]
    assert weighted_balls(entries, CONFIG) == 42.0
    assert weighted_balls([], CONFIG) == 0


def test_summarize_window_filters_to_the_window() -> None:
    start, end = rolling_window(END)
    entries = [
        Entry(start - timedelta(days=1), 600),  # before window
        Entry(end + timedelta(days=1), 600),  # after window
        Entry(start, 6),
        Entry(end, 6),
    ]
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=END, config=CONFIG)
    assert summary.weighted_balls == 12.0
    assert summary.weighted_overs == 2.0
    assert summary.bowling_days == (start, end)
    assert summary.violations == ()


def test_summarize_window_under_ceiling_reports_remaining() -> None:
    summary = summarize_window([Entry(END, 90)], birthdate=BIRTHDATE, end=END, config=CONFIG)
    assert summary.ceiling_overs == 16.0
    assert summary.band_max_age == 11
    assert summary.weighted_overs == 15.0
    assert summary.violations == ()
    assert summary.remaining_balls == 6


def test_summarize_window_at_ceiling_flags_and_zeroes_allowance() -> None:
    summary = summarize_window([Entry(END, 96)], birthdate=BIRTHDATE, end=END, config=CONFIG)
    assert summary.violations == (SafetyCode.WORKLOAD_CEILING,)
    assert summary.remaining_balls == 0


def test_summarize_window_no_entries_full_allowance() -> None:
    summary = summarize_window([], birthdate=BIRTHDATE, end=END, config=CONFIG)
    assert summary.weighted_balls == 0.0
    assert summary.bowling_days == ()
    assert summary.consecutive_day_pairs == 0
    assert summary.ceiling_overs == 16.0
    assert summary.remaining_balls == 96


def test_summarize_window_unbanded_age_has_no_ceiling_but_day_rules_apply() -> None:
    """A 14-year-old is above every default band: no ceiling, day pattern still guards."""
    old = date(2010, 1, 1)  # age 16 at END
    entries = [Entry(END - timedelta(days=offset), 6) for offset in range(5)]  # 5 bowling days
    summary = summarize_window(entries, birthdate=old, end=END, config=CONFIG)
    assert summary.ceiling_overs is None
    assert summary.band_max_age is None
    assert summary.remaining_balls is None
    assert SafetyCode.WORKLOAD_CEILING not in summary.violations
    assert SafetyCode.DAY_PATTERN_VIOLATION in summary.violations


def test_summarize_window_entry_band_governs_when_end_age_is_unbanded() -> None:
    """Entries bowled at a banded age keep their ceiling while in-window even
    if the player ages out of every band at the window end."""
    birthdate = date(2012, 7, 8)  # turns 14 on 2026-07-08, inside the window
    entries = [Entry(date(2026, 7, 6), 120)]  # bowled at 13: ceiling 20 overs
    summary = summarize_window(entries, birthdate=birthdate, end=END, config=CONFIG)
    assert summary.ceiling_overs == 20.0
    assert summary.violations == (SafetyCode.WORKLOAD_CEILING,)


def test_summarize_window_fractional_remaining_floors_to_whole_balls() -> None:
    config = _copy(CONFIG)
    config["workload"]["intensity_weights"]["spin"] = 0.5
    entries = [Entry(END, 1, DeliveryIntensity.SPIN)]  # 0.5 weighted balls used
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=END, config=config)
    assert summary.weighted_balls == 0.5
    assert summary.remaining_balls == 95  # floor(95.5)


def test_summarize_windows_newest_first() -> None:
    entries = [Entry(END, 6)]
    summaries = summarize_windows(entries, birthdate=BIRTHDATE, end=END, days=3, config=CONFIG)
    assert [summary.window_end for summary in summaries] == [
        END,
        END - timedelta(days=1),
        END - timedelta(days=2),
    ]
    assert summaries[0].weighted_balls == 6.0


def test_actual_balls_by_intent_aggregates_and_defaults() -> None:
    blocks: list[dict[str, Any]] = [
        {"intent": "technical", "balls": 100},
        {"intent": "technical", "balls": 40},
        {"intent": "fun"},  # missing balls -> 0
        {"intent": "decision", "balls": None},  # null balls -> 0
    ]
    assert actual_balls_by_intent(blocks) == {"technical": 140, "fun": 0, "decision": 0}


def test_fun_block_intact_requires_presence_balls_and_no_drill() -> None:
    assert fun_block_intact([]) is False
    assert fun_block_intact([{"intent": "technical", "balls": 100}]) is False
    assert fun_block_intact([{"intent": "fun", "balls": 50, "drill_id": "d1"}]) is False
    assert fun_block_intact([{"intent": "fun", "balls": 0}]) is False
    assert fun_block_intact([{"intent": "fun", "balls": None}]) is False
    assert fun_block_intact([{"intent": "fun", "balls": 50}]) is True
    assert fun_block_intact([{"intent": "fun", "balls": 50, "drill_id": None}]) is True
    # one converted fun block poisons the day even if another is intact
    assert (
        fun_block_intact(
            [
                {"intent": "fun", "balls": 25},
                {"intent": "fun", "balls": 25, "drill_id": "d2"},
            ]
        )
        is False
    )


def _default_day_blocks() -> list[dict[str, Any]]:
    return [
        {"intent": "technical", "balls": 150},
        {"intent": "decision", "balls": 150},
        {"intent": "match_scenario", "balls": 100},
        {"intent": "spin_specific", "balls": 50},
        {"intent": "fun", "balls": 50},
    ]


def test_reconcile_batting_split_on_plan_day_has_no_flags() -> None:
    result = reconcile_batting_split(_default_day_blocks(), CONFIG)
    assert result.planned_total == 500
    assert result.actual_total == 500
    assert result.flagged_intents == ()
    assert result.fun_block_intact is True
    assert [row.intent for row in result.intents] == [
        "technical",
        "decision",
        "match_scenario",
        "spin_specific",
        "fun",
    ]
    assert all(row.deviation_pct == 0.0 for row in result.intents)


def test_reconcile_batting_split_flags_over_25_pct_deviation() -> None:
    """US-H2 AC: 400 'prove-yourself' full-intensity balls get flagged."""
    blocks = [{"intent": "technical", "balls": 400}, {"intent": "fun", "balls": 50}]
    result = reconcile_batting_split(blocks, CONFIG)
    by_intent = {row.intent: row for row in result.intents}
    technical = by_intent["technical"]
    assert technical.deviation_pct == pytest.approx(166.6667, abs=1e-3)
    assert technical.flagged is True
    decision = by_intent["decision"]
    assert decision.actual_balls == 0
    assert decision.deviation_pct == -100.0
    assert decision.flagged is True
    fun = by_intent["fun"]
    assert fun.flagged is False
    expected_flags = {"technical", "decision", "match_scenario", "spin_specific"}
    assert set(result.flagged_intents) == expected_flags


def test_reconcile_batting_split_small_deviation_unflagged() -> None:
    blocks = _default_day_blocks()
    blocks[0]["balls"] = 120  # technical -20%: within the 25% alert threshold
    result = reconcile_batting_split(blocks, CONFIG)
    technical = next(row for row in result.intents if row.intent == "technical")
    assert technical.deviation_pct == -20.0
    assert technical.flagged is False


def test_reconcile_batting_split_unplanned_intent_is_flagged_and_ordered_last() -> None:
    blocks = [*_default_day_blocks(), {"intent": "warmup", "balls": 10}]
    result = reconcile_batting_split(blocks, CONFIG)
    warmup = result.intents[-1]
    assert warmup.intent == "warmup"
    assert warmup.planned_balls == 0
    assert warmup.deviation_pct == 100.0
    assert warmup.flagged is True


def test_reconcile_batting_split_custom_plan_overrides_config() -> None:
    """Per-phase-of-season plans (US-H2 AC): plan param replaces the default split."""
    plan = {"technical": 100, "fun": 50, "rest": 0}
    blocks = [{"intent": "technical", "balls": 100}, {"intent": "fun", "balls": 50}]
    result = reconcile_batting_split(blocks, CONFIG, plan=plan)
    assert result.planned_total == 150
    assert result.flagged_intents == ()
    rest = next(row for row in result.intents if row.intent == "rest")
    assert rest.deviation_pct == 0.0  # planned 0, actual 0
    assert rest.flagged is False


def test_detect_ceiling_raises_identical_configs_is_empty() -> None:
    assert detect_ceiling_raises(CONFIG, CONFIG) == ()


def test_detect_ceiling_raises_lowering_is_allowed() -> None:
    lowered = _copy(CONFIG)
    lowered["workload"]["age_bands"][0]["weekly_overs_ceiling"] = 14
    lowered["workload"]["max_bowling_days_per_rolling_7"] = 3
    lowered["batting_split"]["daily_balls"] = 400
    lowered["wellness"]["pain_escalation_count"] = 1
    assert detect_ceiling_raises(CONFIG, lowered) == ()


def test_detect_ceiling_raises_band_ceiling_raise() -> None:
    raised = _copy(CONFIG)
    raised["workload"]["age_bands"][0]["weekly_overs_ceiling"] = 18
    found = detect_ceiling_raises(CONFIG, raised)
    assert len(found) == 1
    assert "age_bands[max_age=11].weekly_overs_ceiling: 16 -> 18" in found[0]


def test_detect_ceiling_raises_band_added_and_removed() -> None:
    changed = _copy(CONFIG)
    changed["workload"]["age_bands"] = [
        {"max_age": 11, "weekly_overs_target": [12, 16], "weekly_overs_ceiling": 16},
        {"max_age": 15, "weekly_overs_target": [20, 24], "weekly_overs_ceiling": 24},
    ]
    found = detect_ceiling_raises(CONFIG, changed)
    assert any("max_age=15]: band added" in item for item in found)
    assert any("max_age=13]: band removed" in item for item in found)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("workload", "balls_per_over", 8),
        ("workload", "max_bowling_days_per_rolling_7", 5),
        ("workload", "max_consecutive_day_pairs_per_rolling_7", 2),
        ("batting_split", "daily_balls", 600),
        ("batting_split", "deviation_alert_pct", 50),
        ("wellness", "pain_escalation_count", 3),
    ],
)
def test_detect_ceiling_raises_scalar_up_is_a_raise(section: str, key: str, value: int) -> None:
    raised = _copy(CONFIG)
    raised[section][key] = value
    found = detect_ceiling_raises(CONFIG, raised)
    assert len(found) == 1
    assert found[0].startswith(f"{section}.{key}:")


def test_detect_ceiling_raises_shorter_pain_window_is_a_raise() -> None:
    """≥2-in-14-days escalation (US-H4): shrinking the window weakens it."""
    raised = _copy(CONFIG)
    raised["wellness"]["pain_escalation_window_days"] = 7
    found = detect_ceiling_raises(CONFIG, raised)
    assert found == ("wellness.pain_escalation_window_days: 14 -> 7",)
    lengthened = _copy(CONFIG)
    lengthened["wellness"]["pain_escalation_window_days"] = 21
    assert detect_ceiling_raises(CONFIG, lengthened) == ()


def test_detect_ceiling_raises_lowered_intensity_weight() -> None:
    loosened = _copy(CONFIG)
    loosened["workload"]["intensity_weights"]["pace_intent"] = 0.5
    found = detect_ceiling_raises(CONFIG, loosened)
    assert len(found) == 1
    assert "intensity_weights.pace_intent: 1.0 -> 0.5" in found[0]


def test_detect_ceiling_raises_weight_key_changes_use_default() -> None:
    # removing throwdown (0.0) means it now defaults to 1.0: stricter, no raise
    stricter = _copy(CONFIG)
    del stricter["workload"]["intensity_weights"]["throwdown"]
    assert detect_ceiling_raises(CONFIG, stricter) == ()
    # adding a new key below the 1.0 default loosens
    loosened = _copy(CONFIG)
    loosened["workload"]["intensity_weights"]["warmup"] = 0.25
    found = detect_ceiling_raises(CONFIG, loosened)
    assert len(found) == 1
    assert "intensity_weights.warmup" in found[0]


def test_validate_safety_config_accepts_defaults() -> None:
    validate_safety_config(CONFIG)  # must not raise


def test_validate_safety_config_missing_section() -> None:
    config = _copy(CONFIG)
    del config["wellness"]
    config["workload"] = 5  # not a mapping
    with pytest.raises(ValueError, match="wellness: missing") as excinfo:
        validate_safety_config(config)
    assert "workload: missing or not a mapping" in str(excinfo.value)


def test_validate_safety_config_missing_workload_keys() -> None:
    config = _copy(CONFIG)
    del config["workload"]["age_bands"]
    del config["workload"]["balls_per_over"]
    with pytest.raises(ValueError, match=r"workload\.age_bands: missing") as excinfo:
        validate_safety_config(config)
    assert "workload.balls_per_over: missing" in str(excinfo.value)


def test_validate_safety_config_band_shape_problems() -> None:
    config = _copy(CONFIG)
    config["workload"]["age_bands"] = [{"max_age": 11}, "not-a-band"]
    with pytest.raises(ValueError) as excinfo:
        validate_safety_config(config)
    message = str(excinfo.value)
    assert "age_bands[0].weekly_overs_ceiling: missing" in message
    assert "age_bands[1]: must be a mapping" in message


def test_validate_safety_config_empty_bands_rejected() -> None:
    config = _copy(CONFIG)
    config["workload"]["age_bands"] = []
    with pytest.raises(ValueError, match="non-empty list"):
        validate_safety_config(config)


def test_validate_safety_config_bad_scalars() -> None:
    config = _copy(CONFIG)
    config["workload"]["balls_per_over"] = 0
    config["workload"]["intensity_weights"] = "heavy"
    with pytest.raises(ValueError, match="balls_per_over: must be >= 1") as excinfo:
        validate_safety_config(config)
    assert "intensity_weights: must be a mapping" in str(excinfo.value)


def test_validate_safety_config_missing_split_and_wellness_keys() -> None:
    config = _copy(CONFIG)
    del config["batting_split"]["deviation_alert_pct"]
    config["batting_split"]["blocks"] = ["technical"]
    del config["wellness"]["pain_escalation_count"]
    with pytest.raises(ValueError) as excinfo:
        validate_safety_config(config)
    message = str(excinfo.value)
    assert "batting_split.deviation_alert_pct: missing" in message
    assert "batting_split.blocks: must be a mapping" in message
    assert "wellness.pain_escalation_count: missing" in message
