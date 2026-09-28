"""US-C2: pixel <-> pitch homography from clicked landmarks (extrinsics)."""

import json
import math
from collections.abc import Sequence
from typing import Final

import numpy as np
import pytest
from cricai_vision.extrinsics import (
    MIN_LANDMARKS,
    PARAMS_VERSION,
    ExtrinsicsError,
    InsufficientLandmarksError,
    PlaneCalibration,
    draw_pitch_map,
    fit_homography,
    from_params,
    pitch_xy_to_pixel,
    pixel_to_pitch_xy,
    to_params,
)
from cricai_vision.geometry import (
    PITCH_LENGTH_M,
    POPPING_CREASE_OFFSET_M,
    PinholeCamera,
    landmark_catalog,
    make_overhead_camera,
)

Pair = tuple[float, float]

CATALOG: Final = landmark_catalog()
NAMES: Final = sorted(CATALOG)

#: Center of the good-length region, used for the noise-tolerance criterion.
PITCH_CENTER_XY: Final = (PITCH_LENGTH_M / 2, 0.0)

#: The six stump bases: y-spread 0.2286 m against a 20.12 m x-spread, a nearly
#: collinear set (principal-axis spread ratio ~ 0.009) that must be rejected.
STUMP_BASE_NAMES: Final = (
    "striker_middle_stump_base",
    "striker_off_stump_base",
    "striker_leg_stump_base",
    "bowler_middle_stump_base",
    "bowler_off_stump_base",
    "bowler_leg_stump_base",
)

#: Six points including popping-crease corners (y-spread 2.64 m >= 1.5 m):
#: well-conditioned (spread ratio ~ 0.10 pitch / ~ 0.053 pixel), must fit.
CREASE_SET_NAMES: Final = (
    "striker_middle_stump_base",
    "striker_off_stump_base",
    "striker_leg_stump_base",
    "bowler_middle_stump_base",
    "striker_popping_crease_off",
    "striker_popping_crease_leg",
)


def _named_correspondences(names: Sequence[str]) -> tuple[list[Pair], list[Pair]]:
    """Project the named catalog landmarks through the default synthetic camera."""
    pitch = [CATALOG[name].xy for name in names]
    projected = make_overhead_camera().project_pitch_xy(np.array(pitch))
    pixels = [(float(u), float(v)) for u, v in projected]
    return pixels, pitch


def _correspondences(camera: PinholeCamera) -> tuple[list[Pair], list[Pair]]:
    """Project every catalog landmark through a synthetic camera."""
    pitch = [CATALOG[name].xy for name in NAMES]
    projected = camera.project_pitch_xy(np.array(pitch))
    pixels = [(float(u), float(v)) for u, v in projected]
    return pixels, pitch


@pytest.fixture(scope="module")
def exact_pairs() -> tuple[list[Pair], list[Pair]]:
    return _correspondences(make_overhead_camera())


@pytest.fixture(scope="module")
def exact_cal(exact_pairs: tuple[list[Pair], list[Pair]]) -> PlaneCalibration:
    pixels, pitch = exact_pairs
    return fit_homography(pixels, pitch)


# ---------------------------------------------------------------------------
# Exact synthetic scene: fit + both mapping directions
# ---------------------------------------------------------------------------


def test_exact_scene_round_trip(
    exact_pairs: tuple[list[Pair], list[Pair]], exact_cal: PlaneCalibration
) -> None:
    pixels, pitch = exact_pairs
    for px, xy in zip(pixels, pitch, strict=True):
        assert pixel_to_pitch_xy(exact_cal, px) == pytest.approx(xy, abs=1e-6)
        assert pitch_xy_to_pixel(exact_cal, xy) == pytest.approx(px, abs=1e-6)
    assert exact_cal.rms_px < 1e-9
    assert exact_cal.rms_m < 1e-9
    assert exact_cal.n_landmarks == len(NAMES)


def test_calibration_matrix_is_3x3(exact_cal: PlaneCalibration) -> None:
    assert len(exact_cal.matrix) == 3
    assert all(len(row) == 3 for row in exact_cal.matrix)
    assert all(isinstance(value, float) for row in exact_cal.matrix for value in row)


def test_fit_is_deterministic(exact_pairs: tuple[list[Pair], list[Pair]]) -> None:
    """REG: the same landmark-click fixture reproduces the identical homography."""
    pixels, pitch = exact_pairs
    assert fit_homography(pixels, pitch) == fit_homography(pixels, pitch)


def test_fit_succeeds_with_exactly_min_landmarks(
    exact_pairs: tuple[list[Pair], list[Pair]],
) -> None:
    pixels, pitch = exact_pairs
    cal = fit_homography(pixels[:MIN_LANDMARKS], pitch[:MIN_LANDMARKS])
    assert cal.n_landmarks == MIN_LANDMARKS
    assert cal.rms_m < 1e-9


# ---------------------------------------------------------------------------
# Coordinate frame contract (US-C2 AC: documented signs)
# ---------------------------------------------------------------------------


def test_coordinate_frame_contract(
    exact_pairs: tuple[list[Pair], list[Pair]], exact_cal: PlaneCalibration
) -> None:
    pixels, _pitch = exact_pairs
    # Origin: middle-stump base at the striker's end.
    origin_px = pixels[NAMES.index("striker_middle_stump_base")]
    assert pixel_to_pitch_xy(exact_cal, origin_px) == pytest.approx((0.0, 0.0), abs=1e-6)
    # +x toward the bowler: the bowler-end middle stump is 20.12 m down the pitch.
    bowler_px = pixels[NAMES.index("bowler_middle_stump_base")]
    bowler_x, bowler_y = pixel_to_pitch_xy(exact_cal, bowler_px)
    assert bowler_x == pytest.approx(PITCH_LENGTH_M, abs=1e-6)
    assert bowler_y == pytest.approx(0.0, abs=1e-6)
    # +y toward the off side for a right-hand batter; leg side is negative.
    off_y = pixel_to_pitch_xy(exact_cal, pixels[NAMES.index("striker_off_stump_base")])[1]
    assert off_y > 0
    leg_y = pixel_to_pitch_xy(exact_cal, pixels[NAMES.index("striker_leg_stump_base")])[1]
    assert leg_y < 0
    # Popping crease sits 1.22 m in front of the striker's stumps line.
    crease_px = pixels[NAMES.index("striker_popping_crease_center")]
    assert pixel_to_pitch_xy(exact_cal, crease_px)[0] == pytest.approx(
        POPPING_CREASE_OFFSET_M, abs=1e-6
    )


# ---------------------------------------------------------------------------
# Noise tolerance
# ---------------------------------------------------------------------------


def test_noise_tolerance_half_pixel_sigma(exact_pairs: tuple[list[Pair], list[Pair]]) -> None:
    """Gaussian click noise (sigma = 0.5 px) keeps mapping error << 5 cm."""
    pixels, pitch = exact_pairs
    camera = make_overhead_camera()
    rng = np.random.default_rng(42)
    noisy = [(u + float(rng.normal(0.0, 0.5)), v + float(rng.normal(0.0, 0.5))) for u, v in pixels]
    cal = fit_homography(noisy, pitch)
    assert cal.rms_m < 0.05
    assert cal.rms_px < 2.0
    center_u, center_v = camera.project_pitch_xy(np.array([PITCH_CENTER_XY]))[0]
    mapped_x, mapped_y = pixel_to_pitch_xy(cal, (float(center_u), float(center_v)))
    error_m = math.hypot(mapped_x - PITCH_CENTER_XY[0], mapped_y - PITCH_CENTER_XY[1])
    assert error_m < 0.05


# ---------------------------------------------------------------------------
# Conditioning guard: nearly collinear landmark sets are rejected (REVIEW FIX)
# ---------------------------------------------------------------------------


def test_stump_bases_only_rejected_as_nearly_collinear() -> None:
    """The six stump bases hug the pitch centerline (y-spread 0.2286 m over a
    20.12 m x-spread): the DLT solves, but the fit must be rejected."""
    pixels, pitch = _named_correspondences(STUMP_BASE_NAMES)
    with pytest.raises(ExtrinsicsError, match="nearly collinear") as excinfo:
        fit_homography(pixels, pitch)
    message = str(excinfo.value)
    assert "spread ratio" in message
    assert "crease/edge" in message


def test_crease_corner_set_still_fits_exactly() -> None:
    """A 6-point set including popping-crease corners (y-spread 2.64 m) clears
    the conditioning guard and fits the synthetic scene exactly."""
    pixels, pitch = _named_correspondences(CREASE_SET_NAMES)
    cal = fit_homography(pixels, pitch)
    assert cal.rms_m < 1e-9
    assert cal.rms_px < 1e-6
    for px, xy in zip(pixels, pitch, strict=True):
        assert pixel_to_pitch_xy(cal, px) == pytest.approx(xy, abs=1e-6)


def test_guard_prevents_confidently_wrong_noisy_stump_fit() -> None:
    """Noise reproduction of the finding: under 0.5 px click noise the stump
    bases used to fit 'successfully' with rms_m ~ 0.5 cm while the true mapping
    error at (6.0, 0.35) m exceeded 10 cm. The guard rejects that fit outright,
    while the crease set under the identical noise fits AND maps accurately."""
    camera = make_overhead_camera()
    stump_pixels, stump_pitch = _named_correspondences(STUMP_BASE_NAMES)
    rng = np.random.default_rng(7)
    noisy_stumps = [
        (u + float(rng.normal(0.0, 0.5)), v + float(rng.normal(0.0, 0.5))) for u, v in stump_pixels
    ]
    with pytest.raises(ExtrinsicsError, match="nearly collinear"):
        fit_homography(noisy_stumps, stump_pitch)

    crease_pixels, crease_pitch = _named_correspondences(CREASE_SET_NAMES)
    rng = np.random.default_rng(7)
    noisy_crease = [
        (u + float(rng.normal(0.0, 0.5)), v + float(rng.normal(0.0, 0.5))) for u, v in crease_pixels
    ]
    cal = fit_homography(noisy_crease, crease_pitch)
    probe_xy = (6.0, 0.35)
    probe_u, probe_v = camera.project_pitch_xy(np.array([probe_xy]))[0]
    mapped_x, mapped_y = pixel_to_pitch_xy(cal, (float(probe_u), float(probe_v)))
    assert math.hypot(mapped_x - probe_xy[0], mapped_y - probe_xy[1]) < 0.05


# ---------------------------------------------------------------------------
# Holdout semantics: rms_m measured on the held-out pairs only
# ---------------------------------------------------------------------------


def test_holdout_rms_measured_on_heldout_pairs_only(
    exact_pairs: tuple[list[Pair], list[Pair]],
) -> None:
    pixels, pitch = exact_pairs
    holdout = 4
    n_fit = len(pixels) - holdout
    # Fit pairs stay exact; every held-out pair's pitch position is shifted
    # 0.1 m down the pitch, so its true mapping error is exactly 0.1 m.
    shifted = [(x + 0.1, y) if i >= n_fit else (x, y) for i, (x, y) in enumerate(pitch)]
    cal = fit_homography(pixels, shifted, holdout=holdout)
    # RMS over the 4 held-out pairs is 0.1 m; over all 16 it would be 0.05 m.
    assert cal.rms_m == pytest.approx(0.1, rel=1e-6)
    assert cal.rms_m > 0.09
    assert cal.n_landmarks == len(pixels)
    # The held-out pairs did not participate in the fit: fit pairs map exactly.
    assert pixel_to_pitch_xy(cal, pixels[0]) == pytest.approx(pitch[0], abs=1e-9)


def test_holdout_can_leave_exactly_min_fit_pairs(
    exact_pairs: tuple[list[Pair], list[Pair]],
) -> None:
    pixels, pitch = exact_pairs
    cal = fit_homography(pixels, pitch, holdout=len(pixels) - MIN_LANDMARKS)
    assert cal.rms_m < 1e-9  # held-out pairs are exact under the fitted map


# ---------------------------------------------------------------------------
# Input validation and degenerate configurations
# ---------------------------------------------------------------------------


def test_error_hierarchy() -> None:
    assert issubclass(InsufficientLandmarksError, ExtrinsicsError)
    assert issubclass(ExtrinsicsError, ValueError)


def test_too_few_pairs_raise(exact_pairs: tuple[list[Pair], list[Pair]]) -> None:
    pixels, pitch = exact_pairs
    n = MIN_LANDMARKS - 1
    with pytest.raises(InsufficientLandmarksError):
        fit_homography(pixels[:n], pitch[:n])


def test_holdout_leaving_too_few_fit_pairs_raises(
    exact_pairs: tuple[list[Pair], list[Pair]],
) -> None:
    pixels, pitch = exact_pairs
    with pytest.raises(InsufficientLandmarksError):
        fit_homography(pixels, pitch, holdout=len(pixels) - MIN_LANDMARKS + 1)


def test_mismatched_lengths_raise(exact_pairs: tuple[list[Pair], list[Pair]]) -> None:
    pixels, pitch = exact_pairs
    with pytest.raises(ExtrinsicsError, match="lengths differ"):
        fit_homography(pixels[:-1], pitch)


def test_negative_holdout_raises(exact_pairs: tuple[list[Pair], list[Pair]]) -> None:
    pixels, pitch = exact_pairs
    with pytest.raises(ExtrinsicsError, match="holdout"):
        fit_homography(pixels, pitch, holdout=-1)


def test_non_finite_input_raises(exact_pairs: tuple[list[Pair], list[Pair]]) -> None:
    pixels, pitch = exact_pairs
    corrupted = [(math.nan, pixels[0][1]), *pixels[1:]]
    with pytest.raises(ExtrinsicsError, match="non-finite"):
        fit_homography(corrupted, pitch)


def test_coincident_pixels_raise() -> None:
    pitch = [CATALOG[name].xy for name in NAMES[:MIN_LANDMARKS]]
    pixels = [(100.0, 200.0)] * MIN_LANDMARKS
    with pytest.raises(ExtrinsicsError, match="coincident"):
        fit_homography(pixels, pitch)


def test_collinear_landmarks_raise() -> None:
    camera = make_overhead_camera()
    pitch = [(float(i), 0.0) for i in range(8)]
    pixels = [(float(u), float(v)) for u, v in camera.project_pitch_xy(np.array(pitch))]
    with pytest.raises(ExtrinsicsError, match="not uniquely determined"):
        fit_homography(pixels, pitch)


def test_collinear_pitch_targets_raise_singular(
    exact_pairs: tuple[list[Pair], list[Pair]],
) -> None:
    """Pixels in general position paired with collinear pitch targets admit
    only a plane-to-line (singular) homography, which must be rejected."""
    pixels, _pitch = exact_pairs
    collinear_pitch = [(float(i), 0.0) for i in range(8)]
    with pytest.raises(ExtrinsicsError, match="singular"):
        fit_homography(pixels[:8], collinear_pitch)


def test_pixel_on_mapping_horizon_raises(exact_cal: PlaneCalibration) -> None:
    matrix = np.asarray(exact_cal.matrix, dtype=np.float64)
    assert abs(matrix[2, 1]) > 1e-12  # tilted camera: the horizon is reachable
    u = 960.0
    v = float(-(matrix[2, 0] * u + matrix[2, 2]) / matrix[2, 1])
    with pytest.raises(ExtrinsicsError, match="horizon"):
        pixel_to_pitch_xy(exact_cal, (u, v))


# ---------------------------------------------------------------------------
# Params (Calibration.params JSON payload) round-trip and validation
# ---------------------------------------------------------------------------


def test_params_round_trip_through_json(exact_cal: PlaneCalibration) -> None:
    params = to_params(exact_cal)
    assert params["version"] == PARAMS_VERSION
    assert set(params) == {"version", "matrix", "rms_px", "rms_m", "n_landmarks"}
    loaded = from_params(json.loads(json.dumps(params)))
    assert loaded == exact_cal
    # The loaded calibration is immediately usable in both directions.
    xy = pixel_to_pitch_xy(loaded, (960.0, 540.0))
    assert pitch_xy_to_pixel(loaded, xy) == pytest.approx((960.0, 540.0), abs=1e-6)


_REMOVE: Final = object()

_IDENTITY: Final = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", _REMOVE),
        ("version", 2),
        ("matrix", _REMOVE),
        ("matrix", "not a matrix"),
        ("matrix", [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
        ("matrix", [[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]),
        ("matrix", ["abc", "def", "ghi"]),
        ("matrix", [[1.0, 0.0, "x"], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
        ("matrix", [[True, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
        ("matrix", [[math.nan, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
        ("matrix", [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]]),
        ("rms_px", _REMOVE),
        ("rms_px", "high"),
        ("rms_m", -0.1),
        ("n_landmarks", "8"),
        ("n_landmarks", True),
        ("n_landmarks", 7.0),
        ("n_landmarks", MIN_LANDMARKS - 1),
    ],
)
def test_from_params_rejects_malformed(
    field: str, value: object, exact_cal: PlaneCalibration
) -> None:
    params = to_params(exact_cal)
    if value is _REMOVE:
        del params[field]
    else:
        params[field] = value
    with pytest.raises(ExtrinsicsError):
        from_params(params)


def test_from_params_ignores_unknown_extra_keys(exact_cal: PlaneCalibration) -> None:
    """Forward compat (REVIEW FIX): the calibration service stores extra keys
    (e.g. 'intrinsics_id' for traceability) alongside the fit payload; loading
    must ignore them while still strictly validating the known keys."""
    params = to_params(exact_cal)
    params["intrinsics_id"] = "6f6e3f1e-0000-4000-8000-000000000000"
    params["operator_note"] = {"nested": ["ignored", 1]}
    assert from_params(json.loads(json.dumps(params))) == exact_cal
    # Known-key corruption is still rejected even with extras present.
    params["rms_m"] = -1.0
    with pytest.raises(ExtrinsicsError, match="rms"):
        from_params(params)


def test_from_params_accepts_integer_matrix_entries() -> None:
    params = {
        "version": PARAMS_VERSION,
        "matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "rms_px": 0.5,
        "rms_m": 0.01,
        "n_landmarks": MIN_LANDMARKS,
    }
    loaded = from_params(params)
    assert loaded.matrix == tuple(tuple(float(v) for v in row) for row in _IDENTITY)
    assert pixel_to_pitch_xy(loaded, (3.0, 4.0)) == pytest.approx((3.0, 4.0))


# ---------------------------------------------------------------------------
# Verification overlay
# ---------------------------------------------------------------------------


def test_draw_pitch_map_marks_landmarks_and_grid() -> None:
    # A wide-angle framing that keeps every landmark inside a 640x360 image.
    camera = make_overhead_camera(focal_px=200.0, cx=320.0, cy=180.0)
    pixels, pitch = _correspondences(camera)
    cal = fit_homography(pixels, pitch)
    width, height = 640, 360
    grid_only = draw_pitch_map(cal, (width, height), [])
    image = draw_pitch_map(cal, (width, height), pixels)
    assert image.shape == (height, width, 3)
    assert image.dtype == np.uint8
    background = image[0, 0].tolist()
    assert grid_only[0, 0].tolist() == background
    for u, v in pixels:
        assert 0 <= round(u) < width
        assert 0 <= round(v) < height
        assert image[round(v), round(u)].tolist() != background
    # The reprojected pitch grid is painted even without landmarks ...
    assert len(np.unique(grid_only.reshape(-1, 3), axis=0)) > 1
    # ... and landmark markers add on top of it.
    assert not np.array_equal(image, grid_only)


def test_draw_pitch_map_rejects_empty_image(exact_cal: PlaneCalibration) -> None:
    with pytest.raises(ExtrinsicsError, match="image_size"):
        draw_pitch_map(exact_cal, (0, 360), [])
