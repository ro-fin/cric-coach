"""US-J4 progress agent: snapshots, trend guardrails, frozen history."""

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from cricai_coaching.fatigue import HIGHER_IS_BETTER, LOWER_IS_BETTER
from cricai_coaching.progress_agent import (
    FLAT,
    IMPROVING,
    MAX_HISTORY_POINTS,
    METRIC_DIRECTIONS,
    REGRESSING,
    UNTRENDED,
    ProgressConfig,
    ProgressError,
    baseline_to_mapping,
    build_snapshots,
    direction_of,
    metric_direction,
    personal_best,
    regression_alert,
)
from cricai_data.models import MetricBaseline

START = date(2026, 6, 1)


def row(
    day: int,
    value: float,
    *,
    n: int = 40,
    metric: str = "control_pct",
    zone_key: str = "all",
    window: str = "session",
    session: str | None = None,
) -> dict[str, Any]:
    return {
        "metric": metric,
        "zone_key": zone_key,
        "window": window,
        "snapshot_date": START + timedelta(days=day),
        "value": value,
        "n": n,
        "payload": {"session_id": session or f"s-{day}"},
    }


def trajectory(values: list[float], *, n: int = 40) -> list[dict[str, Any]]:
    return [row(day, value, n=n) for day, value in enumerate(values)]


class TestSnapshots:
    def test_improving_trajectory_qualifies_and_trends_up(self) -> None:
        snapshots = build_snapshots(trajectory([0.5, 0.6, 0.7, 0.8, 0.9]))
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot["metric"] == "control_pct"
        assert snapshot["zone_key"] == "all"
        assert snapshot["window"] == "session"
        assert snapshot["direction"] == IMPROVING
        assert snapshot["qualified"] is True
        assert snapshot["baseline"] == pytest.approx(0.7)
        assert [point["value"] for point in snapshot["points"]] == [0.5, 0.6, 0.7, 0.8, 0.9]
        assert snapshot["points"][0] == {
            "session_id": "s-0",
            "date": "2026-06-01",
            "value": 0.5,
            "n": 40,
        }

    def test_noisy_flat_trajectory_is_flat(self) -> None:
        snapshot = build_snapshots(trajectory([0.70, 0.72, 0.69, 0.71, 0.70]))[0]
        assert snapshot["direction"] == FLAT

    def test_regressing_trajectory_alerts_when_qualified(self) -> None:
        snapshot = build_snapshots(trajectory([0.9, 0.8, 0.7, 0.6, 0.5]))[0]
        assert snapshot["direction"] == REGRESSING
        assert regression_alert(snapshot) is True

    def test_unqualified_regression_never_alerts(self) -> None:
        snapshot = build_snapshots(trajectory([0.9, 0.8, 0.7], n=10))[0]
        assert snapshot["direction"] == REGRESSING
        assert snapshot["qualified"] is False
        assert regression_alert(snapshot) is False

    def test_lower_is_better_registry_metric_improving_downward(self) -> None:
        """US-G5 finding [7/74]: falling less each week is IMPROVEMENT."""
        rows = [
            row(day, value, metric="falling_away_deg")
            for day, value in enumerate([15.0, 10.0, 5.0])
        ]
        snapshot = build_snapshots(rows)[0]
        assert snapshot["direction"] == IMPROVING
        assert regression_alert(snapshot) is False

    def test_lower_is_better_registry_metric_worsening_upward(self) -> None:
        """US-G5 finding [7/74]: falling away MORE each week regresses and alerts."""
        rows = [
            row(day, value, metric="falling_away_deg")
            for day, value in enumerate([5.0, 10.0, 15.0])
        ]
        snapshot = build_snapshots(rows)[0]
        assert snapshot["direction"] == REGRESSING
        assert regression_alert(snapshot) is True

    def test_directions_override_wins_over_the_registry(self) -> None:
        rows = trajectory([0.9, 0.8, 0.7, 0.6, 0.5])
        snapshot = build_snapshots(rows, directions={"control_pct": LOWER_IS_BETTER})[0]
        assert snapshot["direction"] == IMPROVING

    def test_groups_split_and_sort_by_metric_zone_window(self) -> None:
        rows = trajectory([0.5, 0.6, 0.7])
        rows += [row(day, 0.4, metric="head_stability_score") for day in range(3)]
        rows += [row(day, 0.6, zone_key="outside_off/full") for day in range(3)]
        snapshots = build_snapshots(rows)
        assert [(s["metric"], s["zone_key"]) for s in snapshots] == [
            ("control_pct", "all"),
            ("control_pct", "outside_off/full"),
            ("head_stability_score", "all"),
        ]

    def test_rows_arrive_unordered_points_come_out_dated(self) -> None:
        rows = trajectory([0.5, 0.6, 0.7, 0.8, 0.9])
        snapshot = build_snapshots(list(reversed(rows)))[0]
        assert [point["date"] for point in snapshot["points"]] == [
            "2026-06-01",
            "2026-06-02",
            "2026-06-03",
            "2026-06-04",
            "2026-06-05",
        ]

    def test_missing_payload_yields_null_session_id(self) -> None:
        bare = row(0, 0.5)
        bare["payload"] = None
        snapshot = build_snapshots([bare])[0]
        assert snapshot["points"][0]["session_id"] is None

    def test_point_reads_the_scalar_session_id_the_nightly_job_writes(self) -> None:
        """Contract #7: the nightly writer stamps both keys; the scalar wins."""
        both = row(0, 0.5)
        both["payload"] = {"session_id": "s-new", "session_ids": ["s-new"]}
        snapshot = build_snapshots([both])[0]
        assert snapshot["points"][0]["session_id"] == "s-new"

    def test_point_falls_back_to_a_lone_plural_session_ids(self) -> None:
        """Rows written before the scalar key existed still link their session."""
        legacy = row(0, 0.5)
        legacy["payload"] = {"session_ids": ["s-legacy"]}
        snapshot = build_snapshots([legacy])[0]
        assert snapshot["points"][0]["session_id"] == "s-legacy"

    def test_point_with_multiple_contributing_sessions_stays_null(self) -> None:
        """Two sessions merged into one date: no single session to attribute."""
        merged = row(0, 0.5)
        merged["payload"] = {"session_id": None, "session_ids": ["s-a", "s-b"]}
        snapshot = build_snapshots([merged])[0]
        assert snapshot["points"][0]["session_id"] is None

    def test_bad_snapshot_date_raises(self) -> None:
        bad = row(0, 0.5) | {"snapshot_date": "2026-06-01"}
        with pytest.raises(ProgressError, match="snapshot_date"):
            build_snapshots([bad])


class TestGuardrails:
    def test_fewer_than_three_sessions_is_unqualified(self) -> None:
        snapshot = build_snapshots(trajectory([0.5, 0.9]))[0]
        assert snapshot["qualified"] is False

    def test_any_point_below_thirty_balls_is_unqualified(self) -> None:
        rows = trajectory([0.5, 0.6, 0.7, 0.8])
        rows[2]["n"] = 29
        snapshot = build_snapshots(rows)[0]
        assert snapshot["qualified"] is False

    def test_exactly_at_the_guardrails_qualifies(self) -> None:
        snapshot = build_snapshots(trajectory([0.5, 0.6, 0.7], n=30))[0]
        assert snapshot["qualified"] is True

    def test_config_overrides_the_guardrails(self) -> None:
        config = ProgressConfig(min_sessions=2, min_balls_per_point=5)
        snapshot = build_snapshots(trajectory([0.5, 0.9], n=5), config=config)[0]
        assert snapshot["qualified"] is True


class TestFrozenHistory:
    def test_as_of_freezes_the_view_against_later_rows(self) -> None:
        rows = trajectory([0.5, 0.6, 0.7, 0.8])
        frozen_day = START + timedelta(days=2)
        before = build_snapshots(rows[:3], as_of=frozen_day)
        after = build_snapshots(rows, as_of=frozen_day)  # a new nightly row landed
        assert before == after  # snapshot isolation: reports never see drift

    def test_history_is_size_bounded_dropping_oldest(self) -> None:
        rows = trajectory([0.5 + 0.01 * day for day in range(MAX_HISTORY_POINTS + 3)])
        snapshot = build_snapshots(rows)[0]
        assert len(snapshot["points"]) == MAX_HISTORY_POINTS
        assert snapshot["points"][0]["date"] == "2026-06-04"  # oldest three dropped

    def test_custom_cap_applies(self) -> None:
        snapshot = build_snapshots(
            trajectory([0.5, 0.6, 0.7, 0.8]), config=ProgressConfig(max_points=2)
        )[0]
        assert [point["value"] for point in snapshot["points"]] == [0.7, 0.8]


class TestSlopeAndDirection:
    def test_single_point_is_flat(self) -> None:
        snapshot = build_snapshots(trajectory([0.9]))[0]
        assert snapshot["direction"] == FLAT

    def test_same_day_points_have_no_time_axis(self) -> None:
        rows = [row(0, 0.2, session="s-a"), row(0, 0.9, session="s-b")]
        snapshot = build_snapshots(rows)[0]
        assert snapshot["direction"] == FLAT

    def test_direction_thresholds_are_inclusive(self) -> None:
        assert direction_of(0.01, threshold=0.01) == IMPROVING
        assert direction_of(-0.01, threshold=0.01) == REGRESSING
        assert direction_of(0.009, threshold=0.01) == FLAT
        assert direction_of(0.02, threshold=0.01, higher_is_better=False) == REGRESSING


class TestDirectionsRegistry:
    """US-G5 finding [7/74]: per-metric polarity, consulted by trends/bests/alerts."""

    def test_phase6_lower_is_better_metrics_are_registered(self) -> None:
        for metric in (
            "falling_away_deg",
            "head_offset_at_release_cm",
            "head_offset_at_release_px",
            "trigger_to_contact_ms",
        ):
            assert metric_direction(metric) == LOWER_IS_BETTER

    def test_internal_primitives_are_untrended(self) -> None:
        for metric in (
            "release_frame",
            "release_ms",
            "release_frame_offset",
            "release_height_cm",
            "hand_xy",
            "turn_cm",
            "apex_m",
            "dip_flag",
        ):
            assert metric_direction(metric) == UNTRENDED

    def test_unknown_metrics_default_to_higher_is_better(self) -> None:
        """The Phase-5 behaviour stays the default for unregistered metrics."""
        assert metric_direction("control_pct") == HIGHER_IS_BETTER
        assert metric_direction("brand_new_metric") == HIGHER_IS_BETTER

    def test_override_mapping_wins(self) -> None:
        assert metric_direction("control_pct", {"control_pct": LOWER_IS_BETTER}) == LOWER_IS_BETTER

    def test_registry_values_use_the_fatigue_vocabulary(self) -> None:
        assert set(METRIC_DIRECTIONS.values()) <= {HIGHER_IS_BETTER, LOWER_IS_BETTER, UNTRENDED}

    def test_untrended_metrics_build_no_snapshot(self) -> None:
        """release_ms means of frame timestamps are not a trend (finding [7])."""
        rows = [row(day, 100.0 + day, metric="release_ms") for day in range(3)]
        assert build_snapshots(rows) == []

    def test_untrended_override_excludes_a_metric(self) -> None:
        rows = trajectory([0.5, 0.6, 0.7])
        assert build_snapshots(rows, directions={"control_pct": UNTRENDED}) == []


class TestPersonalBest:
    def test_qualified_new_best_is_celebrated(self) -> None:
        snapshot = build_snapshots(trajectory([0.5, 0.6, 0.7, 0.9]))[0]
        assert personal_best(snapshot) is True

    def test_equalling_the_best_is_not_a_new_best(self) -> None:
        snapshot = build_snapshots(trajectory([0.5, 0.9, 0.7, 0.9]))[0]
        assert personal_best(snapshot) is False

    def test_unqualified_snapshot_never_celebrates(self) -> None:
        snapshot = build_snapshots(trajectory([0.5, 0.6, 0.9], n=10))[0]
        assert personal_best(snapshot) is False

    def test_single_point_has_no_history_to_beat(self) -> None:
        config = ProgressConfig(min_sessions=1)
        snapshot = build_snapshots(trajectory([0.9]), config=config)[0]
        assert personal_best(snapshot) is False

    def test_lower_is_better_best(self) -> None:
        snapshot = build_snapshots(trajectory([0.9, 0.8, 0.7, 0.5]))[0]
        assert personal_best(snapshot, higher_is_better=False) is True
        assert personal_best(snapshot, higher_is_better=True) is False

    def test_best_that_scrolled_out_of_the_window_still_counts(self) -> None:
        """US-J4/K4 finding [80]: 'beats all history' means the FULL baseline
        history, not the 12-point render window — an aged-out true best must
        block a merely window-local best from celebrating."""
        values = [0.82] + [0.6 + 0.01 * i for i in range(14)] + [0.78]
        snapshot = build_snapshots(trajectory(values))[0]
        assert len(snapshot["points"]) == MAX_HISTORY_POINTS
        assert all(point["value"] != 0.82 for point in snapshot["points"])  # peak aged out
        assert snapshot["history_high"] == pytest.approx(0.82)
        assert personal_best(snapshot) is False

    def test_true_all_time_best_still_celebrates_after_a_long_history(self) -> None:
        values = [0.82] + [0.6 + 0.01 * i for i in range(14)] + [0.83]
        snapshot = build_snapshots(trajectory(values))[0]
        assert personal_best(snapshot) is True

    def test_snapshot_carries_the_full_history_extremes(self) -> None:
        snapshot = build_snapshots(trajectory([0.5, 0.6, 0.4, 0.7]))[0]
        assert snapshot["history_high"] == pytest.approx(0.6)  # prior points only
        assert snapshot["history_low"] == pytest.approx(0.4)

    def test_single_point_history_extremes_are_null(self) -> None:
        snapshot = build_snapshots(trajectory([0.9]))[0]
        assert snapshot["history_high"] is None
        assert snapshot["history_low"] is None

    def test_registry_lower_is_better_personal_best(self) -> None:
        """US-K4 finding [7/74]: the all-time LOW is the best for falling_away_deg."""
        rows = [
            row(day, value, metric="falling_away_deg")
            for day, value in enumerate([12.0, 8.0, 6.0, 4.0])
        ]
        assert personal_best(build_snapshots(rows)[0]) is True
        worse = [
            row(day, value, metric="falling_away_deg")
            for day, value in enumerate([4.0, 8.0, 6.0, 5.0])
        ]
        assert personal_best(build_snapshots(worse)[0]) is False

    def test_lower_is_better_low_that_aged_out_still_blocks(self) -> None:
        values = [3.0] + [8.0 - 0.01 * i for i in range(14)] + [4.0]
        rows = [row(day, value, metric="falling_away_deg") for day, value in enumerate(values)]
        snapshot = build_snapshots(rows)[0]
        assert snapshot["history_low"] == pytest.approx(3.0)
        assert personal_best(snapshot) is False

    def test_untrended_metric_never_celebrates(self) -> None:
        snapshot = {
            "metric": "release_ms",
            "qualified": True,
            "points": [{"value": 100.0}, {"value": 200.0}],
        }
        assert personal_best(snapshot) is False

    def test_hand_built_snapshot_falls_back_to_the_window(self) -> None:
        """Snapshots without the history keys (older callers) keep working."""
        higher = {
            "metric": "control_pct",
            "qualified": True,
            "points": [{"value": 0.5}, {"value": 0.9}],
        }
        assert personal_best(higher) is True
        lower = {
            "metric": "falling_away_deg",
            "qualified": True,
            "points": [{"value": 5.0}, {"value": 4.0}],
        }
        assert personal_best(lower) is True


class TestBaselineRowAdapter:
    def test_orm_row_converts_to_the_consumed_mapping(self) -> None:
        player_id = uuid.uuid4()
        orm_row = MetricBaseline(
            player_id=player_id,
            metric="control_pct",
            zone_key="all",
            window="session",
            snapshot_date=START,
            value=0.75,
            n=42,
            payload={"session_id": "s-0"},
        )
        assert baseline_to_mapping(orm_row) == {
            "metric": "control_pct",
            "zone_key": "all",
            "window": "session",
            "snapshot_date": START,
            "value": 0.75,
            "n": 42,
            "payload": {"session_id": "s-0"},
        }

    def test_converted_rows_build_snapshots(self) -> None:
        rows = [
            baseline_to_mapping(
                MetricBaseline(
                    player_id=uuid.uuid4(),
                    metric="control_pct",
                    zone_key="all",
                    window="session",
                    snapshot_date=START + timedelta(days=day),
                    value=0.5 + 0.1 * day,
                    n=40,
                    payload={"session_id": f"s-{day}"},
                )
            )
            for day in range(3)
        ]
        snapshot = build_snapshots(rows)[0]
        assert snapshot["qualified"] is True
        assert snapshot["direction"] == IMPROVING
