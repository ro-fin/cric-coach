"""Per-camera intrinsic calibration from ChArUco board captures (US-C1).

A short board-sweep clip (or a directory of still frames) is reduced to an
:class:`IntrinsicsResult`: the 3x3 camera matrix, the OpenCV distortion
coefficients, the mean reprojection error in pixels, and full provenance
(board spec, number of contributing views, capture date, params version).

ChArUco boards tolerate partial views and occlusion: every detected interior
chessboard corner carries a unique id, so views only contribute the corners
they actually saw. Calibration fails loudly (:class:`InsufficientCoverageError`)
when too few views detect the board or when the views collectively cover too
little of it - a silent low-coverage fit would produce confidently wrong
distortion coefficients.

:func:`to_params` / :func:`from_params` round-trip the result through the
JSON-safe dict stored in ``Calibration.params`` for ``kind=intrinsic``.
:func:`undistort_pixel` / :func:`undistort_pixels` apply the stored model to
click coordinates so downstream homographies see lens-corrected pixels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Final, cast

import cv2
import numpy as np
import numpy.typing as npt

#: A view must contribute at least this many detected interior corners to
#: count toward ``min_views``; fewer over-weights noisy, barely-seen views.
MIN_CORNERS_PER_VIEW: Final = 8

#: Default fraction of the board's interior corners that must be detected in
#: at least one view across the whole capture (aggregate corner coverage).
DEFAULT_MIN_CORNER_COVERAGE: Final = 0.5

#: Version of the JSON params layout produced by :func:`to_params`.
PARAMS_VERSION: Final = 1

#: Row-major 3x3 matrix as plain floats (JSON-safe, hashable).
Matrix3x3 = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]


class InsufficientCoverageError(ValueError):
    """Board coverage is too thin to calibrate - capture more/better views (US-C1)."""


@dataclass(frozen=True)
class BoardSpec:
    """Physical ChArUco board description.

    Defaults describe a printable A1 board (594 x 841 mm): a 7 x 10 grid of
    80 mm squares (560 x 800 mm) with 60 mm ArUco markers from the 5x5-100
    dictionary. ``aruco_dict`` is the ``cv2.aruco`` predefined-dictionary
    constant name.
    """

    squares_x: int = 7
    squares_y: int = 10
    square_len_m: float = 0.08
    marker_len_m: float = 0.06
    aruco_dict: str = "DICT_5X5_100"

    def __post_init__(self) -> None:
        if self.squares_x < 3 or self.squares_y < 3:
            raise ValueError(
                f"board must be at least 3x3 squares, got {self.squares_x}x{self.squares_y}"
            )
        if not self.marker_len_m > 0:
            raise ValueError(f"marker_len_m must be positive, got {self.marker_len_m}")
        if not self.square_len_m > self.marker_len_m:
            raise ValueError(
                f"square_len_m ({self.square_len_m}) must exceed marker_len_m ({self.marker_len_m})"
            )
        if not self.aruco_dict.startswith("DICT_") or not hasattr(cv2.aruco, self.aruco_dict):
            raise ValueError(f"unknown ArUco dictionary name: {self.aruco_dict!r}")

    @property
    def n_interior_corners(self) -> int:
        """Number of interior chessboard corners the detector can identify."""
        return (self.squares_x - 1) * (self.squares_y - 1)


@dataclass(frozen=True)
class IntrinsicsResult:
    """Calibrated intrinsics with provenance (US-C1 AC: versioned + dated)."""

    camera_matrix: Matrix3x3
    dist_coeffs: tuple[float, ...]
    reprojection_error_px: float
    board_spec: BoardSpec
    n_views: int
    captured_on: str
    version: int = PARAMS_VERSION


def _make_board(spec: BoardSpec) -> cv2.aruco.CharucoBoard:
    dictionary = cv2.aruco.getPredefinedDictionary(int(getattr(cv2.aruco, spec.aruco_dict)))
    return cv2.aruco.CharucoBoard(
        (spec.squares_x, spec.squares_y), spec.square_len_m, spec.marker_len_m, dictionary
    )


def generate_board_image(spec: BoardSpec, *, px_per_square: int = 200) -> npt.NDArray[np.uint8]:
    """Render the board as a grayscale image (for printing and synthetic tests).

    The image is exactly ``squares_x * px_per_square`` wide and
    ``squares_y * px_per_square`` tall with no margin, so board coordinates in
    meters map to image pixels by ``px_per_square / square_len_m``.
    """
    if px_per_square < 1:
        raise ValueError(f"px_per_square must be >= 1, got {px_per_square}")
    board = _make_board(spec)
    size = (spec.squares_x * px_per_square, spec.squares_y * px_per_square)
    return np.asarray(board.generateImage(size), dtype=np.uint8)


def _matrix_to_tuple(matrix: npt.NDArray[np.float64]) -> Matrix3x3:
    if matrix.shape != (3, 3):
        raise ValueError(f"camera matrix must be 3x3, got shape {matrix.shape}")
    return (
        (float(matrix[0, 0]), float(matrix[0, 1]), float(matrix[0, 2])),
        (float(matrix[1, 0]), float(matrix[1, 1]), float(matrix[1, 2])),
        (float(matrix[2, 0]), float(matrix[2, 1]), float(matrix[2, 2])),
    )


def _validate_captured_on(captured_on: str) -> None:
    try:
        date.fromisoformat(captured_on)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"captured_on must be an ISO date (YYYY-MM-DD), got {captured_on!r}"
        ) from exc


def calibrate_from_images(
    images: Sequence[npt.NDArray[Any]],
    board: BoardSpec,
    *,
    min_views: int = 8,
    captured_on: str,
    min_corner_coverage: float = DEFAULT_MIN_CORNER_COVERAGE,
) -> IntrinsicsResult:
    """Calibrate camera intrinsics from grayscale or BGR views of ``board``.

    Each view contributes the ChArUco corners it detects (partial views are
    fine). Raises :class:`InsufficientCoverageError` when fewer than
    ``min_views`` views detect at least :data:`MIN_CORNERS_PER_VIEW` corners,
    or when the views collectively cover less than ``min_corner_coverage`` of
    the board's interior corners. ``captured_on`` is the ISO date the views
    were captured; it is provenance, so there is deliberately no default.
    """
    if min_views < 1:
        raise ValueError(f"min_views must be >= 1, got {min_views}")
    if not 0.0 < min_corner_coverage <= 1.0:
        raise ValueError(f"min_corner_coverage must be in (0, 1], got {min_corner_coverage}")
    _validate_captured_on(captured_on)

    board_cv = _make_board(board)
    detector = cv2.aruco.CharucoDetector(board_cv)
    corner_xyz = np.asarray(board_cv.getChessboardCorners(), dtype=np.float32)

    object_points: list[npt.NDArray[np.float32]] = []
    image_points: list[npt.NDArray[np.float32]] = []
    seen_corner_ids: set[int] = set()
    image_size: tuple[int, int] | None = None

    for image in images:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        size = (int(gray.shape[1]), int(gray.shape[0]))
        if image_size is None:
            image_size = size
        elif size != image_size:
            raise ValueError(f"all views must share one image size; got {image_size} and {size}")
        detection = detector.detectBoard(gray)
        # The stubs type detectBoard as non-optional, but at runtime both
        # corners and ids are None when no board is visible in the view.
        corners = cast("cv2.typing.MatLike | None", detection[0])
        ids = cast("cv2.typing.MatLike | None", detection[1])
        if corners is None or ids is None:
            continue
        flat_ids = np.asarray(ids, dtype=np.intp).reshape(-1)
        if flat_ids.size < MIN_CORNERS_PER_VIEW:
            continue
        object_points.append(corner_xyz[flat_ids].reshape(-1, 1, 3))
        image_points.append(np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2))
        seen_corner_ids.update(int(i) for i in flat_ids)

    n_views = len(object_points)
    if image_size is None or n_views < min_views:
        raise InsufficientCoverageError(
            f"only {n_views} of {len(images)} views detected at least "
            f"{MIN_CORNERS_PER_VIEW} ChArUco corners; {min_views} such views are required"
        )
    coverage = len(seen_corner_ids) / board.n_interior_corners
    if coverage < min_corner_coverage:
        raise InsufficientCoverageError(
            f"views collectively cover only {coverage:.0%} of the board's "
            f"{board.n_interior_corners} interior corners; at least "
            f"{min_corner_coverage:.0%} is required - sweep the board across the frame"
        )

    rms, camera_matrix, dist_coeffs, _rvecs, _tvecs = cv2.calibrateCamera(
        object_points, image_points, image_size, None, None
    )
    return IntrinsicsResult(
        camera_matrix=_matrix_to_tuple(np.asarray(camera_matrix, dtype=np.float64)),
        dist_coeffs=tuple(float(c) for c in np.asarray(dist_coeffs, dtype=np.float64).reshape(-1)),
        reprojection_error_px=float(rms),
        board_spec=board,
        n_views=n_views,
        captured_on=captured_on,
    )


def to_params(result: IntrinsicsResult) -> dict[str, Any]:
    """JSON-safe dict stored in ``Calibration.params`` for ``kind=intrinsic``."""
    return {
        "version": result.version,
        "camera_matrix": [list(row) for row in result.camera_matrix],
        "dist_coeffs": list(result.dist_coeffs),
        "reprojection_error_px": result.reprojection_error_px,
        "board_spec": asdict(result.board_spec),
        "n_views": result.n_views,
        "captured_on": result.captured_on,
    }


def from_params(params: dict[str, Any]) -> IntrinsicsResult:
    """Rebuild an :class:`IntrinsicsResult` from :func:`to_params` output.

    Raises ``ValueError`` on a wrong/missing version or malformed payload.
    """
    version = params.get("version")
    if version != PARAMS_VERSION:
        raise ValueError(f"unsupported intrinsics params version: {version!r}")
    try:
        board_raw = params["board_spec"]
        board = BoardSpec(
            squares_x=int(board_raw["squares_x"]),
            squares_y=int(board_raw["squares_y"]),
            square_len_m=float(board_raw["square_len_m"]),
            marker_len_m=float(board_raw["marker_len_m"]),
            aruco_dict=str(board_raw["aruco_dict"]),
        )
        captured_on = str(params["captured_on"])
        _validate_captured_on(captured_on)
        result = IntrinsicsResult(
            camera_matrix=_matrix_to_tuple(np.asarray(params["camera_matrix"], dtype=np.float64)),
            dist_coeffs=tuple(float(c) for c in params["dist_coeffs"]),
            reprojection_error_px=float(params["reprojection_error_px"]),
            board_spec=board,
            n_views=int(params["n_views"]),
            captured_on=captured_on,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed intrinsics params: {exc}") from exc
    return result


def undistort_pixels(
    params: dict[str, Any], pxs: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Map lens-distorted pixel coordinates to their ideal (undistorted) pixels.

    ``params`` is the ``kind=intrinsic`` :func:`to_params` payload; it is
    validated via :func:`from_params`, so a malformed payload raises the same
    ``ValueError``. Uses ``cv2.undistortPoints`` with ``P=camera_matrix`` so
    the results stay in PIXEL coordinates (not normalized ones); with all-zero
    distortion coefficients the mapping is the identity within float epsilon.
    """
    result = from_params(params)
    if not pxs:
        return []
    camera_matrix = np.asarray(result.camera_matrix, dtype=np.float64)
    dist_coeffs = np.asarray(result.dist_coeffs, dtype=np.float64)
    points = np.asarray([(float(x), float(y)) for x, y in pxs], dtype=np.float64).reshape(-1, 1, 2)
    undistorted = np.asarray(
        cv2.undistortPoints(points, camera_matrix, dist_coeffs, P=camera_matrix),
        dtype=np.float64,
    ).reshape(-1, 2)
    return [(float(u), float(v)) for u, v in undistorted]


def undistort_pixel(params: dict[str, Any], px: tuple[float, float]) -> tuple[float, float]:
    """Single-point convenience wrapper around :func:`undistort_pixels`."""
    return undistort_pixels(params, [px])[0]
