"""Audio onset detection for bat-contact refinement (US-D3): pure numpy DSP.

Pipeline: a short-window RMS energy envelope and a spectral-flux curve are each
normalised and summed into one novelty curve; peaks are picked with an adaptive
threshold (median + k * MAD of the novelty) and a minimum peak separation; each
surviving peak is timed to the loudest sample inside its analysis window and
classified by spectral centroid.

Classification heuristic (bands are configuration on :class:`OnsetConfig`, not
code): a bat crack is a sharp broadband transient with most energy up high, so
peaks whose centroid is at or above ``bat_crack_min_centroid_hz`` classify as
``BAT_CRACK``; pad thuds and ball-on-mat bounces are dull low-frequency thumps,
so centroids at or below ``thud_max_centroid_hz`` classify as ``THUD``; the band
between is ``UNKNOWN`` (net rustle, voices, machine hum harmonics).

Degrades gracefully (US-D3 AC): silence, too-short input, or noise-only audio
yields an empty or spurious-but-harmless onset list, and :func:`refine_contact`
returns ``None`` rather than guessing - callers keep the vision-derived timing
and flag lower confidence instead.

:func:`refine_contact` only considers onsets strictly inside
``(release_ms, end_ms]``: bat contact cannot precede release, so restricting
the search keeps the pinned event ordering invariant
``start_ms <= release_ms <= contact_ms <= end_ms`` true by construction - a
pre-release transient (machine feed clank, bounce before release) can never be
selected as the contact.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np
import numpy.typing as npt

#: Confidence multiplier applied by :func:`refine_contact` when no onset of the
#: preferred kind is inside the window and it falls back to any-kind matching.
FALLBACK_CONFIDENCE_SCALE = 0.5


class AudioOnsetError(ValueError):
    """Raised for malformed audio input, bad config, or an invalid event window."""


class OnsetKind(StrEnum):
    """What a detected transient sounds like (spectral-centroid heuristic)."""

    BAT_CRACK = "bat_crack"
    THUD = "thud"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OnsetConfig:
    """Analysis and classification tuning: every heuristic lives here, not in code."""

    window_ms: float = 10.0  # RMS/FFT analysis window length
    hop_ms: float = 5.0  # hop between analysis frames
    threshold_k: float = 6.0  # adaptive threshold: median + k * MAD of novelty
    min_separation_ms: float = 60.0  # peaks closer than this keep only the strongest
    bat_crack_min_centroid_hz: float = 2500.0  # centroid at/above -> BAT_CRACK
    thud_max_centroid_hz: float = 900.0  # centroid at/below -> THUD

    def __post_init__(self) -> None:
        if self.window_ms <= 0 or self.hop_ms <= 0:
            raise AudioOnsetError("window_ms and hop_ms must be > 0")
        if self.threshold_k < 0 or self.min_separation_ms < 0:
            raise AudioOnsetError("threshold_k and min_separation_ms must be >= 0")
        if self.thud_max_centroid_hz >= self.bat_crack_min_centroid_hz:
            raise AudioOnsetError("thud centroid band must sit below the bat-crack band")


#: Shared default so signatures avoid a call in argument defaults (frozen, safe).
DEFAULT_ONSET_CONFIG = OnsetConfig()


@dataclass(frozen=True)
class Onset:
    """One detected transient on the audio timeline."""

    time_ms: float
    strength: float  # novelty at the peak, normalised to (0, 1] within the signal
    kind: OnsetKind


@dataclass(frozen=True)
class RefinedContact:
    """Audio-refined contact estimate for one ball event window."""

    contact_ms: float
    confidence: float
    kind: OnsetKind


def _normalised(curve: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Scale a non-negative curve to [0, 1]; an all-zero curve stays zero."""
    peak = float(curve.max())
    if peak <= 0.0:
        return np.zeros_like(curve)
    return curve / peak


def _pick_peaks(
    novelty: npt.NDArray[np.float64], threshold: float, min_gap_frames: int
) -> list[int]:
    """Local maxima above the threshold, deduped to the strongest per gap window."""
    candidates: list[int] = []
    for i in range(1, novelty.size):  # novelty[0] is 0 by construction (diff prepend)
        rising = novelty[i] >= novelty[i - 1]
        falling = i + 1 == novelty.size or novelty[i] > novelty[i + 1]
        if novelty[i] > threshold and rising and falling:
            candidates.append(i)
    kept: list[int] = []
    for i in sorted(candidates, key=lambda idx: (-novelty[idx], idx)):
        if all(abs(i - j) >= min_gap_frames for j in kept):
            kept.append(i)
    return sorted(kept)


def _classify(
    magnitudes: npt.NDArray[np.float64],
    freqs_hz: npt.NDArray[np.float64],
    config: OnsetConfig,
) -> OnsetKind:
    """Spectral-centroid band classification; degenerate spectra are UNKNOWN."""
    total = float(magnitudes.sum())
    if total <= 0.0:
        return OnsetKind.UNKNOWN
    centroid_hz = float((freqs_hz * magnitudes).sum() / total)
    if centroid_hz >= config.bat_crack_min_centroid_hz:
        return OnsetKind.BAT_CRACK
    if centroid_hz <= config.thud_max_centroid_hz:
        return OnsetKind.THUD
    return OnsetKind.UNKNOWN


def detect_onsets(
    samples: npt.NDArray[np.floating[Any]],
    sample_rate: int,
    *,
    config: OnsetConfig = DEFAULT_ONSET_CONFIG,
) -> list[Onset]:
    """Detect transient onsets in mono audio; empty list when nothing stands out."""
    if sample_rate <= 0:
        raise AudioOnsetError(f"sample_rate must be > 0, got {sample_rate}")
    signal = np.asarray(samples, dtype=np.float64)
    if signal.ndim != 1:
        raise AudioOnsetError(f"samples must be 1-D mono, got shape {signal.shape}")
    window = max(2, round(sample_rate * config.window_ms / 1000.0))
    hop = max(1, round(sample_rate * config.hop_ms / 1000.0))
    if signal.size < window:
        return []  # shorter than one analysis window: nothing to detect, no crash

    frames = np.lib.stride_tricks.sliding_window_view(signal, window)[::hop]
    rms: npt.NDArray[np.float64] = np.sqrt(np.mean(frames**2, axis=1))
    magnitudes: npt.NDArray[np.float64] = np.abs(np.fft.rfft(frames * np.hanning(window), axis=1))

    energy_novelty = np.maximum(np.diff(rms, prepend=rms[:1]), 0.0)
    flux = np.zeros(frames.shape[0], dtype=np.float64)
    if frames.shape[0] > 1:
        flux[1:] = np.maximum(np.diff(magnitudes, axis=0), 0.0).sum(axis=1)
    novelty = _normalised(energy_novelty) + _normalised(flux)

    median = float(np.median(novelty))
    mad = float(np.median(np.abs(novelty - median)))
    threshold = median + config.threshold_k * mad
    min_gap_frames = max(1, round(config.min_separation_ms * sample_rate / (1000.0 * hop)))

    max_novelty = float(novelty.max())
    freqs_hz = np.fft.rfftfreq(window, d=1.0 / sample_rate)
    onsets: list[Onset] = []
    for i in _pick_peaks(novelty, threshold, min_gap_frames):
        peak_sample = i * hop + int(np.argmax(frames[i] ** 2))
        onsets.append(
            Onset(
                time_ms=peak_sample * 1000.0 / sample_rate,
                strength=float(novelty[i]) / max_novelty,
                kind=_classify(magnitudes[i], freqs_hz, config),
            )
        )
    return onsets


def refine_contact(
    event: tuple[float, float],
    onsets: Sequence[Onset],
    *,
    release_ms: float,
    prefer: OnsetKind = OnsetKind.BAT_CRACK,
) -> RefinedContact | None:
    """Refine a contact time from onsets strictly inside ``(release_ms, end_ms]``.

    Bat contact is physically impossible at or before release, so only onsets
    strictly after ``release_ms`` (and at or before the window end) are
    candidates - the result always satisfies the pinned ordering invariant
    ``start_ms <= release_ms <= contact_ms <= end_ms``. Picks the
    preferred-kind onset nearest the centre of that post-release span; when
    none of the preferred kind is inside, falls back to the nearest onset of
    any kind at :data:`FALLBACK_CONFIDENCE_SCALE` confidence. Returns ``None``
    when no onset is inside the span at all - the caller keeps its
    vision-derived timing and flags lower confidence (US-D3 AC).
    """
    start_ms, end_ms = event
    if end_ms <= start_ms:
        raise AudioOnsetError(f"event end {end_ms} must be after start {start_ms}")
    if not start_ms <= release_ms <= end_ms:
        raise AudioOnsetError(
            f"release {release_ms} must lie inside the event window [{start_ms}, {end_ms}]"
        )
    inside = [onset for onset in onsets if release_ms < onset.time_ms <= end_ms]
    if not inside:
        return None
    preferred = [onset for onset in inside if onset.kind is prefer]
    pool = preferred or inside
    scale = 1.0 if preferred else FALLBACK_CONFIDENCE_SCALE
    centre = (release_ms + end_ms) / 2.0
    best = min(pool, key=lambda o: (abs(o.time_ms - centre), -o.strength, o.time_ms))
    confidence = max(0.0, min(1.0, best.strength)) * scale
    return RefinedContact(contact_ms=best.time_ms, confidence=confidence, kind=best.kind)
