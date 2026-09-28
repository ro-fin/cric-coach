"""US-D3 audio onset tests: synthetic impulse+noise mixes, SNR sweep, refinement."""

import numpy as np
import numpy.typing as npt
import pytest
from cricai_vision.audio_onset import (
    DEFAULT_ONSET_CONFIG,
    FALLBACK_CONFIDENCE_SCALE,
    AudioOnsetError,
    Onset,
    OnsetConfig,
    OnsetKind,
    RefinedContact,
    _classify,
    _pick_peaks,
    detect_onsets,
    refine_contact,
)

SR = 48_000
#: One frame at 120 fps: the US-D3 timing budget for median onset error.
FRAME_120FPS_MS = 1000.0 / 120.0

CRACK_HZ = 4000.0  # sharp high-frequency transient (bat on ball)
THUD_HZ = 150.0  # dull low-frequency thump (pad / mat)
MID_HZ = 1600.0  # between the config bands -> UNKNOWN


def _burst(
    freq_hz: float, *, duration_ms: float = 6.0, amplitude: float = 1.0
) -> npt.NDArray[np.float64]:
    """Decaying sinusoid transient starting at its first sample."""
    n = int(SR * duration_ms / 1000.0)
    t = np.arange(n, dtype=np.float64) / SR
    envelope = np.exp(-t / (duration_ms / 3000.0))
    return amplitude * envelope * np.sin(2.0 * np.pi * freq_hz * t)


def _track(
    duration_ms: float, events: list[tuple[float, npt.NDArray[np.float64]]]
) -> npt.NDArray[np.float64]:
    """Silent track with transients mixed in at the given millisecond offsets."""
    out = np.zeros(int(SR * duration_ms / 1000.0), dtype=np.float64)
    for at_ms, burst in events:
        start = int(SR * at_ms / 1000.0)
        end = min(out.size, start + burst.size)
        out[start:end] += burst[: end - start]
    return out


def _crack_onset(time_ms: float, strength: float = 0.9) -> Onset:
    return Onset(time_ms=time_ms, strength=strength, kind=OnsetKind.BAT_CRACK)


def _thud_onset(time_ms: float, strength: float = 0.9) -> Onset:
    return Onset(time_ms=time_ms, strength=strength, kind=OnsetKind.THUD)


def test_clean_impulse_train_timing_within_one_frame_at_120fps() -> None:
    truth_ms = [400.0, 1300.0, 2200.0, 3100.0]
    samples = _track(4000.0, [(t, _burst(CRACK_HZ)) for t in truth_ms])
    onsets = detect_onsets(samples, SR)
    assert len(onsets) == len(truth_ms)
    assert [o.time_ms for o in onsets] == sorted(o.time_ms for o in onsets)
    errors = [abs(o.time_ms - t) for o, t in zip(onsets, truth_ms, strict=True)]
    assert float(np.median(errors)) <= FRAME_120FPS_MS
    assert all(o.kind is OnsetKind.BAT_CRACK for o in onsets)
    assert all(0.0 < o.strength <= 1.0 for o in onsets)
    assert max(o.strength for o in onsets) == 1.0


@pytest.mark.parametrize(
    ("freq_hz", "expected"),
    [
        (CRACK_HZ, OnsetKind.BAT_CRACK),
        (THUD_HZ, OnsetKind.THUD),
        (MID_HZ, OnsetKind.UNKNOWN),
    ],
)
def test_kind_classification_by_spectral_centroid_band(freq_hz: float, expected: OnsetKind) -> None:
    duration_ms = 30.0 if freq_hz == THUD_HZ else 6.0
    samples = _track(1000.0, [(500.0, _burst(freq_hz, duration_ms=duration_ms))])
    onsets = detect_onsets(samples, SR)
    assert len(onsets) == 1
    assert onsets[0].kind is expected


@pytest.mark.parametrize("snr_db", [30.0, 20.0])
def test_detection_holds_at_high_snr(snr_db: float) -> None:
    rng = np.random.default_rng(7)
    clean = _track(1000.0, [(500.0, _burst(CRACK_HZ))])
    noisy = clean + rng.normal(0.0, 10.0 ** (-snr_db / 20.0), clean.size)
    onsets = detect_onsets(noisy, SR)
    assert len(onsets) >= 1
    best = min(onsets, key=lambda o: abs(o.time_ms - 500.0))
    assert abs(best.time_ms - 500.0) <= FRAME_120FPS_MS


@pytest.mark.parametrize("snr_db", [-10.0, -20.0])
def test_low_snr_degrades_without_crashing(snr_db: float) -> None:
    rng = np.random.default_rng(11)
    clean = _track(1000.0, [(500.0, _burst(CRACK_HZ))])
    noisy = clean + rng.normal(0.0, 10.0 ** (-snr_db / 20.0), clean.size)
    onsets = detect_onsets(noisy, SR)
    assert isinstance(onsets, list)  # may be empty or spurious, never a crash


def test_machine_hum_background_does_not_mask_the_crack() -> None:
    t = np.arange(int(SR * 1.0), dtype=np.float64) / SR
    hum = 0.05 * np.sin(2.0 * np.pi * 50.0 * t) + 0.02 * np.sin(2.0 * np.pi * 150.0 * t)
    samples = _track(1000.0, [(500.0, _burst(CRACK_HZ))]) + hum
    onsets = detect_onsets(samples, SR)
    assert len(onsets) == 1
    assert abs(onsets[0].time_ms - 500.0) <= FRAME_120FPS_MS
    assert onsets[0].kind is OnsetKind.BAT_CRACK


def test_silence_short_empty_and_dc_inputs_yield_no_onsets() -> None:
    assert detect_onsets(np.zeros(0), SR) == []
    assert detect_onsets(np.zeros(100), SR) == []  # shorter than one window
    assert detect_onsets(np.zeros(SR), SR) == []
    assert detect_onsets(np.ones(SR), SR) == []  # DC: zero novelty on both channels
    assert detect_onsets(np.zeros(480), SR) == []  # exactly one analysis frame


def test_input_validation() -> None:
    with pytest.raises(AudioOnsetError, match="1-D mono"):
        detect_onsets(np.zeros((2, SR)), SR)
    with pytest.raises(AudioOnsetError, match="sample_rate"):
        detect_onsets(np.zeros(SR), 0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"window_ms": 0.0}, "window_ms and hop_ms"),
        ({"hop_ms": -1.0}, "window_ms and hop_ms"),
        ({"threshold_k": -0.5}, "threshold_k and min_separation_ms"),
        ({"min_separation_ms": -1.0}, "threshold_k and min_separation_ms"),
        ({"thud_max_centroid_hz": 3000.0}, "must sit below"),
    ],
)
def test_config_validation(kwargs: dict[str, float], match: str) -> None:
    with pytest.raises(AudioOnsetError, match=match):
        OnsetConfig(**kwargs)


def test_min_separation_keeps_only_the_strongest_of_close_peaks() -> None:
    events = [(500.0, _burst(CRACK_HZ)), (520.0, _burst(CRACK_HZ, amplitude=0.6))]
    samples = _track(1000.0, events)
    merged = detect_onsets(samples, SR)  # default 60 ms separation
    assert len(merged) == 1
    assert abs(merged[0].time_ms - 500.0) <= FRAME_120FPS_MS
    split = detect_onsets(samples, SR, config=OnsetConfig(min_separation_ms=10.0))
    assert len(split) == 2


def test_onset_in_final_analysis_frame_is_detected() -> None:
    samples = np.zeros(SR, dtype=np.float64)
    samples[47_800] = 1.0  # inside only the last frame's window
    onsets = detect_onsets(samples, SR)
    assert len(onsets) == 1
    assert abs(onsets[0].time_ms - 47_800 * 1000.0 / SR) <= FRAME_120FPS_MS


def test_pick_peaks_skips_shoulders_and_dedupes_to_strongest() -> None:
    novelty = np.array([0.0, 1.0, 0.8, 0.0, 0.9, 0.0])
    assert _pick_peaks(novelty, 0.5, 1) == [1, 4]  # index 2 is a falling shoulder
    assert _pick_peaks(novelty, 0.5, 4) == [1]  # 4 is within the gap of stronger 1


def test_classify_degenerate_spectrum_is_unknown() -> None:
    freqs = np.fft.rfftfreq(480, d=1.0 / SR)
    zeros = np.zeros(freqs.size)
    assert _classify(zeros, freqs, DEFAULT_ONSET_CONFIG) is OnsetKind.UNKNOWN


def test_refine_contact_picks_preferred_kind_nearest_post_release_centre() -> None:
    onsets = [
        _crack_onset(1500.0),
        _thud_onset(1995.0),
        _crack_onset(2100.0, strength=0.8),
        _crack_onset(9000.0),  # outside the window, ignored
    ]
    refined = refine_contact((1000.0, 3000.0), onsets, release_ms=1000.0)
    assert refined == RefinedContact(contact_ms=2100.0, confidence=0.8, kind=OnsetKind.BAT_CRACK)


def test_refine_contact_centre_anchor_is_the_post_release_span() -> None:
    # Release 2000 -> span (2000, 3000], centre 2500: 2600 wins over 2100
    # (the full-window centre 2000 would have preferred 2100).
    onsets = [_crack_onset(2100.0), _crack_onset(2600.0)]
    refined = refine_contact((1000.0, 3000.0), onsets, release_ms=2000.0)
    assert refined is not None
    assert refined.contact_ms == 2600.0


def test_refine_contact_ignores_onsets_at_or_before_release() -> None:
    # 1500 (pre-release) and 2000 (exactly at release) are physically
    # impossible contacts; only the post-release 2400 is a candidate.
    onsets = [_crack_onset(1500.0), _crack_onset(2000.0), _crack_onset(2400.0, strength=0.6)]
    refined = refine_contact((1000.0, 3000.0), onsets, release_ms=2000.0)
    assert refined == RefinedContact(contact_ms=2400.0, confidence=0.6, kind=OnsetKind.BAT_CRACK)


def test_refine_contact_returns_none_when_only_pre_release_onsets_exist() -> None:
    assert refine_contact((1000.0, 3000.0), [_crack_onset(1500.0)], release_ms=2000.0) is None
    assert refine_contact((1000.0, 3000.0), [_thud_onset(1500.0)], release_ms=2000.0) is None


def test_refine_contact_release_at_window_end_yields_none() -> None:
    # A release clamped to the window end leaves no room for contact: no guess.
    assert refine_contact((1000.0, 3000.0), [_crack_onset(2500.0)], release_ms=3000.0) is None


def test_refine_contact_falls_back_to_any_kind_at_reduced_confidence() -> None:
    refined = refine_contact(
        (1000.0, 3000.0), [_thud_onset(1900.0, strength=0.8)], release_ms=1000.0
    )
    assert refined is not None
    assert refined.kind is OnsetKind.THUD
    assert refined.confidence == pytest.approx(0.8 * FALLBACK_CONFIDENCE_SCALE)


def test_refine_contact_returns_none_when_window_is_empty() -> None:
    assert refine_contact((1000.0, 3000.0), [], release_ms=1000.0) is None
    assert refine_contact((1000.0, 3000.0), [_crack_onset(500.0)], release_ms=1000.0) is None


def test_refine_contact_rejects_invalid_window() -> None:
    with pytest.raises(AudioOnsetError, match="must be after start"):
        refine_contact((3000.0, 3000.0), [_crack_onset(1500.0)], release_ms=3000.0)


def test_refine_contact_rejects_release_outside_window() -> None:
    with pytest.raises(AudioOnsetError, match="must lie inside"):
        refine_contact((1000.0, 3000.0), [], release_ms=500.0)
    with pytest.raises(AudioOnsetError, match="must lie inside"):
        refine_contact((1000.0, 3000.0), [], release_ms=3500.0)


def test_refine_contact_prefer_parameter_switches_the_preferred_kind() -> None:
    onsets = [_crack_onset(2050.0), _thud_onset(1800.0, strength=0.7)]
    refined = refine_contact((1000.0, 3000.0), onsets, release_ms=1000.0, prefer=OnsetKind.THUD)
    assert refined == RefinedContact(contact_ms=1800.0, confidence=0.7, kind=OnsetKind.THUD)


def test_refine_contact_breaks_centre_distance_ties_by_strength() -> None:
    onsets = [_crack_onset(1900.0, strength=0.5), _crack_onset(2100.0, strength=0.9)]
    refined = refine_contact((1000.0, 3000.0), onsets, release_ms=1000.0)
    assert refined is not None
    assert refined.contact_ms == 2100.0


def test_refine_contact_clamps_confidence_to_unit_range() -> None:
    high = refine_contact((0.0, 100.0), [_crack_onset(50.0, strength=1.7)], release_ms=0.0)
    assert high is not None
    assert high.confidence == 1.0
    low = refine_contact((0.0, 100.0), [_crack_onset(50.0, strength=-0.2)], release_ms=0.0)
    assert low is not None
    assert low.confidence == 0.0
