"""US-C4: landmark-shift statistic and drift threshold boundary tests."""

import math

import pytest
from cricai_vision.drift import (
    DEFAULT_DRIFT_THRESHOLD_PX,
    ShiftStats,
    is_drifted,
    landmark_shift_stats,
)


def _shifted(
    points: dict[str, tuple[float, float]], dx: float, dy: float
) -> dict[str, tuple[float, float]]:
    return {name: (x + dx, y + dy) for name, (x, y) in points.items()}


EXPECTED: dict[str, tuple[float, float]] = {
    "striker_middle_stump_base": (960.0, 900.0),
    "striker_popping_crease_off": (1200.0, 820.0),
    "striker_popping_crease_leg": (720.0, 820.0),
    "bowler_middle_stump_base": (960.0, 300.0),
    "pitch_edge_off_striker": (1400.0, 910.0),
}


def test_zero_shift_yields_zero_stats() -> None:
    stats = landmark_shift_stats(EXPECTED, dict(EXPECTED))
    assert stats.n == 5
    assert stats.median_px == 0.0
    assert stats.max_px == 0.0
    assert set(stats.per_landmark) == set(EXPECTED)
    assert all(shift == 0.0 for shift in stats.per_landmark.values())


def test_uniform_translation_measured_per_landmark() -> None:
    stats = landmark_shift_stats(EXPECTED, _shifted(EXPECTED, 3.0, 4.0))
    assert stats.n == 5
    assert stats.median_px == pytest.approx(5.0)
    assert stats.max_px == pytest.approx(5.0)
    assert stats.per_landmark["bowler_middle_stump_base"] == pytest.approx(5.0)


def test_median_and_max_with_mixed_shifts() -> None:
    observed = dict(EXPECTED)
    observed["striker_middle_stump_base"] = (960.0 + 1.0, 900.0)  # 1 px
    observed["striker_popping_crease_off"] = (1200.0, 820.0 + 2.0)  # 2 px
    observed["striker_popping_crease_leg"] = (720.0 + 7.0, 820.0)  # 7 px
    observed["bowler_middle_stump_base"] = (960.0, 300.0 + 40.0)  # 40 px
    observed["pitch_edge_off_striker"] = (1400.0, 910.0)  # 0 px
    stats = landmark_shift_stats(EXPECTED, observed)
    assert stats.median_px == pytest.approx(2.0)  # odd count: middle value
    assert stats.max_px == pytest.approx(40.0)
    assert stats.n == 5


def test_even_count_median_averages_middle_pair() -> None:
    expected = {"a": (0.0, 0.0), "b": (0.0, 0.0), "c": (0.0, 0.0), "d": (0.0, 0.0)}
    observed = {"a": (1.0, 0.0), "b": (3.0, 0.0), "c": (5.0, 0.0), "d": (100.0, 0.0)}
    stats = landmark_shift_stats(expected, observed)
    assert stats.median_px == pytest.approx(4.0)
    assert stats.max_px == pytest.approx(100.0)


def test_only_common_landmarks_are_compared() -> None:
    expected = dict(EXPECTED) | {"only_expected": (0.0, 0.0)}
    observed = _shifted(EXPECTED, 0.0, 2.0) | {"only_observed": (5.0, 5.0)}
    stats = landmark_shift_stats(expected, observed)
    assert stats.n == 5
    assert "only_expected" not in stats.per_landmark
    assert "only_observed" not in stats.per_landmark


def test_empty_intersection_raises() -> None:
    with pytest.raises(ValueError, match="no common landmarks"):
        landmark_shift_stats({"a": (0.0, 0.0)}, {"b": (0.0, 0.0)})
    with pytest.raises(ValueError, match="no common landmarks"):
        landmark_shift_stats({}, {})


def test_shift_is_euclidean_distance() -> None:
    stats = landmark_shift_stats({"a": (10.0, 20.0)}, {"a": (13.0, 24.0)})
    assert stats.per_landmark["a"] == pytest.approx(math.hypot(3.0, 4.0))
    assert stats.n == 1


@pytest.mark.safety
def test_is_drifted_threshold_boundaries() -> None:
    """US-C4 boundary rule: strictly greater than threshold flags drift."""
    below = ShiftStats(per_landmark={"a": 4.9}, median_px=4.9, max_px=4.9, n=1)
    exact = ShiftStats(per_landmark={"a": 5.0}, median_px=5.0, max_px=5.0, n=1)
    above = ShiftStats(per_landmark={"a": 5.1}, median_px=5.1, max_px=5.1, n=1)
    assert not is_drifted(below)
    assert not is_drifted(exact)  # exactly at threshold: not drifted
    assert is_drifted(above)
    assert DEFAULT_DRIFT_THRESHOLD_PX == 5.0


def test_is_drifted_custom_threshold() -> None:
    stats = ShiftStats(per_landmark={"a": 3.0}, median_px=3.0, max_px=3.0, n=1)
    assert is_drifted(stats, threshold_px=1.0)
    assert not is_drifted(stats, threshold_px=10.0)
