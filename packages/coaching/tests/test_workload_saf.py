"""SAF: US-H1 exhaustive boundary matrix as BDD tests — release-gating, never waivable.

Each test states its Gherkin scenario (Given/When/Then). The flagship
scenarios from the US-H1 acceptance criteria run verbatim; around them sits
the boundary matrix: 15.5 → 16 overs, 4th vs 5th bowling day, consecutive-day
single vs second instance, rolling-window edges at midnight, age flip
mid-week, weighted mixed-intensity edges and the throwdown zero-weight rule.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_coaching.workload import rolling_window, summarize_window
from cricai_data.enums import DeliveryIntensity, SafetyCode

pytestmark = pytest.mark.safety

CONFIG = DEFAULT_SAFETY_CONFIG

#: Turns 12 on 2026-07-15.
BIRTHDATE = date(2014, 7, 15)
#: Window [2026-07-04 .. 2026-07-10]: the player is 11 all week.
END_AGE_11 = date(2026, 7, 10)
#: Window [2026-07-15 .. 2026-07-21]: the player is 12 all week.
END_AGE_12 = date(2026, 7, 21)

BALLS_PER_OVER = 6
CEILING_11_BALLS = 16 * BALLS_PER_OVER  # 96
CEILING_13_BALLS = 20 * BALLS_PER_OVER  # 120


@dataclass(frozen=True)
class Entry:
    entry_date: date
    balls: int
    intensity: DeliveryIntensity = DeliveryIntensity.PACE_INTENT


def _spread(end: date, total_balls: int, *, days: int) -> list[Entry]:
    """Spread ``total_balls`` over ``days`` NON-consecutive-safe dates in the
    window ending ``end`` (every second day, newest first)."""
    per_day, remainder = divmod(total_balls, days)
    return [
        Entry(end - timedelta(days=2 * index), per_day + (remainder if index == 0 else 0))
        for index in range(days)
    ]


def test_flagship_reaching_16_overs_raises_workload_ceiling() -> None:
    """Given the player is 11 years old
    And he has bowled 15 overs in the current rolling 7 days
    When a session plan or live count reaches 16 overs
    Then the system raises a WORKLOAD_CEILING warning
    And the Drill Planner is blocked from scheduling further bowling this window."""
    fifteen_overs = _spread(END_AGE_11, 15 * BALLS_PER_OVER, days=3)
    before = summarize_window(fifteen_overs, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert before.violations == ()
    assert before.remaining_balls == BALLS_PER_OVER  # one over left

    at_sixteen = [*fifteen_overs, Entry(END_AGE_11, BALLS_PER_OVER)]
    after = summarize_window(at_sixteen, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert SafetyCode.WORKLOAD_CEILING in after.violations
    assert after.remaining_balls == 0  # planner hard block: zero allowance


def test_flagship_fifth_bowling_day_flags_day_pattern() -> None:
    """Given he has bowled on 4 distinct days within the rolling 7 days
    When a new bowling block is started (a 5th distinct day)
    Then the system flags DAY_PATTERN_VIOLATION."""
    four_days = [Entry(END_AGE_11 - timedelta(days=offset), 6) for offset in (1, 3, 4, 6)]
    before = summarize_window(four_days, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert SafetyCode.DAY_PATTERN_VIOLATION not in before.violations

    fifth_day = [*four_days, Entry(END_AGE_11, 6)]
    after = summarize_window(fifth_day, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert SafetyCode.DAY_PATTERN_VIOLATION in after.violations


@pytest.mark.parametrize(
    ("balls", "violated", "remaining"),
    [
        (CEILING_11_BALLS - 3, False, 3),  # 15.5 overs: under
        (CEILING_11_BALLS - 1, False, 1),  # one ball under the ceiling
        (CEILING_11_BALLS, True, 0),  # exactly at the ceiling: warned + blocked
        (CEILING_11_BALLS + 1, True, 0),  # one ball over
    ],
)
def test_boundary_exactly_at_ceiling_vs_one_ball_either_side(
    balls: int, violated: bool, remaining: int
) -> None:
    """Given an 11-year-old's rolling week
    When his weighted balls sit just under, exactly at, or just over 16 overs
    Then only reaching the ceiling raises WORKLOAD_CEILING and zeroes allowance."""
    summary = summarize_window(
        _spread(END_AGE_11, balls, days=3), birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG
    )
    assert (SafetyCode.WORKLOAD_CEILING in summary.violations) is violated
    assert summary.remaining_balls == remaining


@pytest.mark.parametrize(("days", "violated"), [(4, False), (5, True)])
def test_boundary_4_vs_5_bowling_days(days: int, violated: bool) -> None:
    """Given bowling spread over N distinct days of the rolling 7
    When N is 4 the pattern is legal; when N is 5 it is flagged."""
    offsets = (0, 2, 4, 6, 3)[:days]  # any 5th day in a rolling 7 also forms pairs
    entries = [Entry(END_AGE_11 - timedelta(days=offset), 6) for offset in offsets]
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert (SafetyCode.DAY_PATTERN_VIOLATION in summary.violations) is violated


def test_boundary_consecutive_day_pair_single_vs_second_instance() -> None:
    """Given one back-to-back bowling pair in the rolling 7 (allowed once)
    When a second consecutive-day instance appears — as a separate pair or by
    extending the first into a 3-day run
    Then DAY_PATTERN_VIOLATION is flagged."""
    one_pair = [Entry(END_AGE_11 - timedelta(days=offset), 6) for offset in (5, 6)]
    summary = summarize_window(one_pair, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert summary.consecutive_day_pairs == 1
    assert summary.violations == ()

    two_pairs = [*one_pair, *(Entry(END_AGE_11 - timedelta(days=o), 6) for o in (0, 1))]
    summary = summarize_window(two_pairs, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert summary.consecutive_day_pairs == 2
    assert SafetyCode.DAY_PATTERN_VIOLATION in summary.violations

    three_day_run = [Entry(END_AGE_11 - timedelta(days=o), 6) for o in (4, 5, 6)]
    summary = summarize_window(three_day_run, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert summary.consecutive_day_pairs == 2  # a run of 3 is a second instance
    assert SafetyCode.DAY_PATTERN_VIOLATION in summary.violations


def test_boundary_rolling_window_edges_at_midnight() -> None:
    """Given entries exactly 7 days before the window end and exactly at its
    oldest included day
    When the rolling window is evaluated
    Then day end-7 is excluded and day end-6 is included (midnight edges)."""
    start, end = rolling_window(END_AGE_11)
    assert start == END_AGE_11 - timedelta(days=6)
    entries = [
        Entry(end - timedelta(days=7), CEILING_11_BALLS),  # aged out at midnight
        Entry(start, 6),
    ]
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=end, config=CONFIG)
    assert summary.weighted_balls == 6.0
    assert summary.bowling_days == (start,)
    assert summary.violations == ()


def test_boundary_age_11_vs_12_band_switch() -> None:
    """Given 17 overs bowled in a rolling week
    When the player is 11 the 16-over ceiling flags it
    But when the same volume falls entirely after his 12th birthday
    Then the 12-13 band's 20-over ceiling applies and it is legal."""
    volume = 17 * BALLS_PER_OVER
    at_11 = summarize_window(
        _spread(END_AGE_11, volume, days=3), birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG
    )
    assert at_11.ceiling_overs == 16.0
    assert SafetyCode.WORKLOAD_CEILING in at_11.violations

    at_12 = summarize_window(
        _spread(END_AGE_12, volume, days=3), birthdate=BIRTHDATE, end=END_AGE_12, config=CONFIG
    )
    assert at_12.ceiling_overs == 20.0
    assert at_12.band_max_age == 13
    assert SafetyCode.WORKLOAD_CEILING not in at_12.violations
    assert at_12.remaining_balls == CEILING_13_BALLS - volume


def test_boundary_birthday_inside_window_keeps_stricter_ceiling() -> None:
    """Given overs bowled at age 11 still inside the rolling window
    When the player turns 12 mid-window (age flip mid-week)
    Then the stricter 16-over ceiling keeps governing until those entries
    roll out — a birthday never retroactively grants headroom."""
    end = date(2026, 7, 16)  # birthday 2026-07-15 is inside [07-10 .. 07-16]
    entries = [
        Entry(date(2026, 7, 12), 90),  # 15 overs bowled at age 11
        Entry(date(2026, 7, 16), 6),  # 1 over bowled at age 12
    ]
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=end, config=CONFIG)
    assert summary.ceiling_overs == 16.0  # min(16, 20): conservative
    assert summary.band_max_age == 11
    assert SafetyCode.WORKLOAD_CEILING in summary.violations
    assert summary.remaining_balls == 0


def test_boundary_after_age_11_entries_roll_out_new_band_governs() -> None:
    """Given the same player one week after his birthday
    When every in-window entry was bowled at age 12
    Then the 20-over ceiling governs (band switched automatically)."""
    entries = [Entry(date(2026, 7, 12), 90), Entry(date(2026, 7, 16), 6)]
    end = date(2026, 7, 22)  # window [07-16 .. 07-22]: the age-11 entry aged out
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=end, config=CONFIG)
    assert summary.ceiling_overs == 20.0
    assert summary.band_max_age == 13
    assert summary.violations == ()


def test_boundary_weighted_mixed_intensity_edge() -> None:
    """Given a week mixing pace-intent, spin and throwdowns
    When the weighted balls (pace 1.0, spin 1.0, throwdown 0.0) reach exactly
    the ceiling only because throwdowns weigh zero
    Then WORKLOAD_CEILING flags at the weighted — not raw — total."""
    mixed = [
        Entry(END_AGE_11 - timedelta(days=4), 90, DeliveryIntensity.PACE_INTENT),
        Entry(END_AGE_11 - timedelta(days=2), 6, DeliveryIntensity.SPIN),
        Entry(END_AGE_11, 60, DeliveryIntensity.THROWDOWN),
    ]
    summary = summarize_window(mixed, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert summary.weighted_balls == 96.0  # 156 raw balls, 96 weighted
    assert SafetyCode.WORKLOAD_CEILING in summary.violations

    one_spin_less = [
        mixed[0],
        Entry(END_AGE_11 - timedelta(days=2), 5, DeliveryIntensity.SPIN),
        mixed[2],
    ]
    summary = summarize_window(one_spin_less, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert summary.weighted_balls == 95.0
    assert summary.violations == ()
    assert summary.remaining_balls == 1


def test_boundary_throwdowns_weigh_zero_and_never_make_bowling_days() -> None:
    """Given a week of heavy throwdown volume and two real bowling days
    When the window is evaluated
    Then throwdowns add no overs, create no bowling days and cannot bridge
    two real bowling days into a consecutive pair."""
    entries = [
        Entry(END_AGE_11 - timedelta(days=2), 120, DeliveryIntensity.THROWDOWN),
        Entry(END_AGE_11 - timedelta(days=3), 6, DeliveryIntensity.PACE_INTENT),
        Entry(END_AGE_11 - timedelta(days=1), 200, DeliveryIntensity.THROWDOWN),
        Entry(END_AGE_11, 6, DeliveryIntensity.PACE_INTENT),
    ]
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert summary.weighted_balls == 12.0
    assert summary.bowling_days == (
        END_AGE_11 - timedelta(days=3),
        END_AGE_11,
    )
    assert summary.consecutive_day_pairs == 0
    assert summary.violations == ()


def test_boundary_throwdown_only_week_is_fully_legal() -> None:
    """Given a week of only throwdown replies
    When the window is evaluated
    Then no overs count, no bowling days exist and no violation fires."""
    entries = [
        Entry(END_AGE_11 - timedelta(days=offset), 100, DeliveryIntensity.THROWDOWN)
        for offset in range(7)
    ]
    summary = summarize_window(entries, birthdate=BIRTHDATE, end=END_AGE_11, config=CONFIG)
    assert summary.weighted_overs == 0.0
    assert summary.bowling_days == ()
    assert summary.violations == ()
    assert summary.remaining_balls == CEILING_11_BALLS
