"""US-H3 acceptance: rolling-window fatigue scorer + context-change guard."""

from collections.abc import Mapping
from typing import Any

import pytest
from cricai_coaching.fatigue import (
    DEFAULT_TECHNIQUE_DIRECTIONS,
    FATIGUE_SUGGESTION,
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    BallSample,
    FatigueConfig,
    score_fatigue,
)

#: Small windows keep test sessions readable; thresholds match the US-H3 rule.
CFG = FatigueConfig(window=10, min_baseline=10, min_metric_n=5)


def _balls(
    start: int,
    count: int,
    *,
    control_rate: float = 1.0,
    head: float = 0.8,
    trigger: float = 500.0,
    footwork: float = 0.7,
    intent: str | None = "technical",
    machine_settings: Mapping[str, Any] | None = None,
) -> list[BallSample]:
    """``count`` balls from ``start``; the first ``control_rate`` share controlled."""
    controlled = round(count * control_rate)
    return [
        BallSample(
            ball_no=start + i,
            control=i < controlled,
            technique={
                "head_stability_score": head,
                "trigger_to_contact_ms": trigger,
                "footwork_score": footwork,
            },
            intent=intent,
            machine_settings=machine_settings,
        )
        for i in range(count)
    ]


def _fresh_then_tired(
    *,
    window_control: float = 0.6,
    window_head: float = 0.65,
    window_trigger: float = 575.0,
) -> list[BallSample]:
    """Baseline of 10 sharp balls, then a 10-ball window shaped by the kwargs."""
    baseline = _balls(1, 10, control_rate=0.9)
    window = _balls(11, 10, control_rate=window_control, head=window_head, trigger=window_trigger)
    return baseline + window


class TestTrigger:
    def test_control_drop_plus_two_signals_triggers(self) -> None:
        assessment = score_fatigue(_fresh_then_tired(), CFG)
        assert assessment.evaluated is True
        assert assessment.reason is None
        assert assessment.control_baseline_pct == 90.0
        assert assessment.control_window_pct == 60.0
        assert assessment.control_drop_points == pytest.approx(30.0)
        assert assessment.degrading_signals == 2
        assert assessment.fatigued is True

    def test_note_components_are_individually_inspectable(self) -> None:
        note = score_fatigue(_fresh_then_tired(), CFG).note
        assert note is not None
        assert note["kind"] == "fatigue"
        assert note["window"] == 10
        assert note["baseline_n"] == 10
        assert note["window_n"] == 10
        assert note["control_drop_points"] == 30.0
        assert {s["metric"] for s in note["degrading_signals"]} == {
            "head_stability_score",
            "trigger_to_contact_ms",
        }
        assert note["suggestion"] == FATIGUE_SUGGESTION

    def test_control_drop_alone_is_not_fatigue(self) -> None:
        session = _fresh_then_tired(window_head=0.79, window_trigger=505.0)
        assessment = score_fatigue(session, CFG)
        assert assessment.control_drop_points == pytest.approx(30.0)
        assert assessment.degrading_signals == 0
        assert assessment.fatigued is False
        assert assessment.note is None

    def test_one_degrading_signal_is_not_enough(self) -> None:
        session = _fresh_then_tired(window_trigger=500.0)
        assessment = score_fatigue(session, CFG)
        assert assessment.degrading_signals == 1
        assert assessment.fatigued is False

    def test_technique_decline_without_control_drop_is_not_fatigue(self) -> None:
        session = _fresh_then_tired(window_control=0.9)
        assessment = score_fatigue(session, CFG)
        assert assessment.control_drop_points == pytest.approx(0.0)
        assert assessment.degrading_signals == 2
        assert assessment.fatigued is False

    def test_boundary_drop_and_signals_trigger_exactly(self) -> None:
        cfg = FatigueConfig(window=10, min_baseline=10, min_metric_n=5, control_drop_points=30.0)
        assessment = score_fatigue(_fresh_then_tired(), cfg)
        assert assessment.fatigued is True  # drop == threshold, signals == minimum


class TestFalsePositiveGuard:
    def test_machine_settings_change_suppresses_the_flag(self) -> None:
        """US-H3 AC: a harder feed after a block change never alone flags fatigue."""
        baseline = _balls(1, 10, control_rate=0.9, machine_settings={"speed_kph": 75})
        window = _balls(
            11,
            10,
            control_rate=0.5,
            head=0.6,
            trigger=600.0,
            machine_settings={"speed_kph": 95},
        )
        assessment = score_fatigue(baseline + window, CFG)
        assert assessment.evaluated is False
        assert assessment.reason is not None
        assert "block context changed" in assessment.reason
        assert assessment.fatigued is False

    def test_intent_change_suppresses_the_flag(self) -> None:
        session = _balls(1, 10, control_rate=0.9) + _balls(
            11, 10, control_rate=0.5, head=0.6, intent="match_scenario"
        )
        assessment = score_fatigue(session, CFG)
        assert assessment.evaluated is False

    def test_mixed_context_within_window_suppresses(self) -> None:
        session = (
            _balls(1, 10, control_rate=0.9)
            + _balls(11, 5, control_rate=0.5)
            + _balls(16, 5, control_rate=0.5, intent="fun")
        )
        assessment = score_fatigue(session, CFG)
        assert assessment.evaluated is False
        assert assessment.reason == "mixed block context within the rolling window"

    def test_same_context_comparison_survives_other_context_noise(self) -> None:
        """Earlier balls from OTHER contexts are excluded, not confounding."""
        fun_block = _balls(1, 10, control_rate=0.2, intent="fun")
        baseline = _balls(11, 10, control_rate=0.9)
        window = _balls(21, 10, control_rate=0.6, head=0.65, trigger=575.0)
        assessment = score_fatigue(fun_block + baseline + window, CFG)
        assert assessment.evaluated is True
        assert assessment.baseline_n == 10  # the fun block never joins the baseline
        assert assessment.fatigued is True


class TestHonestNonEvaluation:
    def test_insufficient_balls(self) -> None:
        assessment = score_fatigue(_balls(1, 19), CFG)
        assert assessment.evaluated is False
        assert assessment.reason == "insufficient balls: 19 < window 10 + min baseline 10"

    def test_no_control_data_in_window(self) -> None:
        baseline = _balls(1, 10, control_rate=0.9)
        window = [
            BallSample(ball_no=11 + i, control=None, technique={}, intent="technical")
            for i in range(10)
        ]
        assessment = score_fatigue(baseline + window, CFG)
        assert assessment.evaluated is False
        assert assessment.reason == "no control data on one side of the comparison"


class TestSignals:
    def test_lower_is_better_direction(self) -> None:
        session = _fresh_then_tired(window_trigger=575.0)
        (trigger,) = [
            s for s in score_fatigue(session, CFG).signals if s.metric == "trigger_to_contact_ms"
        ]
        assert trigger.direction == LOWER_IS_BETTER
        assert trigger.adverse_change_pct == pytest.approx(15.0)
        assert trigger.degrading is True

        improving = _fresh_then_tired(window_trigger=440.0)
        (faster,) = [
            s for s in score_fatigue(improving, CFG).signals if s.metric == "trigger_to_contact_ms"
        ]
        assert faster.adverse_change_pct == pytest.approx(-12.0)
        assert faster.degrading is False

    def test_higher_is_better_improvement_is_not_adverse(self) -> None:
        session = _fresh_then_tired(window_head=0.9, window_trigger=500.0)
        (head,) = [
            s for s in score_fatigue(session, CFG).signals if s.metric == "head_stability_score"
        ]
        assert head.direction == HIGHER_IS_BETTER
        assert head.adverse_change_pct == pytest.approx(-12.5)
        assert head.degrading is False

    def test_sparse_metric_is_skipped_honestly(self) -> None:
        baseline = _balls(1, 10, control_rate=0.9)
        window = [
            BallSample(
                ball_no=11 + i,
                control=i < 6,
                technique={"head_stability_score": 0.6}
                if i < 3
                else {"head_stability_score": 0.6, "trigger_to_contact_ms": 575.0},
                intent="technical",
            )
            for i in range(10)
        ]
        assessment = score_fatigue(baseline + window, CFG)
        (trigger,) = [s for s in assessment.signals if s.metric == "trigger_to_contact_ms"]
        assert trigger.window_n == 7
        assert trigger.degrading is True
        (footwork,) = [s for s in assessment.signals if s.metric == "footwork_score"]
        assert footwork.window_n == 0
        assert footwork.baseline_mean is None
        assert footwork.adverse_change_pct is None
        assert footwork.degrading is False

    def test_zero_baseline_mean_never_degrades(self) -> None:
        cfg = FatigueConfig(
            window=10,
            min_baseline=10,
            min_metric_n=5,
            directions={"balance_offset": HIGHER_IS_BETTER},
        )
        session = [
            BallSample(
                ball_no=i + 1,
                control=True,
                technique={"balance_offset": 0.0 if i < 10 else 1.0},
                intent="technical",
            )
            for i in range(20)
        ]
        (signal,) = score_fatigue(session, cfg).signals
        assert signal.baseline_mean == 0.0
        assert signal.adverse_change_pct is None
        assert signal.degrading is False


class TestConfigAndInputs:
    def test_default_config_implements_the_initial_rule(self) -> None:
        cfg = FatigueConfig()
        assert cfg.window == 50
        assert cfg.control_drop_points == 15.0
        assert cfg.min_technique_signals == 2
        assert cfg.directions == DEFAULT_TECHNIQUE_DIRECTIONS

    def test_default_config_path(self) -> None:
        baseline = _balls(1, 40, control_rate=0.9)
        window = _balls(41, 50, control_rate=0.6, head=0.65, trigger=575.0)
        assessment = score_fatigue(baseline + window)
        assert assessment.evaluated is True
        assert assessment.fatigued is True

    def test_invalid_config_rejected(self) -> None:
        with pytest.raises(ValueError, match="window must be >= 1"):
            score_fatigue([], FatigueConfig(window=0))
        with pytest.raises(ValueError, match="control_drop_points must be > 0"):
            score_fatigue([], FatigueConfig(control_drop_points=0.0))
        with pytest.raises(ValueError, match="unknown direction for 'head'"):
            score_fatigue([], FatigueConfig(directions={"head": "sideways"}))

    def test_samples_are_sorted_by_ball_no(self) -> None:
        session = _fresh_then_tired()
        assessment = score_fatigue(list(reversed(session)), CFG)
        assert assessment.evaluated is True
        assert assessment.fatigued is True

    def test_context_key_is_canonical(self) -> None:
        a = BallSample(ball_no=1, control=True, machine_settings={"a": 1, "b": 2})
        b = BallSample(ball_no=2, control=True, machine_settings={"b": 2, "a": 1})
        assert a.context_key == b.context_key
        plain = BallSample(ball_no=3, control=True)
        assert plain.context_key != a.context_key
        assert "null" in plain.context_key
