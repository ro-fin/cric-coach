"""Recording quality-control checks for the capture rig (US-A2).

Pure functions over numpy arrays and plain data — no camera hardware. These
back the acceptance criteria of US-A2 (high-FPS, flicker-free, synchronized
video):

- every video file carries complete metadata (:func:`validate_video_metadata`),
- cross-camera sync error ≤ 1 frame at 120 fps ≈ 8.3 ms
  (:func:`sync_offsets` / :func:`assert_synchronized`),
- lighting produces no visible flicker banding (:func:`flicker_score`),
- no motion smear longer than 2 ball-diameters in a single frame
  (:func:`motion_smear_length_px` / :func:`max_shutter_for_smear`),
- zero dropped-frame periods > 100 ms (:func:`detect_frame_gaps`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

#: Regulation cricket ball diameter in metres (~7.3 cm), used to express the
#: "no smear longer than 2 ball-diameters" acceptance criterion in pixels.
BALL_DIAMETER_M: Final = 0.073

#: Half-width in Hz of the frequency band searched around the mains frequency
#: (and its first harmonic) when scoring flicker.
MAINS_BAND_HALF_WIDTH_HZ: Final = 2.0

_KPH_TO_MPS: Final = 1.0 / 3.6

_RESOLUTION_RE: Final = re.compile(r"[1-9]\d*x[1-9]\d*")


class SyncError(Exception):
    """One or more cameras exceed the allowed cross-camera sync tolerance."""


def _require_positive(**values: float) -> None:
    """Raise ``ValueError`` for any keyword that is not a positive real number."""
    for name, value in values.items():
        if math.isnan(value) or value <= 0.0:
            raise ValueError(f"{name} must be a positive number, got {value}")


# ---------------------------------------------------------------------------
# 1. Video metadata
# ---------------------------------------------------------------------------


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_positive_number(value: object) -> bool:
    return _is_finite_number(value) and isinstance(value, int | float) and value > 0


def _is_start_ts(value: object) -> bool:
    """Accept an ISO-8601 style string or a finite numeric epoch timestamp."""
    return _is_nonempty_str(value) or _is_finite_number(value)


def _is_resolution(value: object) -> bool:
    """Accept ``'WxH'`` with positive integer width and height, e.g. ``'1920x1080'``."""
    return isinstance(value, str) and _RESOLUTION_RE.fullmatch(value) is not None


#: Required metadata fields (US-A2 AC) in canonical order, with their validators.
_METADATA_VALIDATORS: Final[dict[str, Callable[[object], bool]]] = {
    "camera_id": _is_nonempty_str,
    "session_id": _is_nonempty_str,
    "start_ts": _is_start_ts,
    "fps": _is_positive_number,
    "resolution": _is_resolution,
    "codec": _is_nonempty_str,
    "duration": _is_positive_number,
}


def validate_video_metadata(meta: dict[str, object]) -> list[str]:
    """Return the names of missing or invalid required video-metadata fields.

    Required fields: ``camera_id``, ``session_id``, ``start_ts``, ``fps``,
    ``resolution``, ``codec``, ``duration``. An empty list means the metadata
    is valid. Rules: ``fps`` and ``duration`` must be finite numbers > 0;
    ``resolution`` must be ``'WxH'``; ``start_ts`` may be a non-empty string
    or a finite epoch number; the rest must be non-empty strings. Unknown
    extra fields are ignored.
    """
    return [
        field for field, is_valid in _METADATA_VALIDATORS.items() if not is_valid(meta.get(field))
    ]


# ---------------------------------------------------------------------------
# 2. Cross-camera sync
# ---------------------------------------------------------------------------


def sync_offsets(flash_frames: dict[str, int], fps: float) -> dict[str, float]:
    """Per-camera offsets in ms relative to the earliest camera.

    ``flash_frames`` maps camera id → frame index at which a shared sync event
    (LED flash / clap) is first visible in that camera. With a common frame
    rate ``fps``, the offset of camera *c* is::

        offset_ms(c) = (flash_frames[c] - min(flash_frames.values())) * 1000 / fps

    The earliest camera therefore has offset 0.0.
    """
    _require_positive(fps=fps)
    if not flash_frames:
        raise ValueError("flash_frames must contain at least one camera")
    earliest = min(flash_frames.values())
    frame_ms = 1000.0 / fps
    return {camera: (frame - earliest) * frame_ms for camera, frame in flash_frames.items()}


def max_sync_error_ms(offsets: dict[str, float]) -> float:
    """Worst-case pairwise sync error: ``max(offsets) - min(offsets)`` in ms.

    For offsets produced by :func:`sync_offsets` the minimum is 0.0, so this
    is simply the largest offset. An empty mapping has zero error.
    """
    if not offsets:
        return 0.0
    values = offsets.values()
    return max(values) - min(values)


def assert_synchronized(offsets: dict[str, float], fps: float, max_frames: float = 1.0) -> None:
    """Raise :class:`SyncError` if any camera offset exceeds the tolerance.

    The tolerance is ``max_frames * 1000 / fps`` ms — i.e. at the default of
    one frame and 120 fps, ~8.333 ms (US-A2 AC: sync error ≤ 1 frame at
    120 fps). The error message lists every offending camera.
    """
    _require_positive(fps=fps, max_frames=max_frames)
    tolerance_ms = max_frames * 1000.0 / fps
    offenders = sorted(camera for camera, offset in offsets.items() if abs(offset) > tolerance_ms)
    if offenders:
        raise SyncError(f"cameras out of sync beyond {tolerance_ms:.3f} ms: {', '.join(offenders)}")


# ---------------------------------------------------------------------------
# 3. Lighting flicker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlickerReport:
    """Spectral flicker analysis of a mean-brightness time series.

    ``dominant_hz`` is the frequency of the strongest non-DC component.
    ``mains_band_power_ratio`` is the fraction of total AC power that falls
    within ±:data:`MAINS_BAND_HALF_WIDTH_HZ` of the mains frequency or its
    first harmonic (lights driven by AC mains flicker at twice mains).
    ``flicker_detected`` is true when that ratio exceeds the threshold.
    """

    dominant_hz: float
    mains_band_power_ratio: float
    flicker_detected: bool


def flicker_score(
    brightness: npt.NDArray[np.float64],
    fps: float,
    mains_hz: float = 50.0,
    threshold: float = 0.3,
) -> FlickerReport:
    """Score mains-lighting flicker in a per-frame mean-brightness series.

    The series is de-meaned and transformed with a real FFT sampled at
    ``fps`` Hz; bin *k* has frequency ``k * fps / n`` and power ``|X_k|²``.
    The mains band power ratio is::

        ratio = Σ power(f) for |f - mains_hz| ≤ 2 or |f - 2·mains_hz| ≤ 2
                ─────────────────────────────────────────────────────────
                Σ power(f) over all f > 0  (total AC power)

    ``flicker_detected`` is ``ratio > threshold`` (initial threshold 0.3).
    A perfectly constant series has zero AC power and reports no flicker.
    """
    _require_positive(fps=fps, mains_hz=mains_hz)
    series = np.asarray(brightness, dtype=np.float64)
    if series.ndim != 1:
        raise ValueError(f"brightness must be a 1-D time series, got {series.ndim} dimensions")
    if series.size < 2:
        raise ValueError("brightness must contain at least two samples")

    ac = series - series.mean()
    power = np.abs(np.fft.rfft(ac)) ** 2
    freqs = np.fft.rfftfreq(series.size, d=1.0 / fps)
    ac_power = power[1:]  # drop the (already ~zero) DC bin
    ac_freqs = freqs[1:]
    total = float(ac_power.sum())
    if total <= 0.0:
        return FlickerReport(dominant_hz=0.0, mains_band_power_ratio=0.0, flicker_detected=False)

    dominant_hz = float(ac_freqs[int(np.argmax(ac_power))])
    in_band = (np.abs(ac_freqs - mains_hz) <= MAINS_BAND_HALF_WIDTH_HZ) | (
        np.abs(ac_freqs - 2.0 * mains_hz) <= MAINS_BAND_HALF_WIDTH_HZ
    )
    ratio = float(ac_power[in_band].sum()) / total
    return FlickerReport(
        dominant_hz=dominant_hz,
        mains_band_power_ratio=ratio,
        flicker_detected=ratio > threshold,
    )


# ---------------------------------------------------------------------------
# 4. Motion smear vs shutter speed
# ---------------------------------------------------------------------------


def motion_smear_length_px(ball_speed_kph: float, shutter_s: float, px_per_m: float) -> float:
    """Length in pixels of the streak a moving ball leaves in a single frame.

    During an exposure of ``shutter_s`` seconds the ball travels
    ``(ball_speed_kph / 3.6) * shutter_s`` metres, which projects to::

        smear_px = ball_speed_kph / 3.6 * shutter_s * px_per_m
    """
    _require_positive(ball_speed_kph=ball_speed_kph, shutter_s=shutter_s, px_per_m=px_per_m)
    return ball_speed_kph * _KPH_TO_MPS * shutter_s * px_per_m


def max_shutter_for_smear(ball_speed_kph: float, px_per_m: float, max_smear_px: float) -> float:
    """Longest shutter time (s) keeping smear within ``max_smear_px`` pixels.

    Inverse of :func:`motion_smear_length_px`::

        shutter_s = max_smear_px / (ball_speed_kph / 3.6 * px_per_m)

    US-A2 AC example: smear must not exceed 2 ball-diameters, i.e.
    ``max_smear_px = 2 * BALL_DIAMETER_M * px_per_m``.
    """
    _require_positive(ball_speed_kph=ball_speed_kph, px_per_m=px_per_m, max_smear_px=max_smear_px)
    return max_smear_px / (ball_speed_kph * _KPH_TO_MPS * px_per_m)


# ---------------------------------------------------------------------------
# 5. Dropped frames
# ---------------------------------------------------------------------------


def detect_frame_gaps(
    timestamps_s: list[float],
    fps: float,
    max_gap_ms: float = 100.0,
) -> list[tuple[int, float]]:
    """Find dropped-frame periods longer than ``max_gap_ms`` milliseconds.

    ``timestamps_s`` are per-frame capture timestamps in seconds, in order.
    Each gap is ``(timestamps_s[i + 1] - timestamps_s[i]) * 1000`` ms and is
    reported as ``(i, gap_ms)`` — the index of the frame *before* the gap —
    whenever ``gap_ms > max_gap_ms``. An empty result means the recording
    meets the US-A2 AC of zero dropped-frame periods > 100 ms.

    ``max_gap_ms`` must be at least one nominal frame interval
    (``1000 / fps`` ms), otherwise every normally spaced frame would be
    reported as a gap.
    """
    _require_positive(fps=fps, max_gap_ms=max_gap_ms)
    frame_interval_ms = 1000.0 / fps
    if max_gap_ms < frame_interval_ms:
        raise ValueError(
            f"max_gap_ms={max_gap_ms} is below one frame interval "
            f"({frame_interval_ms:.3f} ms at {fps} fps)"
        )
    gaps_ms = np.diff(np.asarray(timestamps_s, dtype=np.float64)) * 1000.0
    negative = np.nonzero(gaps_ms < 0.0)[0]
    if negative.size:
        raise ValueError(
            f"timestamps_s must be non-decreasing (first violation after index {int(negative[0])})"
        )
    return [(int(i), float(gaps_ms[i])) for i in np.nonzero(gaps_ms > max_gap_ms)[0]]
