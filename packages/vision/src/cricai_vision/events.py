"""Cue-based ball-event detection (US-D1): motion-energy segmentation + audio contact.

Pure math over a per-frame motion-energy envelope (reference camera C1) plus
optional audio onset times; no I/O. The worker job
(``cricai_worker.detect_events``) persists candidates as ``ball_events`` rows;
model-based detection (V2) slots in behind the same :class:`EventCandidate`
interface with a bumped :data:`DETECTOR_VERSION` so benchmark studies can
compare detector generations without schema change.

Segmentation pipeline:

1. threshold-crossing runs on the energy envelope -> raw windows;
2. windows closer than ``merge_window_ms`` are unified (sub-threshold flicker
   inside one delivery must not split it);
3. windows shorter than ``min_event_duration_ms`` are dropped as noise blips;
4. surviving windows closer than ``min_gap_ms`` are merged - two energy bumps
   from a single delivery (a double feed) become ONE event, keeping duplicates
   under the US-D1 2% budget;
5. release = onset of the energy peak within the window + ``release_lag_ms``
   (clamped to the window end); contact = the earliest audio onset strictly
   inside ``(release_ms, end_ms]``, or ``None`` when no onset lands there - a
   left ball is still an event. Bat contact cannot precede release, so a
   pre-release sound (machine feed clank, bounce) is never emitted as contact
   and every candidate satisfies the pinned ordering invariant
   ``start_ms <= release_ms <= contact_ms <= end_ms`` by construction.

Confidence blends peak prominence above the threshold with duration
plausibility against ``max_event_duration_ms`` and always lands in [0, 1].
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

#: Recorded on every auto-detected ``ball_events`` row for benchmark tracking.
DETECTOR_VERSION = "cue-segmenter-1.0.0"


class EventDetectionError(ValueError):
    """Invalid detector configuration or input stream."""


@dataclass(frozen=True)
class DetectorConfig:
    """Segmenter tuning knobs; defaults sized for a roughly normalized envelope."""

    energy_threshold: float = 0.3
    min_gap_ms: int = 1000
    min_event_duration_ms: int = 200
    max_event_duration_ms: int = 4000
    release_lag_ms: int = 0
    merge_window_ms: int = 150

    def __post_init__(self) -> None:
        if not math.isfinite(self.energy_threshold) or self.energy_threshold <= 0:
            raise EventDetectionError(
                f"energy_threshold must be finite and > 0, got {self.energy_threshold}"
            )
        if self.min_gap_ms < 0:
            raise EventDetectionError(f"min_gap_ms must be >= 0, got {self.min_gap_ms}")
        if self.min_event_duration_ms <= 0:
            raise EventDetectionError(
                f"min_event_duration_ms must be > 0, got {self.min_event_duration_ms}"
            )
        if self.max_event_duration_ms <= self.min_event_duration_ms:
            raise EventDetectionError(
                f"max_event_duration_ms ({self.max_event_duration_ms}) must exceed "
                f"min_event_duration_ms ({self.min_event_duration_ms})"
            )
        if self.release_lag_ms < 0:
            raise EventDetectionError(f"release_lag_ms must be >= 0, got {self.release_lag_ms}")
        if self.merge_window_ms < 0:
            raise EventDetectionError(f"merge_window_ms must be >= 0, got {self.merge_window_ms}")


DEFAULT_CONFIG = DetectorConfig()


@dataclass(frozen=True)
class EventCandidate:
    """One candidate ball event, in reference-camera video-timeline milliseconds."""

    start_ms: int
    release_ms: int
    contact_ms: int | None  # None = no audio onset in window (left ball / no audio)
    end_ms: int
    confidence: float  # always in [0, 1]


def _to_ms(frame_index: int, fps: float) -> int:
    return round(frame_index * 1000.0 / fps)


def _validate(
    motion_energy: Sequence[float], fps: float, audio_onsets_ms: Sequence[int] | None
) -> None:
    if not math.isfinite(fps) or fps <= 0:
        raise EventDetectionError(f"fps must be finite and > 0, got {fps}")
    for index, value in enumerate(motion_energy):
        if not math.isfinite(value) or value < 0:
            raise EventDetectionError(
                f"motion energy must be finite and >= 0, sample {index} is {value}"
            )
    for onset in audio_onsets_ms or ():
        if onset < 0:
            raise EventDetectionError(f"audio onsets must be >= 0 ms, got {onset}")


def _threshold_runs(motion_energy: Sequence[float], threshold: float) -> list[tuple[int, int]]:
    """Maximal ``[start, end)`` index runs where energy exceeds the threshold."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(motion_energy):
        if value > threshold:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:  # stream ended while still above threshold
        runs.append((start, len(motion_energy)))
    return runs


def _merge_windows(
    windows: list[tuple[int, int]], *, fps: float, gap_ms: int
) -> list[tuple[int, int]]:
    """Unify consecutive windows separated by less than ``gap_ms``."""
    if not windows:
        return []
    merged = [windows[0]]
    for start, end in windows[1:]:
        last_start, last_end = merged[-1]
        if _to_ms(start, fps) - _to_ms(last_end, fps) < gap_ms:
            merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def _nearest_onset(onsets: Sequence[int], *, end_ms: int, release_ms: int) -> int | None:
    """The earliest onset strictly inside ``(release_ms, end_ms]``; ``None`` when absent.

    Contact physically follows release, so onsets at or before release are
    excluded: the detector can never emit ``contact_ms <= release_ms`` (the
    ordering invariant the corrections API enforces). Among eligible onsets the
    earliest is by definition the one nearest to release.
    """
    inside = [onset for onset in onsets if release_ms < onset <= end_ms]
    if not inside:
        return None
    return min(inside)


def _confidence(peak_value: float, duration_ms: int, config: DetectorConfig) -> float:
    """Peak prominence above threshold blended with duration plausibility, in [0, 1]."""
    prominence = 1.0 - config.energy_threshold / peak_value  # peak > threshold by construction
    plausibility = (
        1.0
        if duration_ms <= config.max_event_duration_ms
        else config.max_event_duration_ms / duration_ms
    )
    return round((prominence + plausibility) / 2.0, 4)


def _candidate(
    window: tuple[int, int],
    motion_energy: Sequence[float],
    onsets: Sequence[int],
    fps: float,
    config: DetectorConfig,
) -> EventCandidate:
    start, end = window
    start_ms = _to_ms(start, fps)
    end_ms = _to_ms(end, fps)
    peak_index = max(range(start, end), key=lambda index: motion_energy[index])
    release_ms = min(_to_ms(peak_index, fps) + config.release_lag_ms, end_ms)
    return EventCandidate(
        start_ms=start_ms,
        release_ms=release_ms,
        contact_ms=_nearest_onset(onsets, end_ms=end_ms, release_ms=release_ms),
        end_ms=end_ms,
        confidence=_confidence(motion_energy[peak_index], end_ms - start_ms, config),
    )


def detect_events(
    motion_energy: Sequence[float],
    *,
    fps: float,
    audio_onsets_ms: Sequence[int] | None = None,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> list[EventCandidate]:
    """Segment a motion-energy envelope into candidate ball events.

    ``motion_energy`` is one value per video frame at ``fps`` on the reference
    camera; ``audio_onsets_ms`` are optional bat-crack onset times on the same
    timeline. Raises :class:`EventDetectionError` on invalid inputs. Returns
    candidates ordered by ``start_ms``; an empty or all-quiet stream yields ``[]``.
    """
    _validate(motion_energy, fps, audio_onsets_ms)
    windows = _merge_windows(
        _threshold_runs(motion_energy, config.energy_threshold),
        fps=fps,
        gap_ms=config.merge_window_ms,
    )
    windows = [
        window
        for window in windows
        if _to_ms(window[1], fps) - _to_ms(window[0], fps) >= config.min_event_duration_ms
    ]
    windows = _merge_windows(windows, fps=fps, gap_ms=config.min_gap_ms)
    onsets = tuple(audio_onsets_ms) if audio_onsets_ms is not None else ()
    return [_candidate(window, motion_energy, onsets, fps, config) for window in windows]
