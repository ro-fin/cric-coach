"""Pitch (extrinsic) calibration: pixel plane <-> pitch plane homography (US-C2).

Fits a 3x3 homography mapping undistorted pixel coordinates to pitch-plane
coordinates in the canonical frame (see :mod:`cricai_vision.geometry`: origin =
middle-stump base at the striker's end; +x toward the bowler down the pitch;
+y toward the off side for a right-hand batter; meters) from >= 6 clicked
landmark correspondences. The fit is a deterministic normalized DLT (Hartley
normalization + SVD) with no RANSAC, so a given landmark-click fixture always
reproduces the identical homography.

``PlaneCalibration.matrix`` is the row-major pixel->pitch homography;
:func:`to_params` / :func:`from_params` round-trip the JSON payload stored in
``cricai_data.models.Calibration.params`` for ``kind=extrinsic``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import cv2
import numpy as np
import numpy.typing as npt

from cricai_vision.geometry import (
    PITCH_LENGTH_M,
    PITCH_WIDTH_M,
    POPPING_CREASE_OFFSET_M,
)

#: Version tag of the ``Calibration.params`` JSON payload for kind=extrinsic.
PARAMS_VERSION: Final = 1

#: US-C2 acceptance criterion: the operator clicks at least 6 known landmarks.
MIN_LANDMARKS: Final = 6

#: Relative singular-value tolerance below which a system/matrix is treated as
#: rank-deficient (collinear landmarks, singular homography).
_RANK_RTOL: Final = 1e-9

#: Minimum principal-axis spread ratio (second/first singular value of the
#: mean-centered fit coordinates) required of BOTH the pitch and the pixel
#: landmark sets. Below it the DLT still solves, but the fit is so
#: ill-conditioned across the narrow axis that a confidently-wrong calibration
#: results: the six stump bases (0.23 m y-spread over a 20.12 m pitch) report
#: rms_m ~ 0.5 cm while the real mapping error mid-pitch exceeds 10 cm under
#: half-pixel click noise.
_MIN_SPREAD_RATIO: Final = 0.05

#: |w| below which a homogeneous point is considered at infinity (the mapping
#: horizon). The fitted matrix is normalized to unit Frobenius norm, so this
#: threshold is scale-independent across calibrations.
_W_EPSILON: Final = 1e-9

_BACKGROUND_BGR: Final = (20, 60, 20)
_GRID_BGR: Final = (255, 255, 255)
_LANDMARK_BGR: Final = (0, 0, 255)
_LANDMARK_RING_BGR: Final = (0, 255, 255)


class ExtrinsicsError(ValueError):
    """Invalid landmark input, malformed params, or a degenerate calibration."""


class InsufficientLandmarksError(ExtrinsicsError):
    """Fewer than :data:`MIN_LANDMARKS` landmark pairs available for the fit."""


@dataclass(frozen=True)
class PlaneCalibration:
    """A fitted pixel->pitch plane homography with its fit-quality report.

    ``matrix`` is the 3x3 row-major pixel->pitch homography (unit Frobenius
    norm, deterministic sign). ``rms_px`` is the RMS reprojection error in
    pixels over all landmark pairs, mapping pitch points back through the
    inverse homography. ``rms_m`` is the RMS mapping error in meters, measured
    on the held-out pairs when the fit used a holdout, otherwise on all pairs.
    ``n_landmarks`` is the total number of landmark pairs provided.
    """

    matrix: tuple[tuple[float, float, float], ...]
    rms_px: float
    rms_m: float
    n_landmarks: int


def _as_points(points: Sequence[tuple[float, float]], name: str) -> npt.NDArray[np.float64]:
    """Copy a sequence of (x, y) pairs into an Nx2 float array, or raise."""
    array = np.array(
        [(float(point[0]), float(point[1])) for point in points], dtype=np.float64
    ).reshape(len(points), 2)
    if not np.all(np.isfinite(array)):
        raise ExtrinsicsError(f"{name} contains non-finite coordinates")
    return array


def _apply_homography(
    matrix: npt.NDArray[np.float64], point: tuple[float, float]
) -> tuple[float, float]:
    """Map one 2-D point through a 3x3 homography (homogeneous divide)."""
    u, v = float(point[0]), float(point[1])
    w = float(matrix[2, 0]) * u + float(matrix[2, 1]) * v + float(matrix[2, 2])
    if abs(w) < _W_EPSILON:
        raise ExtrinsicsError(f"point {point!r} lies on the mapping horizon (w ~ 0)")
    x = (float(matrix[0, 0]) * u + float(matrix[0, 1]) * v + float(matrix[0, 2])) / w
    y = (float(matrix[1, 0]) * u + float(matrix[1, 1]) * v + float(matrix[1, 2])) / w
    return (x, y)


def _rms_error(
    matrix: npt.NDArray[np.float64],
    src: npt.NDArray[np.float64],
    dst: npt.NDArray[np.float64],
) -> float:
    """RMS euclidean error of mapping ``src`` through ``matrix`` vs ``dst``."""
    total = 0.0
    for src_point, dst_point in zip(src, dst, strict=True):
        x, y = _apply_homography(matrix, (float(src_point[0]), float(src_point[1])))
        total += (x - float(dst_point[0])) ** 2 + (y - float(dst_point[1])) ** 2
    return math.sqrt(total / len(src))


def _hartley_normalization(
    points: npt.NDArray[np.float64], label: str
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Similarity transform to centroid 0 / mean distance sqrt(2), plus the
    normalized points. Raises for coincident (zero-spread) point sets."""
    centroid = points.mean(axis=0)
    mean_dist = float(np.linalg.norm(points - centroid, axis=1).mean())
    if mean_dist < 1e-12:
        raise ExtrinsicsError(f"degenerate {label} landmarks: points are coincident")
    scale = math.sqrt(2.0) / mean_dist
    transform = np.array(
        [
            [scale, 0.0, -scale * float(centroid[0])],
            [0.0, scale, -scale * float(centroid[1])],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return transform, (points - centroid) * scale


def _fit_matrix(
    pixels: npt.NDArray[np.float64], pitch: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    """Normalized DLT: solve for the pixel->pitch homography via SVD."""
    transform_px, norm_px = _hartley_normalization(pixels, "pixel")
    transform_xy, norm_xy = _hartley_normalization(pitch, "pitch")
    n = len(norm_px)
    system = np.zeros((2 * n, 9), dtype=np.float64)
    for i in range(n):
        u, v = norm_px[i]
        x, y = norm_xy[i]
        system[2 * i] = (u, v, 1.0, 0.0, 0.0, 0.0, -x * u, -x * v, -x)
        system[2 * i + 1] = (0.0, 0.0, 0.0, u, v, 1.0, -y * u, -y * v, -y)
    _basis, singular_values, vt = np.linalg.svd(system)
    if singular_values[-2] <= _RANK_RTOL * singular_values[0]:
        raise ExtrinsicsError(
            "degenerate landmark configuration: homography is not uniquely "
            "determined (collinear or repeated landmarks)"
        )
    matrix: npt.NDArray[np.float64] = (
        np.linalg.inv(transform_xy) @ vt[-1].reshape(3, 3) @ transform_px
    )
    # Deterministic representation: unit Frobenius norm, largest element positive.
    matrix /= float(np.linalg.norm(matrix))
    matrix *= float(np.sign(matrix.reshape(-1)[int(np.argmax(np.abs(matrix)))]))
    _require_invertible(matrix, "fitted homography is singular (collinear pitch landmarks)")
    return matrix


def _spread_ratio(points: npt.NDArray[np.float64]) -> float:
    """Second/first singular value of the mean-centered Nx2 coordinate matrix.

    1.0 means the points spread equally along both principal axes; 0.0 means
    they are exactly collinear. Callers guarantee a non-coincident point set
    (checked by :func:`_hartley_normalization`), so the leading singular value
    is strictly positive.
    """
    singular_values = np.linalg.svd(points - points.mean(axis=0), compute_uv=False)
    return float(singular_values[1] / singular_values[0])


def _require_well_spread(pixels: npt.NDArray[np.float64], pitch: npt.NDArray[np.float64]) -> None:
    """Reject nearly collinear fit landmarks (conditioning guard, US-C2).

    A landmark set that hugs one line (e.g. the six stump bases) passes the
    exact-degeneracy rank check and fits with a tiny reported rms, yet the
    homography extrapolates wildly off that line. Both the pitch-frame and
    pixel-frame spreads must clear :data:`_MIN_SPREAD_RATIO`.
    """
    ratio = min(_spread_ratio(pitch), _spread_ratio(pixels))
    if ratio < _MIN_SPREAD_RATIO:
        raise ExtrinsicsError(
            f"landmarks nearly collinear: spread ratio {ratio:.4f} < {_MIN_SPREAD_RATIO} "
            "- click landmarks spanning both pitch axes (add crease/edge points)"
        )


def _require_invertible(matrix: npt.NDArray[np.float64], message: str) -> None:
    singular_values = np.linalg.svd(matrix, compute_uv=False)
    if singular_values[-1] <= _RANK_RTOL * singular_values[0]:
        raise ExtrinsicsError(message)


def _matrix_rows(matrix: npt.NDArray[np.float64]) -> tuple[tuple[float, float, float], ...]:
    return tuple((float(row[0]), float(row[1]), float(row[2])) for row in matrix)


def fit_homography(
    pixel_xy: Sequence[tuple[float, float]],
    pitch_xy: Sequence[tuple[float, float]],
    *,
    holdout: int = 0,
) -> PlaneCalibration:
    """Fit the pixel->pitch homography from clicked landmark correspondences.

    ``pixel_xy[i]`` is the undistorted pixel of the landmark whose pitch-frame
    position is ``pitch_xy[i]``. With ``holdout > 0`` the last ``holdout``
    pairs are excluded from the fit and ``rms_m`` is the RMS euclidean error
    (meters) of mapping their pixels to pitch, an honest generalization check
    per the US-C2 acceptance criteria; with ``holdout == 0``, ``rms_m`` is
    measured over all pairs. ``rms_px`` always maps every pitch point back
    through the inverse homography against its clicked pixel.

    Raises :class:`InsufficientLandmarksError` when fewer than
    :data:`MIN_LANDMARKS` pairs remain to fit on, and :class:`ExtrinsicsError`
    for mismatched or non-finite inputs, degenerate (collinear, coincident)
    landmark configurations, and nearly collinear fit landmarks whose
    principal-axis spread ratio falls below :data:`_MIN_SPREAD_RATIO` in either
    the pitch or the pixel frame (an ill-conditioned, confidently-wrong fit).
    """
    pixels = _as_points(pixel_xy, "pixel_xy")
    pitch = _as_points(pitch_xy, "pitch_xy")
    if len(pixels) != len(pitch):
        raise ExtrinsicsError(
            f"pixel_xy and pitch_xy lengths differ: {len(pixels)} != {len(pitch)}"
        )
    if holdout < 0:
        raise ExtrinsicsError(f"holdout must be >= 0, got {holdout}")
    n_landmarks = len(pixels)
    n_fit = n_landmarks - holdout
    if n_fit < MIN_LANDMARKS:
        raise InsufficientLandmarksError(
            f"need at least {MIN_LANDMARKS} landmark pairs to fit the homography "
            f"({n_landmarks} provided, {holdout} held out)"
        )
    matrix = _fit_matrix(pixels[:n_fit], pitch[:n_fit])
    _require_well_spread(pixels[:n_fit], pitch[:n_fit])
    if holdout:
        rms_m = _rms_error(matrix, pixels[n_fit:], pitch[n_fit:])
    else:
        rms_m = _rms_error(matrix, pixels, pitch)
    rms_px = _rms_error(np.linalg.inv(matrix), pitch, pixels)
    return PlaneCalibration(
        matrix=_matrix_rows(matrix),
        rms_px=rms_px,
        rms_m=rms_m,
        n_landmarks=n_landmarks,
    )


def pixel_to_pitch_xy(cal: PlaneCalibration, px: tuple[float, float]) -> tuple[float, float]:
    """Map an undistorted pixel to pitch-frame (x, y) meters (US-C2 contract:
    origin = striker's middle-stump base, +x toward bowler, +y off side RH)."""
    return _apply_homography(np.asarray(cal.matrix, dtype=np.float64), px)


def pitch_xy_to_pixel(cal: PlaneCalibration, xy: tuple[float, float]) -> tuple[float, float]:
    """Map a pitch-frame (x, y) in meters back to the camera's pixel plane."""
    return _apply_homography(np.linalg.inv(np.asarray(cal.matrix, dtype=np.float64)), xy)


def to_params(cal: PlaneCalibration) -> dict[str, Any]:
    """JSON-safe payload for ``Calibration.params`` (kind=extrinsic)."""
    return {
        "version": PARAMS_VERSION,
        "matrix": [list(row) for row in cal.matrix],
        "rms_px": cal.rms_px,
        "rms_m": cal.rms_m,
        "n_landmarks": cal.n_landmarks,
    }


def _require_finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ExtrinsicsError(f"calibration params field {name!r} must be a finite number")
    return float(value)


def from_params(params: dict[str, Any]) -> PlaneCalibration:
    """Load a :class:`PlaneCalibration` from its :func:`to_params` payload.

    Raises :class:`ExtrinsicsError` on a wrong/missing version, a matrix that
    is not 3x3 finite numbers (or is singular), malformed rms fields, or an
    invalid landmark count. Unknown extra keys (e.g. ``intrinsics_id`` added
    by the calibration service for traceability) are ignored for forward
    compatibility; the known keys above are still strictly validated.
    """
    version = params.get("version")
    if version != PARAMS_VERSION:
        raise ExtrinsicsError(
            f"unsupported extrinsic params version {version!r} (expected {PARAMS_VERSION})"
        )
    raw_matrix = params.get("matrix")
    if not isinstance(raw_matrix, Sequence) or isinstance(raw_matrix, str) or len(raw_matrix) != 3:
        raise ExtrinsicsError("calibration params field 'matrix' must be a 3x3 matrix")
    rows: list[tuple[float, float, float]] = []
    for row in raw_matrix:
        if not isinstance(row, Sequence) or isinstance(row, str) or len(row) != 3:
            raise ExtrinsicsError("calibration params field 'matrix' must be a 3x3 matrix")
        rows.append(
            (
                _require_finite_number(row[0], "matrix"),
                _require_finite_number(row[1], "matrix"),
                _require_finite_number(row[2], "matrix"),
            )
        )
    rms_px = _require_finite_number(params.get("rms_px"), "rms_px")
    rms_m = _require_finite_number(params.get("rms_m"), "rms_m")
    if rms_px < 0 or rms_m < 0:
        raise ExtrinsicsError("calibration params rms values must be >= 0")
    n_landmarks = params.get("n_landmarks")
    if isinstance(n_landmarks, bool) or not isinstance(n_landmarks, int):
        raise ExtrinsicsError("calibration params field 'n_landmarks' must be an integer")
    if n_landmarks < MIN_LANDMARKS:
        raise ExtrinsicsError(
            f"calibration params field 'n_landmarks' must be >= {MIN_LANDMARKS}, got {n_landmarks}"
        )
    _require_invertible(np.array(rows, dtype=np.float64), "calibration matrix is singular")
    return PlaneCalibration(matrix=tuple(rows), rms_px=rms_px, rms_m=rms_m, n_landmarks=n_landmarks)


def _pitch_segments() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Pitch-frame line segments drawn on the verification overlay: pitch
    edges, centerline, stumps lines, and both popping creases."""
    half_width = PITCH_WIDTH_M / 2
    across_xs = (
        0.0,
        POPPING_CREASE_OFFSET_M,
        PITCH_LENGTH_M - POPPING_CREASE_OFFSET_M,
        PITCH_LENGTH_M,
    )
    across = [((x, -half_width), (x, half_width)) for x in across_xs]
    along = [
        ((0.0, -half_width), (PITCH_LENGTH_M, -half_width)),
        ((0.0, half_width), (PITCH_LENGTH_M, half_width)),
        ((0.0, 0.0), (PITCH_LENGTH_M, 0.0)),
    ]
    return across + along


def _pixel_point(cal: PlaneCalibration, xy: tuple[float, float]) -> tuple[int, int]:
    x, y = pitch_xy_to_pixel(cal, xy)
    return (round(x), round(y))


def draw_pitch_map(
    cal: PlaneCalibration,
    image_size: tuple[int, int],
    landmarks_px: Sequence[tuple[float, float]],
) -> npt.NDArray[np.uint8]:
    """Render a BGR verification overlay for a calibration (US-C2 AC).

    ``image_size`` is (width, height); the result is an HxWx3 uint8 BGR image
    with the reprojected pitch grid (edges, creases, stumps lines, centerline)
    drawn through the calibration and every clicked landmark pixel marked, so
    an operator can eyeball that drawn creases align with the real ones.
    """
    width, height = image_size
    if width <= 0 or height <= 0:
        raise ExtrinsicsError(f"image_size must be positive (width, height), got {image_size}")
    image: npt.NDArray[np.uint8] = np.empty((height, width, 3), dtype=np.uint8)
    image[:] = _BACKGROUND_BGR
    for start_xy, end_xy in _pitch_segments():
        cv2.line(image, _pixel_point(cal, start_xy), _pixel_point(cal, end_xy), _GRID_BGR, 1)
    for landmark in landmarks_px:
        center = (round(float(landmark[0])), round(float(landmark[1])))
        cv2.circle(image, center, 5, _LANDMARK_BGR, -1)
        cv2.circle(image, center, 8, _LANDMARK_RING_BGR, 1)
    return image
