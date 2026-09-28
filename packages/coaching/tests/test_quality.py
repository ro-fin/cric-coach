"""US-L4 per-session data-quality score: components with degraded inputs,
weighted composite, honesty banner threshold, pinned contract shape."""

import pytest
from cricai_coaching.quality import (
    COMPONENT_WEIGHTS,
    HONESTY_BANNER_THRESHOLD,
    QualityScore,
    SessionQualityInputs,
    calibration_freshness_component,
    composite_score,
    exposure_component,
    honesty_banner,
    pose_coverage_component,
    score_session,
    sync_component,
    track_coverage_component,
)


def _inputs(
    *,
    ball_nos: tuple[int, ...] = (1, 2),
    event_offsets_ms: dict[int, dict[str, float]] | None = None,
    luma_samples: dict[str, list[float]] | None = None,
    pose_availability: dict[int, dict[str, float]] | None = None,
    track_coverage: dict[int, dict[str, float]] | None = None,
    calibration_age_days: float | None = 0.0,
    calibration_suspect: bool = False,
) -> SessionQualityInputs:
    full = {1: {"C1": 1.0}, 2: {"C1": 1.0}}
    return SessionQualityInputs(
        ball_nos=ball_nos,
        event_offsets_ms=(
            event_offsets_ms
            if event_offsets_ms is not None
            else {1: {"C1": 0.0, "C2": 10.0}, 2: {"C1": 0.0, "C2": 5.0}}
        ),
        luma_samples=luma_samples if luma_samples is not None else {"C1": [120.0, 130.0]},
        pose_availability=pose_availability if pose_availability is not None else full,
        track_coverage=track_coverage if track_coverage is not None else full,
        calibration_age_days=calibration_age_days,
        calibration_suspect=calibration_suspect,
    )


class TestSyncComponent:
    def test_no_balls_scores_zero(self) -> None:
        assert sync_component((), {}) == 0.0

    def test_aligned_multi_camera_balls_count(self) -> None:
        offsets = {1: {"C1": 0.0, "C2": 30.0}, 2: {"C1": 0.0, "C2": 20.0}}
        assert sync_component((1, 2), offsets) == 1.0

    def test_misaligned_ball_does_not_count(self) -> None:
        offsets = {1: {"C1": 0.0, "C2": 100.0}, 2: {"C1": 0.0, "C2": 10.0}}
        assert sync_component((1, 2), offsets) == 0.5

    def test_single_camera_ball_is_unverified(self) -> None:
        assert sync_component((1,), {1: {"C1": 0.0}}) == 0.0

    def test_ball_missing_from_offsets_is_unverified(self) -> None:
        assert sync_component((1, 2), {2: {"C1": 0.0, "C2": 0.0}}) == 0.5

    def test_tolerance_is_configurable(self) -> None:
        offsets = {1: {"C1": 0.0, "C2": 60.0}}
        assert sync_component((1,), offsets) == 0.0
        assert sync_component((1,), offsets, tolerance_ms=80.0) == 1.0


class TestExposureComponent:
    def test_no_samples_scores_zero(self) -> None:
        assert exposure_component({}) == 0.0
        assert exposure_component({"C1": []}) == 0.0

    def test_all_samples_in_band(self) -> None:
        assert exposure_component({"C1": [60.0, 200.0], "C2": [128.0]}) == 1.0

    def test_underexposed_and_blown_out_samples_penalized(self) -> None:
        assert exposure_component({"C1": [10.0, 250.0, 128.0, 128.0]}) == 0.5


class TestBallCoverageComponents:
    def test_no_balls_scores_zero(self) -> None:
        assert pose_coverage_component((), {}) == 0.0
        assert track_coverage_component((), {}) == 0.0

    def test_best_camera_wins_and_missing_ball_contributes_zero(self) -> None:
        availability = {1: {"C1": 0.4, "C2": 0.8}}
        assert pose_coverage_component((1, 2), availability) == pytest.approx(0.4)

    def test_values_above_one_are_clamped(self) -> None:
        assert track_coverage_component((1,), {1: {"C1": 1.5}}) == 1.0

    def test_negative_values_are_clamped_to_zero(self) -> None:
        assert track_coverage_component((1,), {1: {"C1": -0.5}}) == 0.0


class TestCalibrationFreshness:
    def test_missing_calibration_scores_zero(self) -> None:
        assert calibration_freshness_component(None, False) == 0.0

    def test_suspect_flag_scores_zero_regardless_of_age(self) -> None:
        assert calibration_freshness_component(1.0, True) == 0.0

    def test_fresh_age_scores_one(self) -> None:
        assert calibration_freshness_component(30.0, False) == 1.0

    def test_stale_age_scores_zero(self) -> None:
        assert calibration_freshness_component(120.0, False) == 0.0
        assert calibration_freshness_component(500.0, False) == 0.0

    def test_mid_age_decays_linearly(self) -> None:
        assert calibration_freshness_component(75.0, False) == pytest.approx(0.5)


class TestCompositeScore:
    def test_weighted_mean(self) -> None:
        components = {
            "sync": 1.0,
            "exposure": 0.0,
            "pose_coverage": 1.0,
            "track_coverage": 1.0,
            "calibration_freshness": 1.0,
        }
        assert composite_score(components) == pytest.approx(0.85)

    def test_weights_sum_to_one(self) -> None:
        assert sum(COMPONENT_WEIGHTS.values()) == pytest.approx(1.0)

    def test_unknown_or_missing_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="components must be exactly"):
            composite_score({"sync": 1.0})


class TestHonestyBanner:
    def test_no_banner_at_or_above_threshold(self) -> None:
        assert honesty_banner(HONESTY_BANNER_THRESHOLD, {}) is None
        assert honesty_banner(1.0, {}) is None

    def test_banner_names_weakest_components_in_severity_order(self) -> None:
        components = {
            "sync": 0.9,
            "exposure": 0.1,
            "pose_coverage": 0.3,
            "track_coverage": 0.8,
            "calibration_freshness": 0.2,
        }
        banner = honesty_banner(0.5, components)
        assert banner is not None
        assert "score 50%" in banner
        assert "exposure, calibration freshness, pose coverage" in banner
        assert "unreliable" in banner


class TestScoreSession:
    def test_good_session_scores_high_without_banner(self) -> None:
        score = score_session(_inputs())
        assert score.components == {
            "sync": 1.0,
            "exposure": 1.0,
            "pose_coverage": 1.0,
            "track_coverage": 1.0,
            "calibration_freshness": 1.0,
        }
        assert score.composite == pytest.approx(1.0)
        assert score.banner is None

    def test_degraded_session_gets_banner(self) -> None:
        score = score_session(
            _inputs(
                event_offsets_ms={},
                luma_samples={},
                pose_availability={},
                track_coverage={},
                calibration_age_days=None,
            )
        )
        assert score.composite == pytest.approx(0.0)
        assert score.banner is not None

    def test_tolerance_passes_through(self) -> None:
        offsets = {1: {"C1": 0.0, "C2": 60.0}, 2: {"C1": 0.0, "C2": 60.0}}
        strict = score_session(_inputs(event_offsets_ms=offsets))
        loose = score_session(_inputs(event_offsets_ms=offsets), tolerance_ms=80.0)
        assert strict.components["sync"] == 0.0
        assert loose.components["sync"] == 1.0

    def test_as_dict_matches_pinned_contract(self) -> None:
        score = QualityScore(components={"sync": 0.5}, composite=0.5, banner="b")
        payload = score.as_dict()
        assert payload == {"components": {"sync": 0.5}, "composite": 0.5, "banner": "b"}
        assert payload["components"] is not score.components  # defensive copy
