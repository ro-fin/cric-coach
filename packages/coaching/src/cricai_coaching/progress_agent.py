"""Progress agent (US-J4): the longitudinal picture from frozen baselines.

Builds ProgressSnapshot dicts (pinned contract #7) from stored
``metric_baselines`` rows — the nightly job's frozen snapshots. Past points
are NEVER recomputed here: this module only reads what the nightly job wrote
(``as_of`` filters to a frozen view so report generation sees no
mid-generation drift, US-J4 AC).

Guardrails (contract #7): ``qualified`` is False unless the history has at
least :data:`MIN_SESSIONS` points and every point carries at least
:data:`MIN_BALLS_PER_POINT` balls — "new personal best" and "regression"
events never surface to a child without them (US-J4 AC). ``direction`` is
``improving | flat | regressing`` by a least-squares slope threshold over the
point dates.

History given downstream (LLM context, US-J4 AC) is size-bounded and typed:
points are capped at :data:`MAX_HISTORY_POINTS` per snapshot, dropping the
oldest first, and every snapshot/point is a fixed-key dict, never a raw dump.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import fmean
from typing import Any

from cricai_data.models import MetricBaseline

from cricai_coaching.fatigue import HIGHER_IS_BETTER, LOWER_IS_BETTER

#: Contract-#7 qualification guardrails (>= 3 sessions, >= 30 balls/point).
MIN_SESSIONS = 3
MIN_BALLS_PER_POINT = 30

#: Size bound on the typed history handed downstream (drop oldest first).
MAX_HISTORY_POINTS = 12

#: Direction vocabulary (contract #7).
IMPROVING = "improving"
FLAT = "flat"
REGRESSING = "regressing"

#: Polarity for metrics whose mean is not a trend at all: internal release
#: primitives (frame indices, session-timeline milliseconds, pixel tuples),
#: consistency-not-level measures (release height) and signed/mix-dependent
#: geometry (turn direction depends on the variation bowled). These never
#: build a snapshot, so they can never alert, celebrate or ship as a trend.
UNTRENDED = "untrended"

#: Per-metric trend polarity (US-G5/J4, finding [7/74]): which way is BETTER
#: for every baselined machine metric. ``nightly_baselines`` baselines every
#: numeric/boolean machine metric indiscriminately, so the progress agent —
#: the single reader feeding trends, regression alerts and personal bests —
#: owns the polarity map. Vocabulary is ``cricai_coaching.fatigue``'s
#: (HIGHER_IS_BETTER / LOWER_IS_BETTER) plus :data:`UNTRENDED`. Unregistered
#: metrics keep the Phase-5 higher-is-better default.
METRIC_DIRECTIONS: dict[str, str] = {
    # Batting technique/control (Phase 5): all degrade downward.
    "head_stability_score": HIGHER_IS_BETTER,
    "footwork_score": HIGHER_IS_BETTER,
    "front_foot_direction_cm": HIGHER_IS_BETTER,
    "front_foot_direction_px": HIGHER_IS_BETTER,
    "balance_margin_px": HIGHER_IS_BETTER,
    "control_pct": HIGHER_IS_BETTER,
    "in_control": HIGHER_IS_BETTER,
    # Bowling accuracy (US-I4): fraction of confident target hits.
    "target_hit": HIGHER_IS_BETTER,
    # Lower is better: reaction lag and the US-I2 action checkpoints, where a
    # positive value IS the fault (falling away; head drifting off the release
    # line) — trending these as higher-is-better inverted every verdict.
    "trigger_to_contact_ms": LOWER_IS_BETTER,
    "falling_away_deg": LOWER_IS_BETTER,
    "head_offset_at_release_px": LOWER_IS_BETTER,
    "head_offset_at_release_cm": LOWER_IS_BETTER,
    # Internal primitives / consistency / signed geometry: never a trend.
    "release_frame": UNTRENDED,
    "release_ms": UNTRENDED,
    "release_frame_offset": UNTRENDED,
    "release_height_cm": UNTRENDED,
    "hand_xy": UNTRENDED,
    "turn_cm": UNTRENDED,
    "apex_m": UNTRENDED,
    "dip_flag": UNTRENDED,
}


def metric_direction(metric: str, overrides: Mapping[str, str] | None = None) -> str:
    """The trend polarity governing ``metric`` (finding [7/74]).

    ``overrides`` (per-call, e.g. a test or a future config surface) win over
    :data:`METRIC_DIRECTIONS`; unknown metrics default to higher-is-better —
    the Phase-5 behaviour, unchanged for every metric that predates the map.
    """
    if overrides is not None and metric in overrides:
        return overrides[metric]
    return METRIC_DIRECTIONS.get(metric, HIGHER_IS_BETTER)


class ProgressError(ValueError):
    """Malformed baseline-row input: the snapshot cannot be built honestly."""


@dataclass(frozen=True)
class ProgressConfig:
    """Trend guardrails: slope units are metric-value change per day."""

    slope_threshold: float = 0.01
    max_points: int = MAX_HISTORY_POINTS
    min_sessions: int = MIN_SESSIONS
    min_balls_per_point: int = MIN_BALLS_PER_POINT


DEFAULT_PROGRESS_CONFIG = ProgressConfig()


def baseline_to_mapping(row: MetricBaseline) -> dict[str, Any]:
    """One stored baseline row as the plain-dict shape this module consumes."""
    return {
        "metric": row.metric,
        "zone_key": row.zone_key,
        "window": row.window,
        "snapshot_date": row.snapshot_date,
        "value": row.value,
        "n": row.n,
        "payload": dict(row.payload),
    }


def _row_date(row: Mapping[str, Any]) -> date:
    snapshot_date = row.get("snapshot_date")
    if isinstance(snapshot_date, date):
        return snapshot_date
    raise ProgressError(f"baseline row needs a snapshot_date date, got {snapshot_date!r}")


def _point(row: Mapping[str, Any]) -> dict[str, Any]:
    """One typed history point (contract #7): fixed keys, never a raw dump.

    ``session_id`` reads the scalar the nightly job writes (null when several
    sessions merged into the date's slice); rows written before the scalar key
    existed fall back to a lone ``session_ids`` entry, so old rows keep their
    session linkage.
    """
    payload = row.get("payload") or {}
    session_id = payload.get("session_id")
    if session_id is None:
        session_ids = payload.get("session_ids")
        if isinstance(session_ids, list) and len(session_ids) == 1:
            session_id = session_ids[0]
    return {
        "session_id": session_id,
        "date": _row_date(row).isoformat(),
        "value": float(row["value"]),
        "n": int(row["n"]),
    }


def _slope(points: Sequence[Mapping[str, Any]]) -> float:
    """Least-squares slope of value over days since the first point."""
    if len(points) < 2:
        return 0.0
    first = date.fromisoformat(str(points[0]["date"]))
    xs = [float((date.fromisoformat(str(point["date"])) - first).days) for point in points]
    ys = [float(point["value"]) for point in points]
    x_mean, y_mean = fmean(xs), fmean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0.0:
        return 0.0  # all points on one day: no time axis to trend over
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / denominator


def direction_of(slope: float, *, threshold: float, higher_is_better: bool = True) -> str:
    """improving | flat | regressing by the slope threshold (contract #7)."""
    signed = slope if higher_is_better else -slope
    if signed >= threshold:
        return IMPROVING
    if signed <= -threshold:
        return REGRESSING
    return FLAT


def build_snapshots(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of: date | None = None,
    config: ProgressConfig = DEFAULT_PROGRESS_CONFIG,
    directions: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """ProgressSnapshot dicts (contract #7) per (metric, zone_key, window).

    ``as_of`` freezes the view: rows dated after it are ignored, so a report
    generated against ``as_of`` reads the same history no matter when the
    nightly job next runs (US-J4 snapshot isolation). Points are ordered by
    snapshot date, capped at ``config.max_points`` dropping the oldest, and
    ``baseline`` is the mean of the retained point values.

    Each metric's ``direction`` respects its :func:`metric_direction` polarity
    (finding [7/74]: a lower-is-better metric trending down is IMPROVING);
    :data:`UNTRENDED` metrics build no snapshot at all. ``history_high`` /
    ``history_low`` carry the extremes of the FULL prior history (everything
    before the latest point, cap ignored) so :func:`personal_best` compares
    against all history, not the render window (finding [80]).
    """
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        if as_of is not None and _row_date(row) > as_of:
            continue
        key = (str(row["metric"]), str(row["zone_key"]), str(row["window"]))
        grouped.setdefault(key, []).append(row)
    snapshots: list[dict[str, Any]] = []
    for metric, zone_key, window in sorted(grouped):
        direction = metric_direction(metric, directions)
        if direction == UNTRENDED:
            continue
        group = sorted(grouped[(metric, zone_key, window)], key=_row_date)
        full_history = [_point(row) for row in group]
        prior_values = [float(point["value"]) for point in full_history[:-1]]
        points = full_history[-config.max_points :]
        qualified = len(points) >= config.min_sessions and all(
            point["n"] >= config.min_balls_per_point for point in points
        )
        slope = _slope(points)
        snapshots.append(
            {
                "metric": metric,
                "zone_key": zone_key,
                "window": window,
                "points": points,
                "baseline": round(fmean(point["value"] for point in points), 4),
                "direction": direction_of(
                    slope,
                    threshold=config.slope_threshold,
                    higher_is_better=direction == HIGHER_IS_BETTER,
                ),
                "qualified": qualified,
                "history_high": max(prior_values) if prior_values else None,
                "history_low": min(prior_values) if prior_values else None,
            }
        )
    return snapshots


def personal_best(snapshot: Mapping[str, Any], *, higher_is_better: bool | None = None) -> bool:
    """True when the latest point strictly beats ALL history — guardrailed.

    "All history" means the full uncapped baseline history: the snapshot's
    ``history_high``/``history_low`` extremes cover points the render-window
    cap dropped, so a true best that scrolled out of the window still blocks a
    merely window-local best from celebrating (US-J4/K4, finding [80]).
    Snapshots without those keys (hand-built callers) fall back to the window.

    ``higher_is_better=None`` consults :func:`metric_direction` (finding
    [7/74]: a lower-is-better metric's best is its all-time LOW; an
    :data:`UNTRENDED` metric never celebrates). An unqualified snapshot never
    celebrates (US-J4 AC: statistical guardrails before surfacing "new
    personal best" to a child).
    """
    points = list(snapshot["points"])
    if not snapshot["qualified"] or len(points) < 2:
        return False
    if higher_is_better is None:
        direction = metric_direction(str(snapshot["metric"]))
        if direction == UNTRENDED:
            return False
        higher = direction == HIGHER_IS_BETTER
    else:
        higher = higher_is_better
    latest = float(points[-1]["value"])
    window_history = [float(point["value"]) for point in points[:-1]]
    if higher:
        high = snapshot.get("history_high")
        return latest > (float(high) if high is not None else max(window_history))
    low = snapshot.get("history_low")
    return latest < (float(low) if low is not None else min(window_history))


def regression_alert(snapshot: Mapping[str, Any]) -> bool:
    """True when a QUALIFIED snapshot is regressing (US-J4 guardrail)."""
    return bool(snapshot["qualified"]) and snapshot["direction"] == REGRESSING
