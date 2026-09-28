"""Config-driven line/length zone classification (US-C5, reused by US-E4/F4/I4).

Zone boundaries are configuration, not code: changing them re-derives classes
from stored raw pitch coordinates without re-clicking. Line channels are defined
for a right-hand batter in the canonical frame (+y = off side) and mirrored for
left-handers by negating y before lookup.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cricai_data.enums import Handedness, Length, Line

from cricai_vision.geometry import PITCH_LENGTH_M

#: Mapping slack (m) tolerated just off either end of the pitch: US-C2 accepts
#: up to ~3 cm calibration error, so a legitimate yorker click can map to a
#: slightly negative x. Values inside the epsilon clamp to the pitch; beyond it
#: the click is treated as off-pitch (a mis-click) and rejected.
DEFAULT_CLAMP_EPSILON_M = 0.05

#: Lateral offsets beyond this magnitude (m) are implausibly wide for a
#: delivery on or near the pitch (pitch half-width is ~1.5 m) and rejected.
DEFAULT_WIDE_LIMIT_M = 3.0

#: Initial length bands (m of bounce distance from the striker's stumps line),
#: junior-pitch tunable (backlog: values are starting points, not final claims).
DEFAULT_LENGTH_BANDS_M: dict[Length, tuple[float, float]] = {
    Length.YORKER: (0.0, 2.0),
    Length.FULL: (2.0, 5.0),
    Length.GOOD: (5.0, 8.0),
    Length.SHORT: (8.0, float("inf")),
}

#: Initial line channels (m of lateral offset, +y = off side for RH batter).
#: Channels are ordered leg → off; each is (lower_edge, upper_edge].
DEFAULT_LINE_CHANNELS_M: dict[Line, tuple[float, float]] = {
    Line.LEG: (float("-inf"), -0.1143),
    Line.MIDDLE: (-0.1143, 0.1143),
    Line.OFF: (0.1143, 0.40),
    Line.OUTSIDE_OFF: (0.40, float("inf")),
}


class ZoneConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ZoneConfig:
    """Validated zone map. Bands are contiguous and exhaustive over [0, inf)/(-inf, inf).

    ``clamp_epsilon_m`` is the off-pitch slack absorbed by clamping (US-C2's
    accepted calibration error), ``pitch_length_m`` the stumps-to-stumps
    length defining the far clamp edge, and ``wide_limit_m`` the largest
    plausible lateral offset magnitude; see :func:`classify_length` and
    :func:`classify_line` for the clamp/reject semantics.
    """

    length_bands_m: dict[Length, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_LENGTH_BANDS_M)
    )
    line_channels_m: dict[Line, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_LINE_CHANNELS_M)
    )
    clamp_epsilon_m: float = DEFAULT_CLAMP_EPSILON_M
    pitch_length_m: float = PITCH_LENGTH_M
    wide_limit_m: float = DEFAULT_WIDE_LIMIT_M

    def __post_init__(self) -> None:
        self._validate_bands(dict(self.length_bands_m), set(Length), start=0.0, axis="length")
        self._validate_bands(
            dict(self.line_channels_m), set(Line), start=float("-inf"), axis="line"
        )
        if not math.isfinite(self.clamp_epsilon_m) or self.clamp_epsilon_m < 0:
            raise ZoneConfigError(
                f"clamp_epsilon_m must be a finite value >= 0, got {self.clamp_epsilon_m}"
            )
        for name, value in (
            ("pitch_length_m", self.pitch_length_m),
            ("wide_limit_m", self.wide_limit_m),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ZoneConfigError(f"{name} must be a finite value > 0, got {value}")

    @staticmethod
    def _validate_bands(
        bands: dict[Any, tuple[float, float]],
        required: set[Any],
        *,
        start: float,
        axis: str,
    ) -> None:
        if set(bands) != required:
            raise ZoneConfigError(f"{axis} config must define exactly {sorted(required)}")
        ordered = sorted(bands.items(), key=lambda kv: kv[1][0])
        cursor = start
        for name, (lo, hi) in ordered:
            if lo >= hi:
                raise ZoneConfigError(f"{axis} band {name} is empty: ({lo}, {hi})")
            if lo != cursor:
                raise ZoneConfigError(
                    f"{axis} bands must be contiguous: {name} starts at {lo}, expected {cursor}"
                )
            cursor = hi
        if cursor != float("inf"):
            raise ZoneConfigError(f"{axis} bands must extend to infinity, end at {cursor}")

    # -- (de)serialization: config lives in DB/YAML as plain dicts ------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "length_bands_m": {k.value: [v[0], v[1]] for k, v in self.length_bands_m.items()},
            "line_channels_m": {k.value: [v[0], v[1]] for k, v in self.line_channels_m.items()},
            "clamp_epsilon_m": self.clamp_epsilon_m,
            "pitch_length_m": self.pitch_length_m,
            "wide_limit_m": self.wide_limit_m,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ZoneConfig:
        """Load a config dict; the clamp fields default when absent so stored
        configs predating them keep loading."""
        try:
            lengths = {
                Length(k): (float(v[0]), float(v[1])) for k, v in raw["length_bands_m"].items()
            }
            lines = {Line(k): (float(v[0]), float(v[1])) for k, v in raw["line_channels_m"].items()}
            clamp_epsilon_m = float(raw.get("clamp_epsilon_m", DEFAULT_CLAMP_EPSILON_M))
            pitch_length_m = float(raw.get("pitch_length_m", PITCH_LENGTH_M))
            wide_limit_m = float(raw.get("wide_limit_m", DEFAULT_WIDE_LIMIT_M))
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise ZoneConfigError(f"malformed zone config: {exc!r}") from exc
        return cls(
            length_bands_m=lengths,
            line_channels_m=lines,
            clamp_epsilon_m=clamp_epsilon_m,
            pitch_length_m=pitch_length_m,
            wide_limit_m=wide_limit_m,
        )


def classify_length(config: ZoneConfig, pitch_x_m: float) -> Length:
    """Length from bounce distance down the pitch.

    Boundary rule (documented, US-C5 edge tests): bands are half-open [lo, hi),
    so a ball exactly on an edge belongs to the farther band: x = 2.0 m with
    default bands is FULL, not YORKER.

    Clamp rule (US-C5 off-pitch validation): calibration error up to
    ``clamp_epsilon_m`` just off either end of the pitch is absorbed by
    clamping into [0, ``pitch_length_m``] - a yorker click mapping to
    x = -0.01 m classifies as YORKER. Beyond the epsilon the value cannot come
    from a ball on the pitch (a mis-click or a bad calibration) and raises
    :class:`ZoneConfigError`.
    """
    if not math.isfinite(pitch_x_m):
        raise ZoneConfigError(f"invalid bounce distance: x={pitch_x_m}")
    if (
        pitch_x_m < -config.clamp_epsilon_m
        or pitch_x_m > config.pitch_length_m + config.clamp_epsilon_m
    ):
        raise ZoneConfigError(
            f"bounce distance x={pitch_x_m} m is off the pitch (accepted range "
            f"[{-config.clamp_epsilon_m}, {config.pitch_length_m + config.clamp_epsilon_m}] m); "
            "likely a mis-click"
        )
    x = min(max(pitch_x_m, 0.0), config.pitch_length_m)
    ordered = sorted(config.length_bands_m.items(), key=lambda kv: kv[1][0])
    # Bands are validated contiguous over [0, inf), so this is total for finite x.
    return next(length for length, (_lo, hi) in ordered if x < hi)


def classify_line(config: ZoneConfig, pitch_y_m: float, handedness: Handedness) -> Line:
    """Line channel from lateral offset; LH batters mirror the y axis (US-C5).

    Same half-open boundary rule as lengths: an edge value belongs to the more
    off-side channel (y = 0.1143 m is OFF, not MIDDLE, with default channels).

    Offsets with ``abs(y)`` beyond ``wide_limit_m`` are implausibly wide for a
    delivery and raise :class:`ZoneConfigError` (mis-click validation, US-C5).
    """
    if not math.isfinite(pitch_y_m):
        raise ZoneConfigError(f"invalid lateral offset: y={pitch_y_m}")
    if abs(pitch_y_m) > config.wide_limit_m:
        raise ZoneConfigError(
            f"lateral offset y={pitch_y_m} m is implausibly wide "
            f"(|y| must be <= {config.wide_limit_m} m); likely a mis-click"
        )
    y = pitch_y_m if handedness is Handedness.RIGHT else -pitch_y_m
    ordered = sorted(config.line_channels_m.items(), key=lambda kv: kv[1][0])
    return next(line for line, (_lo, hi) in ordered if y < hi)


def classify(
    config: ZoneConfig, pitch_x_m: float, pitch_y_m: float, handedness: Handedness
) -> tuple[Line, Length]:
    return classify_line(config, pitch_y_m, handedness), classify_length(config, pitch_x_m)
