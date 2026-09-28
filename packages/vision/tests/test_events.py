"""US-D1 segmentation unit tests: synthetic motion-energy streams, no I/O."""

import math
from typing import Any

import pytest
from cricai_vision.events import (
    DETECTOR_VERSION,
    DetectorConfig,
    EventDetectionError,
    detect_events,
)

FPS = 100.0  # 1 frame == 10 ms keeps every expected timing exact

CFG = DetectorConfig(
    energy_threshold=0.3,
    min_gap_ms=500,
    min_event_duration_ms=100,
    max_event_duration_ms=2000,
    release_lag_ms=0,
    merge_window_ms=50,
)

#: 12 frames (120 ms) above threshold with an unambiguous peak at offset 5.
BUMP = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4]


def _stream(n_frames: int, bumps: dict[int, list[float]]) -> list[float]:
    energy = [0.0] * n_frames
    for start, profile in bumps.items():
        energy[start : start + len(profile)] = profile
    return energy


def test_detector_version_is_pinned() -> None:
    assert DETECTOR_VERSION == "cue-segmenter-1.0.0"


def test_machine_cadence_n_balls_n_events_with_correct_timing() -> None:
    starts = [100, 300, 500, 700, 900]
    events = detect_events(_stream(1100, dict.fromkeys(starts, BUMP)), fps=FPS, config=CFG)
    assert len(events) == len(starts)
    for event, start in zip(events, starts, strict=True):
        assert event.start_ms == start * 10
        assert event.end_ms == (start + len(BUMP)) * 10
        assert event.release_ms == (start + 5) * 10  # peak onset, zero lag
        assert event.contact_ms is None
        assert 0.0 <= event.confidence <= 1.0


def test_irregular_human_bowling_counts_and_order() -> None:
    starts = [37, 251, 833]
    events = detect_events(_stream(1000, dict.fromkeys(starts, BUMP)), fps=FPS, config=CFG)
    assert [event.start_ms for event in events] == [370, 2510, 8330]


def test_double_feed_bumps_merge_into_one_event() -> None:
    # Two full bumps 200 ms apart: over merge_window but under min_gap -> ONE ball.
    events = detect_events(_stream(200, {50: BUMP, 82: BUMP}), fps=FPS, config=CFG)
    assert len(events) == 1
    assert events[0].start_ms == 500
    assert events[0].end_ms == (82 + len(BUMP)) * 10


def test_double_feed_stream_duplicate_rate_below_2_percent() -> None:
    # 50 deliveries, each producing a double energy bump -> exactly 50 events (0% dupes).
    bumps: dict[int, list[float]] = {}
    for ball in range(50):
        base = 100 + ball * 150  # 1500 ms apart, well over min_gap
        bumps[base] = BUMP
        bumps[base + 32] = BUMP
    events = detect_events(_stream(100 + 50 * 150 + 60, bumps), fps=FPS, config=CFG)
    assert len(events) == 50


def test_sub_threshold_flicker_within_merge_window_unifies() -> None:
    # Two 6-frame halves (each under min duration) split by a 20 ms dip: the
    # merge_window pass unifies them BEFORE the duration filter -> one event.
    profile = [0.5] * 6 + [0.1, 0.1] + [0.9] + [0.5] * 5
    events = detect_events(_stream(120, {40: profile}), fps=FPS, config=CFG)
    assert len(events) == 1
    assert events[0].start_ms == 400
    assert events[0].end_ms == 540
    assert events[0].release_ms == 480  # peak is the 0.9 after the dip


def test_left_ball_no_onset_in_window_contact_none() -> None:
    events = detect_events(_stream(200, {50: BUMP}), fps=FPS, audio_onsets_ms=[5000], config=CFG)
    assert len(events) == 1
    assert events[0].contact_ms is None


def test_contact_nearest_onset_to_release() -> None:
    # Window 500..620 ms, release 550: post-release onsets at 560 and 610 -> 560 wins.
    events = detect_events(
        _stream(200, {50: BUMP}), fps=FPS, audio_onsets_ms=[560, 610, 9000], config=CFG
    )
    assert events[0].contact_ms == 560


def test_pre_release_onset_is_never_selected_as_contact() -> None:
    # Release 550: the feed clank at 540 is pre-release and excluded, so the
    # bat crack at 560 wins even though both are 10 ms from release.
    events = detect_events(
        _stream(200, {50: BUMP}), fps=FPS, audio_onsets_ms=[540, 560], config=CFG
    )
    assert events[0].contact_ms == 560


def test_onsets_at_or_before_release_yield_contact_none() -> None:
    # Contact is physically impossible at or before release (550): the candidate
    # keeps contact None instead of violating the ordering invariant.
    events = detect_events(
        _stream(200, {50: BUMP}), fps=FPS, audio_onsets_ms=[530, 550], config=CFG
    )
    assert events[0].contact_ms is None


def test_candidates_satisfy_ordering_invariant_with_scattered_onsets() -> None:
    # Onsets across the whole window: the emitted contact must satisfy
    # start <= release < contact <= end (the corrections API invariant).
    events = detect_events(
        _stream(200, {50: BUMP}), fps=FPS, audio_onsets_ms=[500, 545, 555, 600, 620], config=CFG
    )
    event = events[0]
    assert event.contact_ms is not None
    assert event.contact_ms == 555  # earliest strictly-post-release onset
    assert event.start_ms <= event.release_ms < event.contact_ms <= event.end_ms


def test_contact_accepts_onset_on_window_edge() -> None:
    events = detect_events(_stream(200, {50: BUMP}), fps=FPS, audio_onsets_ms=[620], config=CFG)
    assert events[0].contact_ms == 620


def test_empty_onsets_list_contact_none() -> None:
    events = detect_events(_stream(200, {50: BUMP}), fps=FPS, audio_onsets_ms=[], config=CFG)
    assert events[0].contact_ms is None


def test_confidence_clean_peak_above_noisy_peak() -> None:
    noisy = [0.32, 0.33, 0.35, 0.34, 0.33, 0.35, 0.34, 0.33, 0.32, 0.34, 0.33, 0.32]
    events = detect_events(_stream(300, {50: BUMP, 150: noisy}), fps=FPS, config=CFG)
    assert len(events) == 2
    assert events[0].confidence > events[1].confidence
    assert all(0.0 <= event.confidence <= 1.0 for event in events)


def test_overlong_event_gets_lower_confidence() -> None:
    plausible = detect_events(_stream(400, {20: [1.0] * 30}), fps=FPS, config=CFG)[0]
    overlong = detect_events(_stream(400, {20: [1.0] * 250}), fps=FPS, config=CFG)[0]
    assert overlong.end_ms - overlong.start_ms > CFG.max_event_duration_ms
    assert overlong.confidence < plausible.confidence
    assert 0.0 <= overlong.confidence <= 1.0


def test_blip_shorter_than_min_duration_dropped() -> None:
    assert detect_events(_stream(200, {50: [0.9] * 5}), fps=FPS, config=CFG) == []


def test_empty_energy_no_events() -> None:
    assert detect_events([], fps=FPS, config=CFG) == []


def test_flat_below_threshold_no_events() -> None:
    assert detect_events([0.1] * 500, fps=FPS, config=CFG) == []


def test_event_still_open_at_stream_end_is_closed() -> None:
    events = detect_events(_stream(62, {50: BUMP}), fps=FPS, config=CFG)
    assert len(events) == 1
    assert events[0].end_ms == 620


def test_release_lag_shifts_release() -> None:
    cfg = DetectorConfig(
        energy_threshold=0.3,
        min_gap_ms=500,
        min_event_duration_ms=100,
        max_event_duration_ms=2000,
        release_lag_ms=40,
        merge_window_ms=50,
    )
    events = detect_events(_stream(200, {50: BUMP}), fps=FPS, config=cfg)
    assert events[0].release_ms == 550 + 40


def test_release_lag_clamped_to_window_end() -> None:
    cfg = DetectorConfig(
        energy_threshold=0.3,
        min_gap_ms=500,
        min_event_duration_ms=100,
        max_event_duration_ms=2000,
        release_lag_ms=5000,
        merge_window_ms=50,
    )
    events = detect_events(_stream(200, {50: BUMP}), fps=FPS, config=cfg)
    assert events[0].release_ms == events[0].end_ms


def test_default_config_detects_machine_cadence() -> None:
    profile = [0.6] * 10 + [1.0] + [0.6] * 14  # 250 ms, over the default 200 ms minimum
    starts = [100, 400, 700]  # 3 s apart, over the default 1 s min gap
    events = detect_events(_stream(1000, dict.fromkeys(starts, profile)), fps=FPS)
    assert [event.start_ms for event in events] == [1000, 4000, 7000]
    assert [event.release_ms for event in events] == [1100, 4100, 7100]


@pytest.mark.parametrize("fps", [0.0, -30.0, math.nan])
def test_invalid_fps_rejected(fps: float) -> None:
    with pytest.raises(EventDetectionError, match="fps"):
        detect_events([0.0, 1.0], fps=fps)


def test_non_finite_energy_rejected() -> None:
    with pytest.raises(EventDetectionError, match="finite"):
        detect_events([0.1, math.nan], fps=FPS)


def test_negative_energy_rejected() -> None:
    with pytest.raises(EventDetectionError, match="sample 1"):
        detect_events([0.1, -0.2], fps=FPS)


def test_negative_onset_rejected() -> None:
    with pytest.raises(EventDetectionError, match="onsets"):
        detect_events([0.1], fps=FPS, audio_onsets_ms=[-5])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("energy_threshold", 0.0),
        ("energy_threshold", -1.0),
        ("energy_threshold", math.inf),
        ("min_gap_ms", -1),
        ("min_event_duration_ms", 0),
        ("max_event_duration_ms", 100),  # <= default min_event_duration_ms (200)
        ("release_lag_ms", -1),
        ("merge_window_ms", -1),
    ],
)
def test_invalid_config_rejected(field: str, value: float) -> None:
    kwargs: dict[str, Any] = {field: value}
    with pytest.raises(EventDetectionError, match=field):
        DetectorConfig(**kwargs)
