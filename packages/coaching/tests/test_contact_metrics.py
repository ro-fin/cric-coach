"""US-E3 contact metrics: exact-geometry synthetic skeletons per classifier boundary.

Every classifier rule-table boundary is exercised with hand-placed landmarks so the
expected class is arithmetic, not fixture folklore; the deterministic
:class:`FakePoseProvider` covers the end-to-end path over a full synthetic track.
"""

import math

import pytest
from cricai_coaching.contact_metrics import (
    CONTACT_METRIC_KEYS,
    ContactConfig,
    MetricError,
    MetricValue,
    compute_contact,
)
from cricai_vision.pose import (
    LANDMARK_NAMES,
    N_LANDMARKS,
    FakePoseProvider,
    Landmark,
    PoseFrame,
    PoseTrack,
)

#: Side-on right-handed stance anchors (mirrors the fake provider's geometry).
BASE: dict[str, tuple[float, float]] = {
    "nose": (960.0, 300.0),
    "left_shoulder": (930.0, 420.0),
    "right_shoulder": (990.0, 420.0),
    "left_elbow": (900.0, 520.0),
    "right_elbow": (1020.0, 520.0),
    "left_wrist": (890.0, 610.0),
    "right_wrist": (1030.0, 610.0),
    "left_hip": (940.0, 640.0),
    "right_hip": (980.0, 640.0),
    "left_knee": (930.0, 800.0),
    "right_knee": (990.0, 800.0),
    "left_ankle": (920.0, 950.0),
    "right_ankle": (1000.0, 950.0),
    "left_heel": (915.0, 965.0),
    "right_heel": (1005.0, 965.0),
    "left_foot_index": (905.0, 975.0),
    "right_foot_index": (1015.0, 975.0),
}

_INDEX_TO_NAME = {index: name for name, index in LANDMARK_NAMES.items()}


def _frame(
    frame_no: int,
    overrides: dict[str, tuple[float, float]] | None = None,
    *,
    visibility: float = 0.95,
    vis_overrides: dict[str, float] | None = None,
) -> PoseFrame:
    overrides = overrides or {}
    vis_overrides = vis_overrides or {}
    landmarks = []
    for index in range(N_LANDMARKS):
        name = _INDEX_TO_NAME.get(index, "")
        xy = overrides.get(name, BASE.get(name, (960.0, 540.0)))
        landmarks.append(
            Landmark(
                image_xy=xy,
                world_xyz=(0.0, 0.0, 0.0),
                visibility=vis_overrides.get(name, visibility),
            )
        )
    return PoseFrame(frame_no=frame_no, landmarks=tuple(landmarks))


def _track(frames: list[PoseFrame], *, fps: float = 120.0) -> PoseTrack:
    return PoseTrack(
        model_name="test-pose",
        model_version="0",
        fps=fps,
        frames=tuple(frames),
        subject_confidence=0.99,
    )


def _stance_track(n_frames: int) -> PoseTrack:
    return _track([_frame(i) for i in range(n_frames)])


# --- MetricValue contract -------------------------------------------------------------


def test_metric_value_null_without_reason_is_rejected() -> None:
    with pytest.raises(MetricError, match="no silent zeros"):
        MetricValue(None, "cm", 0.0)


def test_metric_value_confidence_out_of_range_is_rejected() -> None:
    with pytest.raises(MetricError, match="confidence"):
        MetricValue(1.0, "cm", 1.5)


def test_metric_value_payload_includes_optional_keys_only_when_meaningful() -> None:
    bare = MetricValue(1.0, "cm", 0.9).to_payload()
    assert bare == {"value": 1.0, "unit": "cm", "confidence": 0.9}
    full = MetricValue(None, "cm", 0.0, reason="why", proxy=True, source="manual").to_payload()
    assert full == {
        "value": None,
        "unit": "cm",
        "confidence": 0.0,
        "reason": "why",
        "proxy": True,
        "source": "manual",
    }


# --- config validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"front_side": "forward"}, "front_side"),
        ({"toward_ball_sign": 0.5}, "toward_ball_sign"),
        ({"baseline_frames": 0}, "baseline_frames"),
        ({"bat_path_window_frames": 1}, "bat_path_window_frames"),
        ({"contact_point_late_max_px": -5.0}, "contact-point rule table"),
        ({"contact_point_cramped_max_px": 40.0}, "contact-point rule table"),
        ({"bat_path_closed_min_deg": 30.0}, "bat-path rule table"),
        ({"bat_path_straight_min_deg": 70.0}, "bat-path rule table"),
        ({"bat_path_straight_max_deg": 120.0}, "bat-path rule table"),
    ],
)
def test_config_rule_tables_must_be_ordered(kwargs: dict[str, float], match: str) -> None:
    with pytest.raises(MetricError, match=match):
        ContactConfig(**kwargs)  # type: ignore[arg-type]


# --- input validation -----------------------------------------------------------------


def test_rejects_nonpositive_px_per_cm() -> None:
    with pytest.raises(MetricError, match="px_per_cm"):
        compute_contact(_stance_track(4), contact_frame=3, px_per_cm=0.0)


@pytest.mark.parametrize(
    ("release_frame", "contact_frame"),
    [(-1, 3), (3, 2), (0, 4)],
)
def test_rejects_out_of_range_frames(release_frame: int, contact_frame: int) -> None:
    with pytest.raises(MetricError, match="contact_frame"):
        compute_contact(_stance_track(4), contact_frame=contact_frame, release_frame=release_frame)


# --- availability gate ----------------------------------------------------------------


def test_low_availability_nulls_every_metric_with_reason() -> None:
    track = _track([_frame(i, visibility=0.3) for i in range(4)])
    metrics = compute_contact(track, contact_frame=3)
    assert tuple(metrics) == CONTACT_METRIC_KEYS
    for key, value in metrics.items():
        assert value.value is None
        assert value.reason is not None and "pose availability" in value.reason
        # Tracking-free stand-ins keep their proxy flag even when null (US-E3 AC).
        assert value.proxy is (key in ("bat_path_class", "contact_point_class"))


# --- front_foot_direction ---------------------------------------------------------------


def test_front_foot_direction_exact_displacement_with_scale() -> None:
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, {"left_ankle": (880.0, 950.0)})]
    metrics = compute_contact(_track(frames), contact_frame=3, px_per_cm=2.0)
    px = metrics["front_foot_direction_px"]
    cm = metrics["front_foot_direction_cm"]
    assert px.value == 40.0  # (880 - 920) * -1: 40 px toward the ball line
    assert px.unit == "px"
    assert cm.value == 20.0
    assert cm.unit == "cm"
    assert px.confidence == 0.95
    assert cm.reason is None


def test_front_foot_direction_px_fallback_without_scale() -> None:
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, {"left_ankle": (880.0, 950.0)})]
    metrics = compute_contact(_track(frames), contact_frame=3)
    assert metrics["front_foot_direction_px"].value == 40.0
    cm = metrics["front_foot_direction_cm"]
    assert cm.value is None
    assert cm.reason is not None and "px_per_cm" in cm.reason


def test_front_foot_baseline_clips_to_contact_frame() -> None:
    # contact at frame 1 with baseline_frames=3: baseline = mean(x at frames 0..1).
    frames = [_frame(0), _frame(1, {"left_ankle": (900.0, 950.0)})]
    metrics = compute_contact(_track(frames), contact_frame=1)
    assert metrics["front_foot_direction_px"].value == 10.0  # (900 - 910) * -1


def test_front_foot_positive_toward_ball_sign() -> None:
    config = ContactConfig(toward_ball_sign=1.0)
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, {"left_ankle": (940.0, 950.0)})]
    metrics = compute_contact(_track(frames), contact_frame=3, config=config)
    assert metrics["front_foot_direction_px"].value == 20.0


def test_front_foot_low_ankle_visibility_nulls_both_fields() -> None:
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, vis_overrides={"left_ankle": 0.2})]
    metrics = compute_contact(_track(frames), contact_frame=3, px_per_cm=2.0)
    for key in ("front_foot_direction_cm", "front_foot_direction_px"):
        assert metrics[key].value is None
        reason = metrics[key].reason
        assert reason is not None and "left_ankle" in reason
    # Other metrics are unaffected by the ankle-only visibility failure.
    assert metrics["contact_point_class"].value is not None


# --- head_stability_score ---------------------------------------------------------------


def test_head_stability_documented_formula_exact_value() -> None:
    # nose xs [960, 990, 930, 960]: median 960, mean deviation 15; shoulder width 60
    # -> full scale 0.5 * 60 = 30 -> score = 1 - 15/30 = 0.5.
    frames = [
        _frame(0),
        _frame(1, {"nose": (990.0, 300.0)}),
        _frame(2, {"nose": (930.0, 300.0)}),
        _frame(3),
    ]
    metrics = compute_contact(_track(frames), contact_frame=3)
    assert metrics["head_stability_score"].value == 0.5
    assert metrics["head_stability_score"].unit == "score"


def test_head_stability_still_head_scores_one() -> None:
    metrics = compute_contact(_stance_track(4), contact_frame=3)
    assert metrics["head_stability_score"].value == 1.0


def test_head_stability_clamps_to_zero() -> None:
    frames = [
        _frame(0),
        _frame(1, {"nose": (1080.0, 300.0)}),
        _frame(2, {"nose": (840.0, 300.0)}),
        _frame(3),
    ]
    metrics = compute_contact(_track(frames), contact_frame=3)
    assert metrics["head_stability_score"].value == 0.0


def test_head_stability_window_respects_release_frame() -> None:
    frames = [_frame(0, {"nose": (900.0, 300.0)}), _frame(1), _frame(2), _frame(3)]
    from_start = compute_contact(_track(frames), contact_frame=3)
    from_release = compute_contact(_track(frames), contact_frame=3, release_frame=1)
    assert from_start.get("head_stability_score") is not None
    assert from_start["head_stability_score"].value == 0.5  # mad 15 vs full scale 30
    assert from_release["head_stability_score"].value == 1.0


def test_head_stability_degenerate_shoulder_width_is_null() -> None:
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, {"right_shoulder": (930.0, 420.0)})]
    value = compute_contact(_track(frames), contact_frame=3)["head_stability_score"]
    assert value.value is None
    assert value.reason is not None and "degenerate shoulder width" in value.reason


def test_head_stability_low_nose_visibility_in_window_is_null() -> None:
    frames = [_frame(0), _frame(1, vis_overrides={"nose": 0.2}), _frame(2), _frame(3)]
    value = compute_contact(_track(frames), contact_frame=3)["head_stability_score"]
    assert value.value is None
    assert value.reason is not None and "nose" in value.reason


def test_head_stability_low_shoulder_visibility_at_contact_is_null() -> None:
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, vis_overrides={"left_shoulder": 0.3})]
    value = compute_contact(_track(frames), contact_frame=3)["head_stability_score"]
    assert value.value is None
    assert value.reason is not None and "left_shoulder" in value.reason


# --- balance_at_contact -----------------------------------------------------------------


def test_balance_boundary_margin_zero_is_stable() -> None:
    # offset toward ball = (930 - 900) * -1 = -30; margin = -30 + 30 = 0 -> stable.
    frames = [_frame(0), _frame(1), _frame(2)]
    frames.append(_frame(3, {"nose": (930.0, 300.0), "left_ankle": (900.0, 950.0)}))
    metrics = compute_contact(_track(frames), contact_frame=3)
    assert metrics["balance_at_contact"].value == "stable"
    assert metrics["balance_margin_px"].value == 0.0


def test_balance_beyond_threshold_is_falling_away() -> None:
    frames = [_frame(0), _frame(1), _frame(2)]
    frames.append(_frame(3, {"nose": (931.0, 300.0), "left_ankle": (900.0, 950.0)}))
    metrics = compute_contact(_track(frames), contact_frame=3)
    assert metrics["balance_at_contact"].value == "falling_away"
    assert metrics["balance_margin_px"].value == -1.0


def test_balance_head_toward_ball_is_stable_with_margin() -> None:
    frames = [_frame(0), _frame(1), _frame(2)]
    frames.append(_frame(3, {"nose": (890.0, 300.0), "left_ankle": (900.0, 950.0)}))
    metrics = compute_contact(_track(frames), contact_frame=3)
    assert metrics["balance_at_contact"].value == "stable"
    assert metrics["balance_margin_px"].value == 40.0


def test_balance_low_visibility_nulls_both_fields() -> None:
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, vis_overrides={"nose": 0.4})]
    metrics = compute_contact(_track(frames), contact_frame=3)
    assert metrics["balance_at_contact"].value is None
    assert metrics["balance_margin_px"].value is None


# --- contact_point_class ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("wrist_x", "expected"),
    [
        (1000.0, "late"),  # d = -40 (boundary, inclusive)
        (999.9, "cramped"),  # d = -39.9
        (970.0, "cramped"),  # d = -10 (boundary, inclusive)
        (969.9, "under_eyes"),  # d = -9.9
        (925.0, "under_eyes"),  # d = 35 (boundary, inclusive)
        (924.9, "too_far_in_front"),  # d = 35.1
    ],
)
def test_contact_point_rule_table_boundaries(wrist_x: float, expected: str) -> None:
    # d = (wrist_mid_x - nose_x) * -1 = 960 - wrist_x with both wrists at wrist_x.
    overrides = {"left_wrist": (wrist_x, 610.0), "right_wrist": (wrist_x, 610.0)}
    frames = [_frame(0), _frame(1), _frame(2), _frame(3, overrides)]
    value = compute_contact(_track(frames), contact_frame=3)["contact_point_class"]
    assert value.value == expected
    # US-E3 AC: the wrist midpoint stands in for ball contact until Epic F tracking.
    assert value.proxy is True


def test_contact_point_low_wrist_visibility_is_null() -> None:
    # Both wrists low: also exercises the first-reason-wins path in the gate.
    frames = [_frame(0), _frame(1), _frame(2)]
    frames.append(_frame(3, vis_overrides={"left_wrist": 0.1, "right_wrist": 0.1}))
    value = compute_contact(_track(frames), contact_frame=3)["contact_point_class"]
    assert value.value is None
    assert value.reason is not None and "left_wrist" in value.reason
    assert value.proxy is True


# --- bat_path_class (PROXY until Epic F) -------------------------------------------------


def _swing_track(end_mid: tuple[float, float], n_frames: int = 6) -> PoseTrack:
    """Wrist midpoint travels from (1000, 500) at frame 1 to ``end_mid`` at frame 5."""
    start = {"left_wrist": (1000.0, 500.0), "right_wrist": (1000.0, 500.0)}
    end = {"left_wrist": end_mid, "right_wrist": end_mid}
    frames = [_frame(0), _frame(1, start), _frame(2), _frame(3), _frame(4), _frame(5, end)]
    return _track(frames[:n_frames])


_BOUNDARY_CONFIG = ContactConfig(
    bat_path_closed_min_deg=-45.0,
    bat_path_straight_min_deg=0.0,
    bat_path_straight_max_deg=45.0,
    bat_path_across_max_deg=90.0,
)


@pytest.mark.parametrize(
    ("end_mid", "expected"),
    [
        ((960.0, 540.0), "straight"),  # theta = 45 (inclusive straight max)
        ((1000.0, 540.0), "straight"),  # theta = 0 (inclusive straight min)
        ((960.0, 500.0), "across"),  # theta = 90 (inclusive across max)
        ((960.0, 460.0), "open"),  # theta = 135 (above across max)
        ((1040.0, 540.0), "closed"),  # theta = -45 (inclusive closed min)
        ((1040.0, 500.0), "open"),  # theta = -90 (below closed min)
    ],
)
def test_bat_path_rule_table_boundaries(end_mid: tuple[float, float], expected: str) -> None:
    value = compute_contact(_swing_track(end_mid), contact_frame=5, config=_BOUNDARY_CONFIG)[
        "bat_path_class"
    ]
    assert value.value == expected
    assert value.proxy is True  # US-E3 AC: pose-only proxy until Epic F bat tracking


def test_bat_path_default_config_classifies_diagonal_downswing_as_straight() -> None:
    metrics = compute_contact(_swing_track((960.0, 540.0)), contact_frame=5)
    assert metrics["bat_path_class"].value == "straight"  # theta = 45 in [25, 65]
    assert math.isclose(metrics["bat_path_class"].confidence, 0.95)


def test_bat_path_default_config_flat_swing_is_across() -> None:
    metrics = compute_contact(_swing_track((960.0, 500.0)), contact_frame=5)
    assert metrics["bat_path_class"].value == "across"  # theta = 90 in (65, 110]


def test_bat_path_window_start_respects_release_frame() -> None:
    # window 5 ending at contact 3 would start at -1; release_frame=1 clamps it.
    start = {"left_wrist": (1000.0, 500.0), "right_wrist": (1000.0, 500.0)}
    end = {"left_wrist": (960.0, 540.0), "right_wrist": (960.0, 540.0)}
    frames = [_frame(0), _frame(1, start), _frame(2), _frame(3, end)]
    value = compute_contact(_track(frames), contact_frame=3, release_frame=1)["bat_path_class"]
    assert value.value == "straight"


def test_bat_path_too_short_travel_is_null_and_still_proxy() -> None:
    value = compute_contact(_stance_track(6), contact_frame=5)["bat_path_class"]
    assert value.value is None
    assert value.reason is not None and "wrist travel" in value.reason
    assert value.proxy is True


def test_bat_path_low_visibility_at_window_start_is_null() -> None:
    frames = [_frame(0), _frame(1, vis_overrides={"right_wrist": 0.2})]
    frames += [_frame(2), _frame(3), _frame(4), _frame(5)]
    value = compute_contact(_track(frames), contact_frame=5)["bat_path_class"]
    assert value.value is None
    assert value.reason is not None and "right_wrist" in value.reason
    assert value.proxy is True


# --- end to end over the deterministic fake provider -------------------------------------


def test_fake_provider_track_produces_the_full_metric_set() -> None:
    track = FakePoseProvider().extract([object()] * 24, fps=120.0)
    metrics = compute_contact(track, contact_frame=23)
    assert tuple(metrics) == CONTACT_METRIC_KEYS
    payloads = {name: value.to_payload() for name, value in metrics.items()}
    for payload in payloads.values():
        assert {"value", "unit", "confidence"} <= set(payload)
        if payload["value"] is None:
            assert payload["reason"]
    # Head sway of 4 px against a 60 px shoulder width stays highly stable.
    score = metrics["head_stability_score"].value
    assert isinstance(score, float) and 0.9 <= score <= 1.0
    assert metrics["contact_point_class"].value == "under_eyes"
    assert metrics["contact_point_class"].proxy is True
    assert metrics["bat_path_class"].proxy is True
    # No px-to-cm scale given: cm is null-with-reason, px fallback is populated.
    assert metrics["front_foot_direction_cm"].value is None
    assert isinstance(metrics["front_foot_direction_px"].value, float)
