"""US-C6: deterministic top-down pitch-map PNG rendering."""

import io

import numpy as np
import pytest
from cricai_data.enums import Length, Line
from cricai_vision.heatmap import render_pitch_map
from cricai_vision.zones import ZoneConfig
from matplotlib import image as mpimg

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _red_columns(png: bytes) -> list[int]:
    """Pixel columns of the bounce-point markers (#d62728: red high, green/blue low)."""
    rgba = mpimg.imread(io.BytesIO(png))
    mask = (rgba[..., 0] > 0.7) & (rgba[..., 1] < 0.35) & (rgba[..., 2] < 0.35)
    return [int(col) for col in np.nonzero(mask)[1]]


def test_png_signature_and_nontrivial_size() -> None:
    png = render_pitch_map({("off", "good"): 5})
    assert png.startswith(PNG_SIGNATURE)
    assert len(png) > 5_000


def test_same_dataset_renders_identical_bytes() -> None:
    counts = {("off", "good"): 5, ("leg", "short"): 2}
    assert render_pitch_map(counts, title="s1") == render_pitch_map(counts, title="s1")


def test_different_datasets_render_different_bytes() -> None:
    assert render_pitch_map({("off", "good"): 5}) != render_pitch_map({("off", "good"): 6})


def test_empty_counts_render_all_zero_cells() -> None:
    png = render_pitch_map({})
    assert png.startswith(PNG_SIGNATURE)


def test_every_canonical_zone_key_is_accepted() -> None:
    counts = {(line.value, length.value): 1 for line in Line for length in Length}
    assert render_pitch_map(counts).startswith(PNG_SIGNATURE)


@pytest.mark.parametrize(
    "key",
    [
        ("wide", "good"),  # unknown line
        ("off", "beamer"),  # unknown length
        ("", ""),
    ],
)
def test_unknown_zone_key_raises_value_error(key: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="unknown zone key"):
        render_pitch_map({key: 1})


def test_title_changes_output() -> None:
    counts = {("middle", "yorker"): 3}
    assert render_pitch_map(counts, title="Session 1") != render_pitch_map(counts)


def test_points_change_output() -> None:
    counts = {("off", "good"): 1}
    with_points = render_pitch_map(counts, points=[(6.5, 0.25)])
    assert with_points.startswith(PNG_SIGNATURE)
    assert with_points != render_pitch_map(counts)


def test_points_render_to_scale_on_pitch_axes() -> None:
    """A mark further down the pitch lands further along the rendered x axis."""
    near = _red_columns(render_pitch_map({}, points=[(2.0, 0.0)]))
    far = _red_columns(render_pitch_map({}, points=[(18.0, 0.0)]))
    assert near and far
    assert sum(near) / len(near) < sum(far) / len(far)


def test_points_outside_cells_still_render() -> None:
    # Beyond the last length band's pitch clip and wider than any line channel:
    # no shaded cell contains these marks, yet they must still be drawn.
    outside = [(20.5, 0.0), (6.5, -1.7)]
    png = render_pitch_map({}, points=outside)
    assert png.startswith(PNG_SIGNATURE)
    assert png != render_pitch_map({})
    assert _red_columns(png)  # markers actually drawn, not clipped away


def test_points_determinism() -> None:
    counts = {("middle", "full"): 2}
    points = [(3.2, 0.05), (4.1, -0.1)]
    assert render_pitch_map(counts, points=points) == render_pitch_map(counts, points=points)


def test_empty_points_render_like_no_points() -> None:
    counts = {("off", "good"): 1}
    assert render_pitch_map(counts, points=[]) == render_pitch_map(counts)


def test_zone_boundaries_come_from_config_not_code() -> None:
    counts = {("middle", "yorker"): 3}
    widened = ZoneConfig(
        length_bands_m={
            Length.YORKER: (0.0, 3.0),
            Length.FULL: (3.0, 6.0),
            Length.GOOD: (6.0, 9.0),
            Length.SHORT: (9.0, float("inf")),
        }
    )
    assert render_pitch_map(counts, config=widened) != render_pitch_map(counts)
