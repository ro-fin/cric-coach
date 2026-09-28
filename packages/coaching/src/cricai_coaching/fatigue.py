"""US-H3: in-session fatigue scorer — rolling window vs session baseline.

The documented fatigue formula (US-H3 AC "formula documented; components
individually inspectable"):

1. The rolling window is the last ``window`` balls of the session; the
   baseline is every EARLIER ball sharing the window's block context.
2. Control % is computed on balls with a known ``control`` flag on each side;
   the drop is ``baseline% - window%`` in percentage points.
3. Each technique signal compares the window mean against the baseline mean in
   its adverse direction (``higher_is_better`` degrades downward,
   ``lower_is_better`` degrades upward); a signal is degrading when the
   adverse change is at least ``technique_drop_pct`` percent.
4. Fatigue triggers when control drops >= ``control_drop_points`` points AND
   >= ``min_technique_signals`` signals degrade (the US-H3 initial rule:
   15 points and 2 signals).

False-positive guard (US-H3 AC): a block-context change — different intent or
machine settings, e.g. a harder feed speed — never triggers fatigue on its
own. The window must be a single context, and only earlier balls of the SAME
context form the baseline; too few of them means the assessment is honestly
"not evaluated", never a guess.

Fatigue notes are suggestions to the Parent/Coach (report note in Phase 5;
the live nudge is Phase 6): only workload ceilings hard-block (US-H1/H5).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The window mean must FALL to degrade (head stability, footwork quality).
HIGHER_IS_BETTER = "higher_is_better"
#: The window mean must RISE to degrade (trigger-to-contact reaction time).
LOWER_IS_BETTER = "lower_is_better"

#: Default monitored technique signals and their adverse directions (US-H3).
DEFAULT_TECHNIQUE_DIRECTIONS: dict[str, str] = {
    "head_stability_score": HIGHER_IS_BETTER,
    "footwork_score": HIGHER_IS_BETTER,
    "trigger_to_contact_ms": LOWER_IS_BETTER,
}

#: The nudge wording attached to a fatigue note — a suggestion, never a block.
FATIGUE_SUGGESTION = (
    "Fatigue signals detected: water and a five-minute break, or switch to the fun "
    "block. This is a suggestion for the parent or coach; only workload ceilings "
    "hard-block."
)


@dataclass(frozen=True)
class FatigueConfig:
    """Tunable thresholds; defaults implement the US-H3 initial rule."""

    window: int = 50
    min_baseline: int = 30
    control_drop_points: float = 15.0
    min_technique_signals: int = 2
    technique_drop_pct: float = 10.0
    min_metric_n: int = 10
    directions: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_TECHNIQUE_DIRECTIONS)
    )


@dataclass(frozen=True)
class BallSample:
    """One ball's fatigue-relevant slice, DB-free (US-H3).

    ``technique`` maps metric name -> value for whichever monitored metrics
    this ball has; missing metrics are simply absent, never zero-filled.
    """

    ball_no: int
    control: bool | None
    technique: Mapping[str, float] = field(default_factory=dict)
    intent: str | None = None
    machine_settings: Mapping[str, Any] | None = None

    @property
    def context_key(self) -> str:
        """Canonical block context: fatigue only compares like with like."""
        settings = None if self.machine_settings is None else dict(self.machine_settings)
        return json.dumps(
            {"intent": self.intent, "machine_settings": settings},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )


@dataclass(frozen=True)
class SignalReading:
    """One technique signal's window-vs-baseline comparison, fully inspectable."""

    metric: str
    direction: str
    baseline_n: int
    window_n: int
    baseline_mean: float | None
    window_mean: float | None
    adverse_change_pct: float | None  # positive = moving the bad way
    degrading: bool


@dataclass(frozen=True)
class FatigueAssessment:
    """The scorer's full output; ``evaluated=False`` always carries a reason."""

    evaluated: bool
    reason: str | None
    baseline_n: int
    window_n: int
    control_baseline_pct: float | None
    control_window_pct: float | None
    control_drop_points: float | None
    signals: tuple[SignalReading, ...]
    degrading_signals: int
    fatigued: bool
    note: dict[str, Any] | None


def score_fatigue(
    samples: Sequence[BallSample],
    config: FatigueConfig | None = None,
) -> FatigueAssessment:
    """Score the session-so-far for fatigue (US-H3). Pure and deterministic."""
    cfg = config or FatigueConfig()
    _validate_config(cfg)
    ordered = sorted(samples, key=lambda s: s.ball_no)
    if len(ordered) < cfg.window + cfg.min_baseline:
        return _not_evaluated(
            f"insufficient balls: {len(ordered)} < window {cfg.window} "
            f"+ min baseline {cfg.min_baseline}"
        )
    window = ordered[-cfg.window :]
    contexts = {s.context_key for s in window}
    if len(contexts) > 1:
        return _not_evaluated("mixed block context within the rolling window")
    window_context = window[0].context_key
    baseline = [s for s in ordered[: -cfg.window] if s.context_key == window_context]
    if len(baseline) < cfg.min_baseline:
        return _not_evaluated(
            f"block context changed: only {len(baseline)} same-context baseline balls "
            f"(need {cfg.min_baseline})"
        )
    control_baseline = _control_pct(baseline)
    control_window = _control_pct(window)
    if control_baseline is None or control_window is None:
        return _not_evaluated("no control data on one side of the comparison")
    drop = control_baseline - control_window
    signals = tuple(
        _read_signal(metric, direction, baseline, window, cfg)
        for metric, direction in sorted(cfg.directions.items())
    )
    degrading = sum(1 for s in signals if s.degrading)
    fatigued = drop >= cfg.control_drop_points and degrading >= cfg.min_technique_signals
    note: dict[str, Any] = {
        "kind": "fatigue",
        "window": cfg.window,
        "baseline_n": len(baseline),
        "window_n": len(window),
        "control_baseline_pct": round(control_baseline, 1),
        "control_window_pct": round(control_window, 1),
        "control_drop_points": round(drop, 1),
        "degrading_signals": [
            {
                "metric": s.metric,
                "baseline_mean": s.baseline_mean,
                "window_mean": s.window_mean,
                "adverse_change_pct": s.adverse_change_pct,
            }
            for s in signals
            if s.degrading
        ],
        "suggestion": FATIGUE_SUGGESTION,
    }
    return FatigueAssessment(
        evaluated=True,
        reason=None,
        baseline_n=len(baseline),
        window_n=len(window),
        control_baseline_pct=control_baseline,
        control_window_pct=control_window,
        control_drop_points=drop,
        signals=signals,
        degrading_signals=degrading,
        fatigued=fatigued,
        note=note if fatigued else None,
    )


def _validate_config(cfg: FatigueConfig) -> None:
    problems: list[str] = []
    for name in ("window", "min_baseline", "min_technique_signals", "min_metric_n"):
        if getattr(cfg, name) < 1:
            problems.append(f"{name} must be >= 1")
    for name in ("control_drop_points", "technique_drop_pct"):
        if getattr(cfg, name) <= 0:
            problems.append(f"{name} must be > 0")
    problems.extend(
        f"unknown direction for {metric!r}: {direction!r}"
        for metric, direction in sorted(cfg.directions.items())
        if direction not in (HIGHER_IS_BETTER, LOWER_IS_BETTER)
    )
    if problems:
        raise ValueError("invalid fatigue config: " + "; ".join(problems))


def _not_evaluated(reason: str) -> FatigueAssessment:
    return FatigueAssessment(
        evaluated=False,
        reason=reason,
        baseline_n=0,
        window_n=0,
        control_baseline_pct=None,
        control_window_pct=None,
        control_drop_points=None,
        signals=(),
        degrading_signals=0,
        fatigued=False,
        note=None,
    )


def _control_pct(samples: Sequence[BallSample]) -> float | None:
    known = [s.control for s in samples if s.control is not None]
    if not known:
        return None
    return 100.0 * sum(1 for c in known if c) / len(known)


def _read_signal(
    metric: str,
    direction: str,
    baseline: Sequence[BallSample],
    window: Sequence[BallSample],
    cfg: FatigueConfig,
) -> SignalReading:
    baseline_values = [s.technique[metric] for s in baseline if metric in s.technique]
    window_values = [s.technique[metric] for s in window if metric in s.technique]
    baseline_n, window_n = len(baseline_values), len(window_values)
    if baseline_n < cfg.min_metric_n or window_n < cfg.min_metric_n:
        return SignalReading(metric, direction, baseline_n, window_n, None, None, None, False)
    baseline_mean = sum(baseline_values) / baseline_n
    window_mean = sum(window_values) / window_n
    if baseline_mean == 0:
        # No honest relative change from a zero baseline: never degrading.
        return SignalReading(
            metric, direction, baseline_n, window_n, baseline_mean, window_mean, None, False
        )
    change_pct = 100.0 * (window_mean - baseline_mean) / abs(baseline_mean)
    adverse = -change_pct if direction == HIGHER_IS_BETTER else change_pct
    return SignalReading(
        metric=metric,
        direction=direction,
        baseline_n=baseline_n,
        window_n=window_n,
        baseline_mean=baseline_mean,
        window_mean=window_mean,
        adverse_change_pct=adverse,
        degrading=adverse >= cfg.technique_drop_pct,
    )
