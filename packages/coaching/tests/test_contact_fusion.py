"""US-F5 fusion unit tests: modality signals, degradation, calibration harness."""

import itertools
import math
from typing import Any

import pytest
from cricai_coaching.contact_fusion import (
    AUDIO_SCORES,
    BAT_SCORES_FAR,
    BAT_SCORES_NEAR,
    BAT_SCORES_OVERLAP,
    DEFAULT_FUSION_CONFIG,
    FUSION_VERSION,
    TRACK_SCORES_EDGE,
    TRACK_SCORES_FAINT,
    TRACK_SCORES_MIDDLED,
    TRACK_SCORES_MISS,
    TRACK_SCORES_NO_DEVIATION,
    FusionConfig,
    FusionError,
    ModalitySignal,
    audio_signal,
    bat_path_from_detections,
    bat_signal,
    confusion_gate,
    fuse_contact,
    reliability_diagram,
    track_signal,
)
from cricai_data.enums import Contact, LabelClass
from cricai_vision.audio_onset import OnsetKind, RefinedContact
from cricai_vision.detect import Detection

# --- synthetic data builders --------------------------------------------------


def pt(ts_ms: float, px_x: float, px_y: float) -> dict[str, Any]:
    return {
        "frame_no": round(ts_ms / 10),
        "ts_ms": ts_ms,
        "px_x": px_x,
        "px_y": px_y,
        "score": 0.9,
        "bridged": False,
    }


def bridged_pt(ts_ms: float, px_x: float, px_y: float) -> dict[str, Any]:
    """A tracker gap-fill: fabricated chord point (score 0, bridged=true, US-F3)."""
    return pt(ts_ms, px_x, px_y) | {"score": 0.0, "bridged": True}


def seg(
    kind: str = "post_contact",
    start_ms: float = 100.0,
    end_ms: float = 200.0,
    confidence: float = 0.9,
) -> dict[str, Any]:
    return {"kind": kind, "start_ms": start_ms, "end_ms": end_ms, "confidence": confidence}


def make_payload(
    points: list[Any] | None = None,
    segments: list[Any] | None = None,
    flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "points": [] if points is None else points,
        "segments": [] if segments is None else segments,
        "flags": {"identity_risk": False, "long_gap": False} if flags is None else flags,
    }


def flight_points(post_dx: float, post_dy: float) -> list[dict[str, Any]]:
    """Pre-contact velocity (100, 0); post velocity (post_dx, post_dy).

    The deviation at the bat plane (contact at t=100 ms) is exactly
    |atan2(post_dy, post_dx)| degrees.
    """
    pre = [pt(0.0, 50.0, 50.0), pt(50.0, 100.0, 50.0), pt(100.0, 150.0, 50.0)]
    post = [
        pt(110.0, 150.0 + post_dx * 0.5, 50.0 + post_dy * 0.5),
        pt(130.0, 150.0 + post_dx, 50.0 + post_dy),
    ]
    return pre + post


def middled_payload() -> dict[str, Any]:
    return make_payload(points=flight_points(0.0, -100.0), segments=[seg()])  # 90 deg


def det(
    label: LabelClass,
    cx: float,
    cy: float,
    *,
    ts_ms: float = 100.0,
    frame_no: int | None = None,
    w: float = 0.05,
    h: float = 0.05,
    score: float = 0.9,
) -> Detection:
    return Detection(
        frame_no=round(ts_ms / 33.0) if frame_no is None else frame_no,
        ts_ms=ts_ms,
        label=label,
        cx=cx,
        cy=cy,
        w=w,
        h=h,
        score=score,
    )


def overlap_detections() -> list[Detection]:
    return [
        det(LabelClass.BALL, 0.5, 0.5, w=0.02, h=0.02),
        det(LabelClass.BAT, 0.5, 0.5, w=0.05, h=0.12),
    ]


# --- FusionConfig validation ---------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"track_weight": 0.0},
        {"audio_weight": -0.1},
        {"bat_weight": 0.0},
        {"deviation_points": 1},
        {"edge_min_deviation_deg": 0.0},
        {"edge_min_deviation_deg": 40.0},  # >= middled_min default 35
        {"no_post_contact_confidence": 1.5},
        {"flagged_track_penalty": -0.1},
        {"contact_frame_tolerance_ms": -1.0},
        {"bat_near_gap": -0.01},
        {"swing_window_ms": 0.0},
        {"bat_path_min_travel": 0.0},
        {"min_bat_frames": 1},
        {"bat_path_straight_min_deg": 60.0},  # >= straight_max default 60
        {"toward_ball_sign": 0.5},
    ],
)
def test_fusion_config_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(FusionError):
        FusionConfig(**kwargs)


# --- ModalitySignal contract ----------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "confidence"),
    [
        ({Contact.MIDDLE: 0.5, Contact.EDGE: 0.5}, 0.9),  # missing MISS
        ({Contact.MIDDLE: 1.2, Contact.EDGE: -0.1, Contact.MISS: -0.1}, 0.9),  # negative
        ({Contact.MIDDLE: 0.5, Contact.EDGE: 0.4, Contact.MISS: 0.3}, 0.9),  # sums to 1.2
        ({Contact.MIDDLE: 0.5, Contact.EDGE: 0.3, Contact.MISS: 0.2}, 1.1),  # bad conf
    ],
)
def test_modality_signal_rejects_malformed_votes(
    scores: dict[Contact, float], confidence: float
) -> None:
    with pytest.raises(FusionError):
        ModalitySignal(scores=scores, confidence=confidence, note="bad")


# --- track modality --------------------------------------------------------------


def test_track_signal_middled_edge_faint_by_deviation() -> None:
    middled = track_signal(middled_payload(), contact_ms=100.0)
    assert middled.scores == TRACK_SCORES_MIDDLED
    assert middled.confidence == 0.9  # the post-contact segment's confidence
    assert "clean-hit range" in middled.note

    edge = track_signal(
        make_payload(points=flight_points(100.0, -30.0), segments=[seg()]), contact_ms=100.0
    )
    assert edge.scores == TRACK_SCORES_EDGE  # 16.7 deg deviation
    assert "deflection range" in edge.note

    faint = track_signal(
        make_payload(points=flight_points(100.0, -5.0), segments=[seg()]), contact_ms=100.0
    )
    assert faint.scores == TRACK_SCORES_FAINT  # 2.9 deg deviation
    assert "below edge range" in faint.note


def test_track_signal_without_post_contact_segment_leans_miss() -> None:
    signal = track_signal(
        make_payload(points=flight_points(100.0, 0.0), segments=[seg(kind="pre_bounce")]),
        contact_ms=100.0,
    )
    assert signal.scores == TRACK_SCORES_MISS
    assert signal.confidence == DEFAULT_FUSION_CONFIG.no_post_contact_confidence
    assert "no post-contact segment" in signal.note


@pytest.mark.parametrize("flag", ["identity_risk", "long_gap"])
def test_track_flags_penalize_confidence(flag: str) -> None:
    flags = {"identity_risk": False, "long_gap": False, flag: True}
    missing = track_signal(make_payload(flags=flags), contact_ms=None)
    assert missing.confidence == round(0.7 * 0.5, 4)  # absence confidence * penalty

    hit = track_signal(
        make_payload(points=flight_points(0.0, -100.0), segments=[seg()], flags=flags),
        contact_ms=100.0,
    )
    assert hit.confidence == round(0.9 * 0.5, 4)  # segment confidence * penalty


def test_track_signal_uses_segment_start_when_contact_ms_unknown() -> None:
    signal = track_signal(middled_payload(), contact_ms=None)
    assert signal.scores == TRACK_SCORES_MIDDLED  # bat plane = segment start (100 ms)


def test_track_signal_segment_confidence_is_clamped() -> None:
    payload = make_payload(points=flight_points(0.0, -100.0), segments=[seg(confidence=1.5)])
    assert track_signal(payload, contact_ms=100.0).confidence == 1.0


@pytest.mark.parametrize(
    "points",
    [
        [pt(110.0, 150.0, 40.0), pt(130.0, 150.0, 20.0)],  # no pre-contact points
        [pt(0.0, 50.0, 50.0), pt(100.0, 150.0, 50.0), pt(110.0, 160.0, 40.0)],  # 1 post point
        # degenerate zero-length pre vector (ball pinned at one pixel):
        [pt(0.0, 50.0, 50.0), pt(100.0, 50.0, 50.0), pt(110.0, 60.0, 40.0), pt(130.0, 80.0, 20.0)],
        # degenerate zero-length post vector:
        [pt(0.0, 50.0, 50.0), pt(100.0, 150.0, 50.0), pt(110.0, 160.0, 40.0), pt(130.0, 160.0, 40)],
    ],
)
def test_track_signal_without_computable_deviation_uses_presence_only(
    points: list[dict[str, Any]],
) -> None:
    signal = track_signal(make_payload(points=points, segments=[seg()]), contact_ms=100.0)
    assert signal.scores == TRACK_SCORES_NO_DEVIATION
    assert "deviation not computable" in signal.note


def occluded_contact_points(*, real_pre: int = 3, real_post: int = 3) -> list[dict[str, Any]]:
    """Real +x flight, a bridged chord spanning the 90-deg turn at contact (105 ms).

    The bridged points are the tracker's constant-velocity fill on the straight
    chord between the last real pre-gap detection (80 ms) and the first real
    post-gap detection (140 ms) — exactly what US-F3's ``_bridge`` fabricates
    when the bat occludes the ball around contact.
    """
    pre = [pt(60.0 + 10.0 * i, 300.0 + 50.0 * i, 50.0) for i in range(3)][-real_pre:]
    start, end = (400.0, 50.0), (525.0, -125.0)
    chord = [
        bridged_pt(
            80.0 + 10.0 * step,
            start[0] + (end[0] - start[0]) * step / 6,
            start[1] + (end[1] - start[1]) * step / 6,
        )
        for step in range(1, 6)
    ]
    post = [pt(140.0 + 10.0 * i, 525.0, -125.0 - 50.0 * i) for i in range(3)][:real_post]
    return pre + chord + post


def test_track_signal_ignores_bridged_points_when_measuring_deviation() -> None:
    """Chord-aligned bridged fills must not read as a ~0 deg deviation (faint/miss)
    for a middled ball whose contact instant falls inside the bridged gap."""
    payload = make_payload(
        points=occluded_contact_points(), segments=[seg(start_ms=105.0, end_ms=300.0)]
    )
    signal = track_signal(payload, contact_ms=105.0)
    assert signal.scores == TRACK_SCORES_MIDDLED  # the real points show the 90-deg turn
    assert "clean-hit range" in signal.note


def test_track_signal_degrades_when_only_bridged_points_flank_contact() -> None:
    """Too few real points around the bat plane -> the NO_DEVIATION degrade path,
    never a confident deviation measured on fabricated chord points."""
    payload = make_payload(
        points=occluded_contact_points(real_pre=1),
        segments=[seg(start_ms=105.0, end_ms=300.0)],
    )
    signal = track_signal(payload, contact_ms=105.0)
    assert signal.scores == TRACK_SCORES_NO_DEVIATION
    assert "deviation not computable" in signal.note


def bounce_occluded_edge_points(*, real_ascent: int) -> list[dict[str, Any]]:
    """Ball played off the pitch with the bat occluding it through contact.

    Steep real descent (10-40 ms), bounce at ~45 ms, ``real_ascent`` real rising
    points, then a bridged chord (the US-F3 ``_bridge`` fill, <= 5 frames)
    spanning the contact instant (95 ms), then a real faint-edge exit only
    ~2 deg off the incoming ascent direction. Any window blending the descent
    into the incoming velocity reads the bounce itself as a fake >= 35 deg
    (clean-hit) deviation.
    """
    descent = [pt(10.0 + 10.0 * i, 100.0 + 20.0 * i, 10.0 + 60.0 * i) for i in range(4)]
    ascent = [pt(50.0, 180.0, 215.0), pt(60.0, 200.0, 205.0)][:real_ascent]
    last = ascent[-1]
    gap_frames = round((100.0 - last["ts_ms"]) / 10.0)
    chord = [
        bridged_pt(
            last["ts_ms"] + 10.0 * step,
            last["px_x"] + (280.3 - last["px_x"]) * step / gap_frames,
            last["px_y"] + (165.3 - last["px_y"]) * step / gap_frames,
        )
        for step in range(1, gap_frames)
    ]
    exit_points = [pt(100.0 + 10.0 * i, 280.3 + 20.6 * i, 165.3 - 9.4 * i) for i in range(3)]
    return descent + ascent + chord + exit_points


def bounce_occluded_edge_segments() -> list[dict[str, Any]]:
    return [
        seg(kind="pre_bounce", start_ms=10.0, end_ms=45.0),
        seg(kind="post_bounce", start_ms=45.0, end_ms=95.0),
        seg(kind="post_contact", start_ms=95.0, end_ms=300.0),
    ]


def test_track_deviation_window_never_reaches_across_the_bounce() -> None:
    """With ONE real post-bounce point the incoming window must not backfill
    from pre-bounce descent points (that measures the bounce, turning a faint
    edge into a confident MIDDLED vote): nothing to measure -> NO_DEVIATION."""
    payload = make_payload(
        points=bounce_occluded_edge_points(real_ascent=1),
        segments=bounce_occluded_edge_segments(),
    )
    signal = track_signal(payload, contact_ms=95.0)
    assert signal.scores != TRACK_SCORES_MIDDLED
    assert signal.scores == TRACK_SCORES_NO_DEVIATION
    assert "deviation not computable" in signal.note


def test_track_deviation_measured_inside_the_post_bounce_segment_stays_faint() -> None:
    """With TWO real post-bounce points the bounded window measures the true
    ~2 deg faint-edge deviation instead of the ~45 deg cross-bounce turn."""
    payload = make_payload(
        points=bounce_occluded_edge_points(real_ascent=2),
        segments=bounce_occluded_edge_segments(),
    )
    signal = track_signal(payload, contact_ms=95.0)
    assert signal.scores == TRACK_SCORES_FAINT
    assert "below edge range" in signal.note


def test_track_deviation_post_window_stays_inside_the_post_contact_segment() -> None:
    """A stray real point beyond the post_contact segment's end (a late
    mis-association) never joins the outgoing window: one in-segment point is
    too few to measure -> NO_DEVIATION."""
    points = [
        pt(0.0, 50.0, 50.0),
        pt(50.0, 100.0, 50.0),
        pt(100.0, 150.0, 50.0),
        pt(110.0, 160.0, 40.0),  # inside post_contact [100, 125]
        pt(400.0, 300.0, 200.0),  # beyond the segment end: excluded
    ]
    payload = make_payload(points=points, segments=[seg(start_ms=100.0, end_ms=125.0)])
    signal = track_signal(payload, contact_ms=100.0)
    assert signal.scores == TRACK_SCORES_NO_DEVIATION
    assert "deviation not computable" in signal.note


def test_incoming_window_bound_is_the_latest_flight_segment_at_or_before_the_plane() -> None:
    """Segment order and junk entries never change the chosen incoming bound:
    the latest non-post_contact segment starting at/before the bat plane wins
    (never an earlier phase, never one starting after the plane)."""
    segments: list[Any] = [
        5,  # not an object: skipped
        seg(kind="post_bounce", start_ms=45.0, end_ms=95.0),  # the bound
        seg(kind="pre_bounce", start_ms=10.0, end_ms=45.0),  # earlier phase: not chosen
        seg(kind="post_bounce", start_ms=400.0, end_ms=500.0),  # after the plane: skipped
        seg(kind="post_contact", start_ms=95.0, end_ms=300.0),
    ]
    payload = make_payload(points=bounce_occluded_edge_points(real_ascent=2), segments=segments)
    assert track_signal(payload, contact_ms=95.0).scores == TRACK_SCORES_FAINT


def test_track_signal_sorts_unordered_points() -> None:
    points = list(reversed(flight_points(0.0, -100.0)))
    signal = track_signal(make_payload(points=points, segments=[seg()]), contact_ms=100.0)
    assert signal.scores == TRACK_SCORES_MIDDLED


@pytest.mark.parametrize(
    "payload",
    [
        {},  # everything missing
        {"points": {}, "segments": [], "flags": {}},  # points not a list
        {"points": [], "segments": {}, "flags": {}},  # segments not a list
        {"points": [], "segments": [], "flags": []},  # flags not an object
        make_payload(points=[42], segments=[seg()]),  # point not an object
        make_payload(points=[{"ts_ms": 1.0, "px_x": 2.0}], segments=[seg()]),  # missing px_y
        make_payload(points=[pt(0.0, 1.0, 1.0) | {"px_x": "wide"}], segments=[seg()]),
        make_payload(segments=[{"kind": "post_contact", "confidence": 0.9}]),  # no start_ms
        make_payload(segments=[{"kind": "post_contact", "start_ms": "x", "confidence": 0.9}]),
        make_payload(segments=[{"kind": "post_contact", "start_ms": None, "confidence": 0.9}]),
        make_payload(segments=[{"kind": "post_contact", "start_ms": 1.0, "confidence": 0.9}]),
        make_payload(segments=[{"kind": "post_contact", "start_ms": 1.0, "end_ms": 2.0}]),
        make_payload(  # malformed flight segment (no end_ms) read for the window bound
            points=flight_points(0.0, -100.0),
            segments=[{"kind": "post_bounce", "start_ms": 0.0}, seg()],
        ),
    ],
)
def test_track_signal_raises_on_malformed_payload(payload: dict[str, Any]) -> None:
    with pytest.raises(FusionError):
        track_signal(payload, contact_ms=100.0)


def test_track_signal_skips_non_object_segments() -> None:
    payload = make_payload(
        points=flight_points(0.0, -100.0),
        segments=[
            5,
            seg(kind="pre_bounce", start_ms=0.0, end_ms=45.0),
            seg(kind="post_bounce", start_ms=45.0, end_ms=100.0),
            seg(),
        ],
    )
    assert track_signal(payload, contact_ms=100.0).scores == TRACK_SCORES_MIDDLED


# --- audio modality ---------------------------------------------------------------


@pytest.mark.parametrize("kind", list(OnsetKind))
def test_audio_signal_maps_each_onset_class(kind: OnsetKind) -> None:
    signal = audio_signal(RefinedContact(contact_ms=105.0, confidence=0.8, kind=kind))
    assert signal.scores == AUDIO_SCORES[kind]
    assert signal.confidence == 0.8
    assert kind.value in signal.note


def test_audio_signal_clamps_out_of_range_confidence() -> None:
    signal = audio_signal(RefinedContact(contact_ms=105.0, confidence=1.5, kind=OnsetKind.THUD))
    assert signal.confidence == 1.0


# --- bat modality ------------------------------------------------------------------


def test_bat_signal_overlap_near_far_tables() -> None:
    overlap = bat_signal(overlap_detections(), contact_ts_ms=100.0)
    assert overlap is not None
    assert overlap.scores == BAT_SCORES_OVERLAP
    assert overlap.confidence == 0.9

    # ball half-widths 0.01 + bat 0.025 = 0.035; cx gap 0.065 -> box gap 0.03 (near)
    near = bat_signal(
        [det(LabelClass.BALL, 0.5, 0.5, w=0.02, h=0.02), det(LabelClass.BAT, 0.565, 0.5)],
        contact_ts_ms=100.0,
    )
    assert near is not None
    assert near.scores == BAT_SCORES_NEAR

    far = bat_signal(
        [det(LabelClass.BALL, 0.5, 0.5, w=0.02, h=0.02), det(LabelClass.BAT, 0.9, 0.5)],
        contact_ts_ms=100.0,
    )
    assert far is not None
    assert far.scores == BAT_SCORES_FAR


def test_bat_signal_missing_when_no_pair_at_contact_frame() -> None:
    assert bat_signal([], contact_ts_ms=100.0) is None
    assert bat_signal([det(LabelClass.BAT, 0.5, 0.5)], contact_ts_ms=100.0) is None  # no ball
    assert (
        bat_signal([det(LabelClass.BALL, 0.5, 0.5, w=0.02, h=0.02)], contact_ts_ms=100.0) is None
    )  # no bat
    # detections outside the contact-frame tolerance window do not count:
    assert bat_signal(overlap_detections(), contact_ts_ms=200.0) is None


def test_bat_signal_missing_when_track_has_no_real_point_at_contact() -> None:
    """A static decoy in the net must not vote far/miss while the bat occludes
    the real ball: without a real tracked point at contact the modality is
    missing (None), never an impostor pairing."""
    detections = [
        det(LabelClass.BALL, 0.15, 0.95, w=0.02, h=0.02, score=0.8),  # decoy in the net
        det(LabelClass.BAT, 0.85, 0.65),
    ]
    occluded = [pt(0.0, 50.0, 50.0), bridged_pt(100.0, 150.0, 50.0), pt(200.0, 250.0, 50.0)]
    assert bat_signal(detections, contact_ts_ms=100.0, track_points=occluded) is None
    assert bat_signal(detections, contact_ts_ms=100.0, track_points=[]) is None


def test_bat_signal_votes_when_a_real_tracked_point_corroborates_contact() -> None:
    """Malformed/bridged/out-of-window points never corroborate; one real point
    within the tolerance does, and the vote is unchanged from the no-hint path."""
    points: list[Any] = [
        42,  # not an object
        bridged_pt(100.0, 150.0, 50.0),  # fabricated fill, never corroborates
        {"px_x": 1.0, "px_y": 2.0},  # no ts_ms
        pt(110.0, 155.0, 50.0) | {"ts_ms": "soon"},  # non-numeric ts_ms
        pt(300.0, 350.0, 50.0),  # real but outside the tolerance window
        pt(110.0, 155.0, 50.0),  # real, within 25 ms of contact -> corroborates
    ]
    signal = bat_signal(overlap_detections(), contact_ts_ms=100.0, track_points=points)
    assert signal is not None
    assert signal.scores == BAT_SCORES_OVERLAP
    assert signal.confidence == 0.9


def test_bat_signal_picks_the_closest_pair_and_weaker_score() -> None:
    detections = [
        det(LabelClass.BALL, 0.5, 0.5, w=0.02, h=0.02, score=0.9),
        det(LabelClass.BAT, 0.9, 0.5, score=0.95),  # far bat
        det(LabelClass.BAT, 0.5, 0.5, score=0.6),  # overlapping bat (wins)
    ]
    signal = bat_signal(detections, contact_ts_ms=100.0)
    assert signal is not None
    assert signal.scores == BAT_SCORES_OVERLAP
    assert signal.confidence == 0.6  # min(ball 0.9, chosen bat 0.6)


# --- bat path -----------------------------------------------------------------------


def bat_at(ts_ms: float, cx: float, cy: float, *, score: float = 0.9) -> Detection:
    return det(LabelClass.BAT, cx, cy, ts_ms=ts_ms, frame_no=round(ts_ms / 33.0), score=score)


def test_bat_path_rule_table_classes() -> None:
    straight = bat_path_from_detections(
        [bat_at(0.0, 0.6, 0.4), bat_at(100.0, 0.5, 0.55)], contact_ts_ms=100.0
    )
    assert straight.value == "straight"  # theta = atan2(0.1, 0.15) = 33.7 deg
    assert straight.confidence == 0.9
    assert straight.proxy is False
    assert straight.source == f"{FUSION_VERSION}(bat:2f)"

    across = bat_path_from_detections(
        [bat_at(0.0, 0.8, 0.5), bat_at(100.0, 0.5, 0.55)], contact_ts_ms=100.0
    )
    assert across.value == "across"  # theta = atan2(0.3, 0.05) = 80.5 deg

    inside_out = bat_path_from_detections(
        [bat_at(0.0, 0.5, 0.4), bat_at(100.0, 0.55, 0.55)], contact_ts_ms=100.0
    )
    assert inside_out.value == "inside_out"  # theta = atan2(-0.05, 0.15) = -18.4 deg


def test_bat_path_null_with_reason_on_too_few_frames_or_travel() -> None:
    none_at_all = bat_path_from_detections([], contact_ts_ms=100.0)
    assert none_at_all.value is None
    assert none_at_all.reason is not None
    assert "0 bat detection frames" in none_at_all.reason

    single = bat_path_from_detections([bat_at(100.0, 0.5, 0.5)], contact_ts_ms=100.0)
    assert single.value is None

    still = bat_path_from_detections(
        [bat_at(0.0, 0.5, 0.5), bat_at(100.0, 0.5, 0.5)], contact_ts_ms=100.0
    )
    assert still.value is None
    assert still.reason is not None
    assert "cannot classify swing direction" in still.reason


def test_bat_path_filters_window_labels_and_keeps_best_box_per_frame() -> None:
    detections = [
        bat_at(-300.0, 0.9, 0.1),  # before the swing window: ignored
        bat_at(150.0, 0.1, 0.9),  # after contact: ignored
        det(LabelClass.BALL, 0.3, 0.3, ts_ms=0.0, frame_no=0),  # not a bat: ignored
        bat_at(0.0, 0.6, 0.4, score=0.5),
        bat_at(0.0, 0.62, 0.38, score=0.8),  # same frame, higher score: replaces
        bat_at(0.0, 0.9, 0.9, score=0.2),  # same frame, lower score: kept out
        bat_at(100.0, 0.5, 0.55, score=0.6),
    ]
    path = bat_path_from_detections(detections, contact_ts_ms=100.0)
    assert path.value == "straight"  # (0.62, 0.38) -> (0.5, 0.55)
    assert path.confidence == round((0.8 + 0.6) / 2, 4)


# --- fusion -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("with_track", "with_audio", "with_bat"),
    list(itertools.product([True, False], repeat=3)),
)
def test_fusion_handles_every_modality_combination(
    with_track: bool, with_audio: bool, with_bat: bool
) -> None:
    """US-F5 AC: fusion with any modality subset (all 8 combinations) is exercised."""
    track = track_signal(middled_payload(), contact_ms=100.0) if with_track else None
    audio = (
        RefinedContact(contact_ms=105.0, confidence=0.8, kind=OnsetKind.BAT_CRACK)
        if with_audio
        else None
    )
    bat = bat_signal(overlap_detections(), contact_ts_ms=100.0) if with_bat else None

    result = fuse_contact(track=track, audio=audio, bat=bat)

    expected_used = tuple(
        name
        for name, present in (("track", with_track), ("audio", with_audio), ("bat", with_bat))
        if present
    )
    assert result.used == expected_used
    if not expected_used:
        assert result.contact_quality.value is None
        assert result.contact_quality.confidence == 0.0
        assert result.contact_quality.reason is not None
        assert "all fusion modalities missing" in result.contact_quality.reason
        assert result.combined == dict.fromkeys(Contact, 0.0)
    else:
        assert result.contact_quality.value == "middle"  # every modality leans middle here
        assert result.contact_quality.source == f"{FUSION_VERSION}({'+'.join(expected_used)})"
        assert 0.0 < result.contact_quality.confidence <= 1.0
        assert result.contact_quality.proxy is False
        assert math.isclose(sum(result.combined.values()), 1.0)


def test_fusion_confidence_drops_as_modalities_go_missing() -> None:
    track = track_signal(middled_payload(), contact_ms=100.0)
    audio = RefinedContact(contact_ms=105.0, confidence=0.8, kind=OnsetKind.BAT_CRACK)
    bat = bat_signal(overlap_detections(), contact_ts_ms=100.0)

    full = fuse_contact(track=track, audio=audio, bat=bat)
    track_only = fuse_contact(track=track, audio=None, bat=None)

    assert full.contact_quality.confidence > track_only.contact_quality.confidence
    # exact degradation math: track-only = combined 0.75 * coverage 0.5 * confidence 0.9
    assert track_only.contact_quality.confidence == round(0.75 * 0.5 * 0.9, 4)


def test_fusion_weight_renormalization_exact_math() -> None:
    track = ModalitySignal(
        scores={Contact.MIDDLE: 1.0, Contact.EDGE: 0.0, Contact.MISS: 0.0},
        confidence=1.0,
        note="track says middle",
    )
    bat = ModalitySignal(
        scores={Contact.MIDDLE: 0.0, Contact.EDGE: 1.0, Contact.MISS: 0.0},
        confidence=1.0,
        note="bat says edge",
    )

    result = fuse_contact(track=track, audio=None, bat=bat)

    # weights 0.5/0.2 renormalize over 0.7; coverage = 0.7; confidences 1.0.
    assert math.isclose(result.combined[Contact.MIDDLE], 5 / 7)
    assert math.isclose(result.combined[Contact.EDGE], 2 / 7)
    assert result.contact_quality.value == "middle"
    assert result.contact_quality.confidence == round((5 / 7) * 0.7, 4)


def test_fusion_tie_breaks_toward_edge() -> None:
    tied = ModalitySignal(
        scores={Contact.MIDDLE: 0.4, Contact.EDGE: 0.4, Contact.MISS: 0.2},
        confidence=1.0,
        note="dead heat",
    )
    result = fuse_contact(track=tied, audio=None, bat=None)
    assert result.contact_quality.value == "edge"  # edge recall matters most (US-F5)


# --- reliability diagram --------------------------------------------------------------


def test_reliability_diagram_bins_and_calibration_gap() -> None:
    pairs = [(0.95, True)] * 4 + [(0.55, True), (0.55, True), (0.55, False), (0.55, False)]

    report = reliability_diagram(pairs, bins=10)

    assert report.total == 8
    top = report.bins[9]
    assert (top.count, top.mean_confidence, top.observed_accuracy) == (4, 0.95, 1.0)
    mid = report.bins[5]
    assert (mid.count, mid.mean_confidence, mid.observed_accuracy) == (4, 0.55, 0.5)
    empty = report.bins[0]
    assert (empty.count, empty.mean_confidence, empty.observed_accuracy) == (0, None, None)
    assert (empty.lo, empty.hi) == (0.0, 0.1)
    # gap = 0.5 * |0.95 - 1.0| + 0.5 * |0.55 - 0.5| = 0.05
    assert report.calibration_gap == 0.05


def test_reliability_diagram_puts_full_confidence_in_the_top_bin() -> None:
    report = reliability_diagram([(1.0, True), (0.0, False)], bins=4)
    assert report.bins[3].count == 1  # confidence 1.0 lands in the last bin, not out of range
    assert report.bins[0].count == 1
    # both bins are perfectly calibrated (conf 1.0/acc 1.0 and conf 0.0/acc 0.0):
    assert report.calibration_gap == 0.0


def test_reliability_diagram_empty_input_reports_zero_evidence() -> None:
    report = reliability_diagram([], bins=3)
    assert report.total == 0
    assert report.calibration_gap == 0.0
    assert all(entry.count == 0 for entry in report.bins)


@pytest.mark.parametrize(("pairs", "bins"), [([(0.5, True)], 0), ([(1.2, True)], 10)])
def test_reliability_diagram_rejects_bad_inputs(pairs: list[tuple[float, bool]], bins: int) -> None:
    with pytest.raises(FusionError):
        reliability_diagram(pairs, bins=bins)


# --- confusion gate --------------------------------------------------------------------


def matrix(edge_to_middle: int, edge_to_edge: int) -> dict[Contact, dict[Contact, int]]:
    return {
        Contact.MIDDLE: {Contact.MIDDLE: 10, Contact.EDGE: 1, Contact.MISS: 0},
        Contact.EDGE: {Contact.MIDDLE: edge_to_middle, Contact.EDGE: edge_to_edge, Contact.MISS: 1},
        Contact.MISS: {Contact.MIDDLE: 0, Contact.EDGE: 0, Contact.MISS: 5},
    }


def test_confusion_gate_fails_when_edge_to_middled_rate_rises() -> None:
    result = confusion_gate(matrix(2, 7), matrix(3, 6))  # 0.2 -> 0.3
    assert result.passed is False
    assert (result.old_rate, result.new_rate) == (0.2, 0.3)
    assert "rose" in result.detail


def test_confusion_gate_passes_on_equal_or_lower_rate() -> None:
    assert confusion_gate(matrix(2, 7), matrix(2, 7)).passed is True
    improved = confusion_gate(matrix(2, 7), matrix(1, 8))
    assert improved.passed is True
    assert "held" in improved.detail


def test_confusion_gate_tolerance_allows_bounded_rise() -> None:
    assert confusion_gate(matrix(2, 7), matrix(3, 6), tolerance=0.15).passed is True


def test_confusion_gate_with_no_edge_samples_scores_zero() -> None:
    no_edges: dict[Contact, dict[Contact, int]] = {
        Contact.MIDDLE: {Contact.MIDDLE: 10, Contact.EDGE: 0, Contact.MISS: 0}
    }
    empty_edge_row: dict[Contact, dict[Contact, int]] = {Contact.EDGE: {}}
    result = confusion_gate(no_edges, matrix(1, 9))
    assert result.old_rate == 0.0
    assert result.passed is False  # 0.0 -> 0.1 is a rise
    assert confusion_gate(empty_edge_row, no_edges).passed is True
    # an edge row that never predicted middled has rate 0:
    no_middled = {Contact.EDGE: {Contact.EDGE: 9, Contact.MISS: 1}}
    assert confusion_gate(matrix(2, 7), no_middled).new_rate == 0.0


def test_confusion_gate_rejects_negative_counts_and_tolerance() -> None:
    with pytest.raises(FusionError):
        confusion_gate(matrix(-1, 7), matrix(1, 9))
    with pytest.raises(FusionError):
        confusion_gate(matrix(1, 7), matrix(1, 9), tolerance=-0.1)
