"""Top-down pitch heatmap rendering (US-C6).

Pure rendering: aggregation happens in the API layer, this module turns a
``{(line, length): count}`` mapping into a PNG of a regulation pitch
(20.12 x 3.05 m, creases per MCC law) with zone cells shaded by density and
annotated with counts.

Zone cell edges come from :class:`cricai_vision.zones.ZoneConfig` (boundaries
are configuration, never hardcoded); the infinite outer bands are clipped to
the pitch rectangle. Cells are drawn in the canonical right-hand frame
(+x toward the bowler, +y toward the off side); zone-level counts are already
handedness-resolved by the classifier, so no mirroring happens here.

Rendering is deterministic (daily-report snapshots diff PNG bytes): the Agg
backend is forced before pyplot is imported and ``savefig`` passes
``metadata={}`` so no timestamps are embedded.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence

import matplotlib

matplotlib.use("Agg")  # headless + deterministic; must precede pyplot import

import matplotlib.pyplot as plt
from cricai_data.enums import Length, Line
from matplotlib import colormaps
from matplotlib.axes import Axes
from matplotlib.patches import Rectangle

from cricai_vision.geometry import (
    PITCH_LENGTH_M,
    PITCH_WIDTH_M,
    POPPING_CREASE_OFFSET_M,
    RETURN_CREASE_HALF_SPAN_M,
)
from cricai_vision.zones import ZoneConfig

#: Brand-neutral sequential colormap for density shading.
_COLORMAP = colormaps["viridis"]

_HALF_WIDTH_M = PITCH_WIDTH_M / 2


def _validate_zone_keys(zone_counts: Mapping[tuple[str, str], int]) -> None:
    """Reject keys outside the canonical Line/Length vocabulary."""
    known_lines = {line.value for line in Line}
    known_lengths = {length.value for length in Length}
    for line_key, length_key in zone_counts:
        if line_key not in known_lines or length_key not in known_lengths:
            raise ValueError(f"unknown zone key: ({line_key!r}, {length_key!r})")


def _clip_x(x_m: float) -> float:
    return min(max(x_m, 0.0), PITCH_LENGTH_M)


def _clip_y(y_m: float) -> float:
    return min(max(y_m, -_HALF_WIDTH_M), _HALF_WIDTH_M)


def _draw_cells(ax: Axes, zone_counts: Mapping[tuple[str, str], int], config: ZoneConfig) -> None:
    """Shade every zone cell by density and annotate it with its ball count."""
    peak = max(zone_counts.values(), default=0)
    scale = float(peak) if peak > 0 else 1.0
    for line in Line:
        y_lo, y_hi = (_clip_y(edge) for edge in config.line_channels_m[line])
        for length in Length:
            x_lo, x_hi = (_clip_x(edge) for edge in config.length_bands_m[length])
            count = zone_counts.get((line.value, length.value), 0)
            density = count / scale
            ax.add_patch(
                Rectangle(
                    (x_lo, y_lo),
                    x_hi - x_lo,
                    y_hi - y_lo,
                    facecolor=_COLORMAP(density),
                    edgecolor="white",
                    linewidth=0.6,
                )
            )
            ax.text(
                (x_lo + x_hi) / 2,
                (y_lo + y_hi) / 2,
                str(count),
                ha="center",
                va="center",
                fontsize=9,
                # viridis runs dark -> bright, so flip text for contrast.
                color="black" if density > 0.5 else "white",
            )


def _draw_points(ax: Axes, points: Sequence[tuple[float, float]]) -> None:
    """Scatter individual bounce marks over the shaded cells, to pitch scale.

    Marks are drawn in data coordinates (m) so they sit exactly where the ball
    bounced, visually distinct from the cell shading (red dot, white rim, above
    cells and creases). ``clip_on=False`` keeps marks visible even when they
    fall outside every zone cell (e.g. beyond the pitch rectangle).
    """
    ax.scatter(
        [x for x, _y in points],
        [y for _x, y in points],
        s=28.0,
        marker="o",
        facecolor="#d62728",
        edgecolor="white",
        linewidths=0.8,
        zorder=5,
        clip_on=False,
    )


def _draw_creases(ax: Axes) -> None:
    """Pitch outline plus bowling/popping/return creases at both ends."""
    ax.add_patch(
        Rectangle(
            (0.0, -_HALF_WIDTH_M),
            PITCH_LENGTH_M,
            PITCH_WIDTH_M,
            fill=False,
            edgecolor="black",
            linewidth=1.2,
        )
    )

    def crease(xs: tuple[float, float], ys: tuple[float, float]) -> None:
        ax.plot(xs, ys, color="white", linewidth=1.4)

    for stumps_x, toward_middle in ((0.0, 1.0), (PITCH_LENGTH_M, -1.0)):
        popping_x = stumps_x + toward_middle * POPPING_CREASE_OFFSET_M
        span = RETURN_CREASE_HALF_SPAN_M
        crease((stumps_x, stumps_x), (-span, span))  # bowling crease
        crease((popping_x, popping_x), (-_HALF_WIDTH_M, _HALF_WIDTH_M))  # popping crease
        for side in (-1.0, 1.0):  # return creases
            crease((stumps_x, popping_x), (side * span, side * span))


def render_pitch_map(
    zone_counts: Mapping[tuple[str, str], int],
    *,
    title: str = "",
    config: ZoneConfig | None = None,
    points: Sequence[tuple[float, float]] | None = None,
) -> bytes:
    """Render zone-level ball counts onto a top-down pitch graphic as PNG bytes.

    ``zone_counts`` keys are ``(line_value, length_value)`` pairs from the
    canonical :class:`Line`/:class:`Length` vocabularies; unknown keys raise
    ``ValueError``. Missing cells render as zero. ``config`` supplies the zone
    boundaries (defaults to :class:`ZoneConfig`). ``points`` are individual
    bounce ``(pitch_x_m, pitch_y_m)`` marks scattered to scale over the shaded
    cells (US-C6: every tagged bounce point renders at its true position, even
    outside the zone cells).
    """
    _validate_zone_keys(zone_counts)
    zone_config = config if config is not None else ZoneConfig()

    fig, ax = plt.subplots(figsize=(12.0, 3.4))
    try:
        _draw_cells(ax, zone_counts, zone_config)
        _draw_creases(ax)
        if points:
            _draw_points(ax, points)
        ax.set_xlim(-0.6, PITCH_LENGTH_M + 0.6)
        ax.set_ylim(-_HALF_WIDTH_M - 0.4, _HALF_WIDTH_M + 0.4)
        ax.set_aspect("equal")
        ax.set_xlabel("bounce distance from striker stumps (m)")
        ax.set_ylabel("off side + (m)")
        if title:
            ax.set_title(title)
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", metadata={})  # no timestamps: deterministic
        return buffer.getvalue()
    finally:
        plt.close(fig)
