"""Stereo-pair calibration + DLT triangulation for true 3D ball flight (US-F6).

Pure NumPy software layer: compose two cameras' intrinsics (the ``camera_matrix``
/ ``dist_coeffs`` shapes produced by :mod:`cricai_vision.intrinsics`) with the
pair's relative extrinsics into a validated :class:`StereoPair`, triangulate
matched pixel pairs with per-point reprojection QC, lift two synchronized track
payloads (the pinned US-F3 contract) into a 3D trajectory with an honest quality
report, and estimate release height with a confidence value. The surveyed-target
rig test (RMS <= 5 cm, release height +/- 3 cm) is a deferred real-hardware
milestone; the math here is validated against synthetic stereo scenes only.

World frame contract
--------------------
Triangulated coordinates are in the canonical pitch frame
(:mod:`cricai_vision.geometry`): origin = middle-stump base at the striker's
end, +x toward the bowler, +y toward the off side for a right-hand batter,
+z up, meters. ``release_height`` therefore reads the ``z`` coordinate
directly as height above the pitch surface.

``Calibration`` row contract (``kind=stereo``)
----------------------------------------------
One row per camera pair, stored under ``camera_id = camera_pair[0]``;
``params`` is exactly the :func:`to_params` payload::

    {
      "version": 1,
      "frame": PITCH_FRAME,               # pinned world-frame contract string
      "camera_pair": ["C1", "C4"],
      "intrinsics_a": {"camera_matrix": [[fx, s, cx], [0, fy, cy], [0, 0, 1]],
                        "dist_coeffs": [k1, k2, p1, p2, k3]},
      "intrinsics_b": {...},               # same shape for the second camera
      "rotation": [[...], [...], [...]],   # camera_a -> camera_b: x_b = R x_a + t
      "translation": [tx, ty, tz],         # meters, expressed in camera_b
      "world_rotation_a": [[...], ...],    # pitch frame -> camera_a
      "world_translation_a": [tx, ty, tz]
    }

Distortion follows the OpenCV radial/tangential model with at most 5
coefficients (rational/thin-prism models are rejected loudly). Track pixel
inputs are RAW (distorted) pixel coordinates per the US-F3 payload contract;
this module normalizes and undistorts internally before the DLT.

Honest limits (US-F6 AC): no spin RPM and no seam/spin-axis claims — those
need specialist hardware. Confidence values here are deterministic heuristics
(monotone in reprojection quality and point count), not calibrated
probabilities.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt

#: Version tag of the ``Calibration.params`` JSON payload for ``kind=stereo``.
PARAMS_VERSION: Final = 1

#: Pinned world-frame contract string stored in stereo params (see module doc).
PITCH_FRAME: Final = "pitch/striker-stumps-origin/x-bowler/y-offside-rh/z-up/meters"

#: Minimum distance between the two camera centers; below this the pair is a
#: coincident/parallel mount that cannot triangulate (degenerate geometry).
MIN_BASELINE_M: Final = 0.01

#: Minimum angle between the two viewing rays at the triangulated point; below
#: it the intersection is numerically meaningless (near-parallel rays).
MIN_RAY_ANGLE_DEG: Final = 0.5

#: Default per-point QC gate: a matched pair whose reprojection error exceeds
#: this in either camera is flagged invalid (mismatched correspondence).
MAX_REPROJECTION_PX: Final = 20.0

#: Matched frames whose two timestamps disagree by more than this are dropped
#: (sync-skew guard; ties into the US-C3 drift monitor).
MAX_SYNC_SKEW_MS: Final = 10.0

#: Below this valid-3D fraction of the union of frames, the track is flagged.
LOW_OVERLAP_FRACTION: Final = 0.5

#: OpenCV distortion coefficients supported: [k1, k2, p1, p2, k3].
MAX_DIST_COEFFS: Final = 5

#: Default half-window around release used by :func:`release_height`.
RELEASE_WINDOW_MS: Final = 50.0

#: Point count at which the release-height count factor saturates at 1.0.
RELEASE_TARGET_POINTS: Final = 4

#: Segment kinds pinned by the US-F3 track payload contract.
SEGMENT_KINDS: Final = frozenset({"pre_bounce", "post_bounce", "post_contact"})

#: Fixed-point iterations for the radial/tangential undistortion inverse.
_UNDISTORT_ITERATIONS: Final = 12

#: |w| of the unit-norm homogeneous DLT solution below which the point is at
#: infinity (exactly parallel rays).
_W_EPSILON: Final = 1e-9

#: Timestamp spread (ms) below which release-height points count as one epoch.
_TS_SPREAD_EPSILON_MS: Final = 1e-6

#: Row-major 3x3 matrix as plain floats (JSON-safe, hashable).
Matrix3x3 = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]
Vector3 = tuple[float, float, float]
Dist5 = tuple[float, float, float, float, float]

MatrixLike = Sequence[Sequence[float]] | npt.NDArray[np.float64]
VectorLike = Sequence[float] | npt.NDArray[np.float64]


class TriangulationError(ValueError):
    """Invalid stereo params, malformed payloads, or bad triangulation input."""


class StereoGeometryError(TriangulationError):
    """Degenerate stereo geometry (coincident/parallel-mounted camera pair)."""


@dataclass(frozen=True)
class RigidTransform:
    """Rigid transform ``x_dst = rotation @ x_src + translation`` (meters)."""

    rotation: MatrixLike
    translation: VectorLike


@dataclass(frozen=True)
class StereoPair:
    """A validated stereo rig; build via :func:`make_stereo_pair` only.

    ``rotation``/``translation`` map camera-A coordinates to camera-B
    coordinates; ``world_rotation_a``/``world_translation_a`` map pitch-frame
    coordinates to camera-A coordinates, so triangulated points come out in
    the pitch frame (:data:`PITCH_FRAME`).
    """

    camera_a: str
    camera_b: str
    camera_matrix_a: Matrix3x3
    dist_coeffs_a: Dist5
    camera_matrix_b: Matrix3x3
    dist_coeffs_b: Dist5
    rotation: Matrix3x3
    translation: Vector3
    world_rotation_a: Matrix3x3
    world_translation_a: Vector3
    frame: str = PITCH_FRAME

    @property
    def baseline_m(self) -> float:
        """Distance between the two camera centers in meters."""
        return float(np.linalg.norm(np.asarray(self.translation, dtype=np.float64)))

    @property
    def projection_a(self) -> npt.NDArray[np.float64]:
        """Ideal (distortion-free) 3x4 pixel projection of camera A: K_a [R_a | t_a]."""
        ctx = _context(self)
        return np.asarray(ctx.k_a @ ctx.ext_a, dtype=np.float64)

    @property
    def projection_b(self) -> npt.NDArray[np.float64]:
        """Ideal (distortion-free) 3x4 pixel projection of camera B: K_b [R_b | t_b]."""
        ctx = _context(self)
        return np.asarray(ctx.k_b @ ctx.ext_b, dtype=np.float64)


@dataclass(frozen=True)
class TriangulatedPoint:
    """One matched pixel pair lifted to 3D with per-point reprojection QC.

    ``xyz`` is ``None`` when no stable 3D point exists (``low_parallax``);
    reprojection errors are ``None`` when the point fails cheirality
    (``behind_camera_a``/``behind_camera_b``). ``reason`` is ``None`` iff
    ``valid`` is true.
    """

    xyz: Vector3 | None
    reprojection_px_a: float | None
    reprojection_px_b: float | None
    valid: bool
    reason: str | None


@dataclass(frozen=True)
class Track3DPoint:
    """One 3D trajectory sample in the pitch frame (meters, video-timeline ms)."""

    frame_no: int
    ts_ms: float
    x: float
    y: float
    z: float
    reprojection_px: float  # max of the two cameras' reprojection errors


@dataclass(frozen=True)
class SegmentQuality3D:
    """3D confidence for one segment kind present in BOTH camera payloads.

    ``start_ms``/``end_ms`` is the intersection of the two cameras' windows
    (each camera's same-kind windows collapse to their envelope first);
    ``end_ms < start_ms`` signals no temporal overlap (confidence 0).
    ``confidence`` = weakest of the two cameras' same-kind 2D confidences,
    scaled by the fraction of union frames inside the window that produced a
    valid 3D point.
    """

    kind: str
    start_ms: float
    end_ms: float
    confidence: float
    n_points: int


@dataclass(frozen=True)
class TrackQuality3D:
    """Quality report for a triangulated track (US-F6: honest accounting).

    ``matched_fraction`` = valid 3D points / union of frame_nos across both
    payloads. Dropped frames are counted, never interpolated in 3D:
    ``n_dropped_unmatched`` (frame_no in only one payload),
    ``n_dropped_skewed`` (timestamps disagree beyond
    :data:`MAX_SYNC_SKEW_MS`), ``n_dropped_bridged`` (either camera's 2D point
    was gap-bridged), ``n_dropped_invalid`` (failed 3D QC).
    ``rms_reprojection_px`` pools both cameras' residuals over valid points
    (``None`` when there are none).
    """

    rms_reprojection_px: float | None
    matched_fraction: float
    n_frames_union: int
    n_points_3d: int
    n_dropped_unmatched: int
    n_dropped_skewed: int
    n_dropped_bridged: int
    n_dropped_invalid: int
    low_overlap: bool
    segments: tuple[SegmentQuality3D, ...]


@dataclass(frozen=True)
class Track3D:
    """A triangulated 3D trajectory plus its quality report."""

    points: tuple[Track3DPoint, ...]
    quality: TrackQuality3D


@dataclass(frozen=True)
class ReleaseHeight:
    """Release-height estimate: value + deterministic confidence (US-F6).

    ``value_m`` is ``None`` (with ``reason``) when no 3D points fall inside
    the release window. ``confidence`` is a heuristic in [0, 1], monotone in
    the number of contributing points and their reprojection quality — NOT a
    calibrated probability.
    """

    value_m: float | None
    confidence: float
    n_points: int
    reason: str | None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise TriangulationError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _require_unit(value: object, name: str) -> float:
    number = _require_number(value, name)
    if not 0.0 <= number <= 1.0:
        raise TriangulationError(f"{name} must be within [0, 1], got {number!r}")
    return number


def _as_float_array(value: object, shape: tuple[int, ...], name: str) -> npt.NDArray[np.float64]:
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise TriangulationError(f"{name} must be numeric with shape {shape}") from exc
    if array.shape != shape:
        raise TriangulationError(f"{name} must have shape {shape}, got {array.shape}")
    if not bool(np.all(np.isfinite(array))):
        raise TriangulationError(f"{name} contains non-finite values")
    return array


def _as_dist(value: object, name: str) -> Dist5:
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise TriangulationError(f"{name} must be a flat sequence of numbers") from exc
    if array.ndim != 1:
        raise TriangulationError(f"{name} must be a flat sequence, got shape {array.shape}")
    if array.size > MAX_DIST_COEFFS:
        raise TriangulationError(
            f"{name} supports at most {MAX_DIST_COEFFS} coefficients [k1, k2, p1, p2, k3]; "
            f"got {array.size} (rational/thin-prism models are unsupported)"
        )
    if not bool(np.all(np.isfinite(array))):
        raise TriangulationError(f"{name} contains non-finite values")
    padded = np.zeros(MAX_DIST_COEFFS, dtype=np.float64)
    padded[: array.size] = array
    return (
        float(padded[0]),
        float(padded[1]),
        float(padded[2]),
        float(padded[3]),
        float(padded[4]),
    )


def _require_camera_matrix(matrix: npt.NDArray[np.float64], name: str) -> None:
    if not np.allclose(matrix[2], (0.0, 0.0, 1.0), atol=1e-9):
        raise TriangulationError(f"{name} bottom row must be (0, 0, 1), got {matrix[2]!r}")
    if matrix[0, 0] <= 0.0 or matrix[1, 1] <= 0.0:
        raise TriangulationError(f"{name} focal lengths must be positive")


def _require_rotation(matrix: npt.NDArray[np.float64], name: str) -> None:
    identity_gap = float(np.max(np.abs(matrix @ matrix.T - np.eye(3))))
    if identity_gap > 1e-6:
        raise TriangulationError(
            f"{name} is not orthonormal (max |R R^T - I| = {identity_gap:.2e})"
        )
    if float(np.linalg.det(matrix)) < 0.0:
        raise TriangulationError(f"{name} is a reflection (det < 0), not a rotation")


def _matrix3_tuple(matrix: npt.NDArray[np.float64]) -> Matrix3x3:
    return (
        (float(matrix[0, 0]), float(matrix[0, 1]), float(matrix[0, 2])),
        (float(matrix[1, 0]), float(matrix[1, 1]), float(matrix[1, 2])),
        (float(matrix[2, 0]), float(matrix[2, 1]), float(matrix[2, 2])),
    )


def _vector3_tuple(vector: npt.NDArray[np.float64]) -> Vector3:
    return (float(vector[0]), float(vector[1]), float(vector[2]))


def _intrinsics_block(block: object, name: str) -> tuple[Matrix3x3, Dist5]:
    if not isinstance(block, Mapping):
        raise TriangulationError(f"{name} must be a mapping with camera_matrix and dist_coeffs")
    if "camera_matrix" not in block or "dist_coeffs" not in block:
        raise TriangulationError(f"{name} must contain 'camera_matrix' and 'dist_coeffs'")
    matrix = _as_float_array(block["camera_matrix"], (3, 3), f"{name}.camera_matrix")
    _require_camera_matrix(matrix, f"{name}.camera_matrix")
    return _matrix3_tuple(matrix), _as_dist(block["dist_coeffs"], f"{name}.dist_coeffs")


# ---------------------------------------------------------------------------
# Stereo pair construction + params round-trip
# ---------------------------------------------------------------------------


def make_stereo_pair(
    *,
    camera_pair: Sequence[str],
    intrinsics_a: Mapping[str, Any],
    intrinsics_b: Mapping[str, Any],
    a_to_b: RigidTransform,
    pitch_to_a: RigidTransform,
) -> StereoPair:
    """Compose two cameras' intrinsics + extrinsics into a validated pair.

    ``intrinsics_a``/``intrinsics_b`` accept the ``kind=intrinsic``
    :func:`cricai_vision.intrinsics.to_params` payloads directly (only
    ``camera_matrix`` and ``dist_coeffs`` are read; extra keys are ignored).
    ``a_to_b`` is the relative extrinsics (camera A -> camera B) and
    ``pitch_to_a`` is camera A's world pose (pitch frame -> camera A), so
    triangulated points land in the pitch frame. Raises
    :class:`TriangulationError` for malformed shapes/values and
    :class:`StereoGeometryError` for a baseline below
    :data:`MIN_BASELINE_M` (coincident/parallel mounts cannot triangulate).
    """
    ids = tuple(camera_pair)
    if len(ids) != 2 or not all(isinstance(c, str) and c for c in ids):
        raise TriangulationError(
            f"camera_pair must be two non-empty camera ids, got {camera_pair!r}"
        )
    if ids[0] == ids[1]:
        raise TriangulationError(f"camera_pair must name two distinct cameras, got {camera_pair!r}")
    k_a, dist_a = _intrinsics_block(intrinsics_a, "intrinsics_a")
    k_b, dist_b = _intrinsics_block(intrinsics_b, "intrinsics_b")
    rotation = _as_float_array(a_to_b.rotation, (3, 3), "a_to_b.rotation")
    _require_rotation(rotation, "a_to_b.rotation")
    translation = _as_float_array(a_to_b.translation, (3,), "a_to_b.translation")
    world_rotation = _as_float_array(pitch_to_a.rotation, (3, 3), "pitch_to_a.rotation")
    _require_rotation(world_rotation, "pitch_to_a.rotation")
    world_translation = _as_float_array(pitch_to_a.translation, (3,), "pitch_to_a.translation")
    baseline = float(np.linalg.norm(translation))
    if baseline < MIN_BASELINE_M:
        raise StereoGeometryError(
            f"camera pair {ids[0]}+{ids[1]} baseline {baseline:.4f} m is below "
            f"{MIN_BASELINE_M} m: coincident/parallel-mounted cameras cannot triangulate"
        )
    return StereoPair(
        camera_a=ids[0],
        camera_b=ids[1],
        camera_matrix_a=k_a,
        dist_coeffs_a=dist_a,
        camera_matrix_b=k_b,
        dist_coeffs_b=dist_b,
        rotation=_matrix3_tuple(rotation),
        translation=_vector3_tuple(translation),
        world_rotation_a=_matrix3_tuple(world_rotation),
        world_translation_a=_vector3_tuple(world_translation),
    )


def to_params(pair: StereoPair) -> dict[str, Any]:
    """JSON-safe payload for ``Calibration.params`` (``kind=stereo``)."""
    return {
        "version": PARAMS_VERSION,
        "frame": pair.frame,
        "camera_pair": [pair.camera_a, pair.camera_b],
        "intrinsics_a": {
            "camera_matrix": [list(row) for row in pair.camera_matrix_a],
            "dist_coeffs": list(pair.dist_coeffs_a),
        },
        "intrinsics_b": {
            "camera_matrix": [list(row) for row in pair.camera_matrix_b],
            "dist_coeffs": list(pair.dist_coeffs_b),
        },
        "rotation": [list(row) for row in pair.rotation],
        "translation": list(pair.translation),
        "world_rotation_a": [list(row) for row in pair.world_rotation_a],
        "world_translation_a": list(pair.world_translation_a),
    }


def from_params(params: Mapping[str, Any]) -> StereoPair:
    """Rebuild a :class:`StereoPair` from its :func:`to_params` payload.

    Raises :class:`TriangulationError` on a wrong version, a wrong frame
    contract, or any malformed/missing field (full re-validation via
    :func:`make_stereo_pair`).
    """
    version = params.get("version")
    if version != PARAMS_VERSION:
        raise TriangulationError(
            f"unsupported stereo params version {version!r} (expected {PARAMS_VERSION})"
        )
    frame = params.get("frame")
    if frame != PITCH_FRAME:
        raise TriangulationError(
            f"unsupported stereo frame contract {frame!r} (expected {PITCH_FRAME!r})"
        )
    camera_pair = params.get("camera_pair")
    if not isinstance(camera_pair, Sequence) or isinstance(camera_pair, str):
        raise TriangulationError("stereo params field 'camera_pair' must be a sequence of two ids")
    try:
        return make_stereo_pair(
            camera_pair=list(camera_pair),
            intrinsics_a=params["intrinsics_a"],
            intrinsics_b=params["intrinsics_b"],
            a_to_b=RigidTransform(rotation=params["rotation"], translation=params["translation"]),
            pitch_to_a=RigidTransform(
                rotation=params["world_rotation_a"],
                translation=params["world_translation_a"],
            ),
        )
    except KeyError as exc:
        raise TriangulationError(f"stereo params missing key: {exc}") from exc


# ---------------------------------------------------------------------------
# Projection model (OpenCV radial/tangential, pure NumPy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class _Ctx:
    """Precomputed arrays for one pair: poses, centers, normalized projections."""

    k_a: npt.NDArray[np.float64]
    dist_a: npt.NDArray[np.float64]
    r_a: npt.NDArray[np.float64]
    t_a: npt.NDArray[np.float64]
    k_b: npt.NDArray[np.float64]
    dist_b: npt.NDArray[np.float64]
    r_b: npt.NDArray[np.float64]
    t_b: npt.NDArray[np.float64]
    center_a: npt.NDArray[np.float64]
    center_b: npt.NDArray[np.float64]
    ext_a: npt.NDArray[np.float64]  # [R_a | t_a], 3x4
    ext_b: npt.NDArray[np.float64]  # [R_b | t_b], 3x4


def _context(pair: StereoPair) -> _Ctx:
    r_a = np.asarray(pair.world_rotation_a, dtype=np.float64)
    t_a = np.asarray(pair.world_translation_a, dtype=np.float64)
    r_ab = np.asarray(pair.rotation, dtype=np.float64)
    t_ab = np.asarray(pair.translation, dtype=np.float64)
    r_b = np.asarray(r_ab @ r_a, dtype=np.float64)
    t_b = np.asarray(r_ab @ t_a + t_ab, dtype=np.float64)
    return _Ctx(
        k_a=np.asarray(pair.camera_matrix_a, dtype=np.float64),
        dist_a=np.asarray(pair.dist_coeffs_a, dtype=np.float64),
        r_a=r_a,
        t_a=t_a,
        k_b=np.asarray(pair.camera_matrix_b, dtype=np.float64),
        dist_b=np.asarray(pair.dist_coeffs_b, dtype=np.float64),
        r_b=r_b,
        t_b=t_b,
        center_a=np.asarray(-r_a.T @ t_a, dtype=np.float64),
        center_b=np.asarray(-r_b.T @ t_b, dtype=np.float64),
        ext_a=np.column_stack([r_a, t_a]),
        ext_b=np.column_stack([r_b, t_b]),
    )


def _distortion_terms(
    dist: npt.NDArray[np.float64], xn: npt.NDArray[np.float64]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Radial factor (N,) and tangential offset (N, 2) at normalized coords."""
    k1, k2, p1, p2, k3 = (float(c) for c in dist)
    x = xn[:, 0]
    y = xn[:, 1]
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    tangential_x = 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    tangential_y = p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return np.asarray(radial, dtype=np.float64), np.column_stack([tangential_x, tangential_y])


def _undistort_normalized(
    k: npt.NDArray[np.float64],
    dist: npt.NDArray[np.float64],
    pixels: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """RAW pixels (N, 2) -> ideal (undistorted) normalized camera coords (N, 2)."""
    homog = np.column_stack([pixels, np.ones(len(pixels))])
    distorted = np.asarray((np.linalg.inv(k) @ homog.T).T[:, :2], dtype=np.float64)
    ideal = distorted.copy()
    for _ in range(_UNDISTORT_ITERATIONS):
        radial, tangential = _distortion_terms(dist, ideal)
        ideal = (distorted - tangential) / radial[:, None]
    return ideal


def _project_pixels(
    k: npt.NDArray[np.float64],
    dist: npt.NDArray[np.float64],
    ext: npt.NDArray[np.float64],
    xyz: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """World points (N, 3) -> RAW (distorted) pixels (N, 2). Depths must be > 0."""
    cam = xyz @ ext[:, :3].T + ext[:, 3]
    xn = np.asarray(cam[:, :2] / cam[:, 2:3], dtype=np.float64)
    radial, tangential = _distortion_terms(dist, xn)
    distorted = xn * radial[:, None] + tangential
    homog = np.column_stack([distorted, np.ones(len(distorted))])
    return np.asarray((homog @ k.T)[:, :2], dtype=np.float64)


# ---------------------------------------------------------------------------
# DLT triangulation with per-point QC
# ---------------------------------------------------------------------------


def _dlt(
    ext_a: npt.NDArray[np.float64],
    ext_b: npt.NDArray[np.float64],
    norm_a: npt.NDArray[np.float64],
    norm_b: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Unit-norm homogeneous DLT solution for one matched normalized pair."""
    rows = np.stack(
        [
            norm_a[0] * ext_a[2] - ext_a[0],
            norm_a[1] * ext_a[2] - ext_a[1],
            norm_b[0] * ext_b[2] - ext_b[0],
            norm_b[1] * ext_b[2] - ext_b[1],
        ]
    )
    _basis, _singular_values, vt = np.linalg.svd(rows)
    return np.asarray(vt[-1], dtype=np.float64)


def _triangulate_one(
    ctx: _Ctx,
    raw: tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]],
    norm: tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]],
    max_reprojection_px: float,
) -> TriangulatedPoint:
    homogeneous = _dlt(ctx.ext_a, ctx.ext_b, norm[0], norm[1])
    if abs(float(homogeneous[3])) < _W_EPSILON:
        return TriangulatedPoint(None, None, None, False, "low_parallax")
    xyz = np.asarray(homogeneous[:3] / homogeneous[3], dtype=np.float64)
    if float(ctx.r_a[2] @ xyz + ctx.t_a[2]) <= 0.0:
        return TriangulatedPoint(_vector3_tuple(xyz), None, None, False, "behind_camera_a")
    if float(ctx.r_b[2] @ xyz + ctx.t_b[2]) <= 0.0:
        return TriangulatedPoint(_vector3_tuple(xyz), None, None, False, "behind_camera_b")
    ray_a = xyz - ctx.center_a
    ray_b = xyz - ctx.center_b
    cos_angle = float(
        np.clip(
            ray_a @ ray_b / (np.linalg.norm(ray_a) * np.linalg.norm(ray_b)),
            -1.0,
            1.0,
        )
    )
    if math.degrees(math.acos(cos_angle)) < MIN_RAY_ANGLE_DEG:
        return TriangulatedPoint(None, None, None, False, "low_parallax")
    error_a = float(
        np.linalg.norm(_project_pixels(ctx.k_a, ctx.dist_a, ctx.ext_a, xyz[None, :])[0] - raw[0])
    )
    error_b = float(
        np.linalg.norm(_project_pixels(ctx.k_b, ctx.dist_b, ctx.ext_b, xyz[None, :])[0] - raw[1])
    )
    if max(error_a, error_b) > max_reprojection_px:
        return TriangulatedPoint(_vector3_tuple(xyz), error_a, error_b, False, "high_reprojection")
    return TriangulatedPoint(_vector3_tuple(xyz), error_a, error_b, True, None)


def triangulate_points(
    pair: StereoPair,
    pixels_a: Sequence[tuple[float, float]],
    pixels_b: Sequence[tuple[float, float]],
    *,
    max_reprojection_px: float = MAX_REPROJECTION_PX,
) -> list[TriangulatedPoint]:
    """DLT-triangulate matched RAW pixel pairs into pitch-frame 3D points.

    ``pixels_a[i]`` and ``pixels_b[i]`` must observe the same physical point
    at the same instant. Pixels are undistorted internally (OpenCV model).
    Each result carries per-point reprojection-error QC; degenerate geometry
    is flagged loudly via ``reason`` (see :class:`TriangulatedPoint`).
    """
    limit = _require_number(max_reprojection_px, "max_reprojection_px")
    if limit <= 0.0:
        raise TriangulationError(f"max_reprojection_px must be positive, got {limit!r}")
    if len(pixels_a) != len(pixels_b):
        raise TriangulationError(
            f"pixels_a and pixels_b lengths differ: {len(pixels_a)} != {len(pixels_b)}"
        )
    if not pixels_a:
        return []
    count = len(pixels_a)
    array_a = _as_float_array(pixels_a, (count, 2), "pixels_a")
    array_b = _as_float_array(pixels_b, (count, 2), "pixels_b")
    ctx = _context(pair)
    norm_a = _undistort_normalized(ctx.k_a, ctx.dist_a, array_a)
    norm_b = _undistort_normalized(ctx.k_b, ctx.dist_b, array_b)
    return [
        _triangulate_one(ctx, (array_a[i], array_b[i]), (norm_a[i], norm_b[i]), limit)
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Track payload parsing (pinned US-F3 contract) + 3D track
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TrackPoint:
    frame_no: int
    ts_ms: float
    px: tuple[float, float]
    score: float
    bridged: bool


@dataclass(frozen=True)
class _Segment:
    kind: str
    start_ms: float
    end_ms: float
    confidence: float


def _parse_point(raw: object, label: str) -> _TrackPoint:
    if not isinstance(raw, Mapping):
        raise TriangulationError(f"{label} must be a mapping")
    frame_no = raw.get("frame_no")
    if isinstance(frame_no, bool) or not isinstance(frame_no, int):
        raise TriangulationError(f"{label}.frame_no must be an integer, got {frame_no!r}")
    bridged = raw.get("bridged")
    if not isinstance(bridged, bool):
        raise TriangulationError(f"{label}.bridged must be a boolean, got {bridged!r}")
    return _TrackPoint(
        frame_no=frame_no,
        ts_ms=_require_number(raw.get("ts_ms"), f"{label}.ts_ms"),
        px=(
            _require_number(raw.get("px_x"), f"{label}.px_x"),
            _require_number(raw.get("px_y"), f"{label}.px_y"),
        ),
        score=_require_unit(raw.get("score"), f"{label}.score"),
        bridged=bridged,
    )


def _parse_segment(raw: object, label: str) -> _Segment:
    if not isinstance(raw, Mapping):
        raise TriangulationError(f"{label} must be a mapping")
    kind = raw.get("kind")
    if kind not in SEGMENT_KINDS:
        raise TriangulationError(
            f"{label}.kind must be one of {sorted(SEGMENT_KINDS)}, got {kind!r}"
        )
    start_ms = _require_number(raw.get("start_ms"), f"{label}.start_ms")
    end_ms = _require_number(raw.get("end_ms"), f"{label}.end_ms")
    if end_ms < start_ms:
        raise TriangulationError(f"{label} start_ms must be <= end_ms, got {start_ms}..{end_ms}")
    return _Segment(
        kind=str(kind),
        start_ms=start_ms,
        end_ms=end_ms,
        confidence=_require_unit(raw.get("confidence"), f"{label}.confidence"),
    )


def _parse_track(
    payload: object, name: str
) -> tuple[dict[int, _TrackPoint], dict[str, list[_Segment]]]:
    if not isinstance(payload, Mapping):
        raise TriangulationError(
            f"{name} must be a track payload mapping, got {type(payload).__name__}"
        )
    points_raw = payload.get("points")
    if not isinstance(points_raw, Sequence) or isinstance(points_raw, str | bytes):
        raise TriangulationError(f"{name}.points must be a list of track points")
    segments_raw = payload.get("segments")
    if not isinstance(segments_raw, Sequence) or isinstance(segments_raw, str | bytes):
        raise TriangulationError(f"{name}.segments must be a list of segments")
    points: dict[int, _TrackPoint] = {}
    for index, raw in enumerate(points_raw):
        point = _parse_point(raw, f"{name}.points[{index}]")
        if point.frame_no in points:
            raise TriangulationError(f"{name} has duplicate frame_no {point.frame_no}")
        points[point.frame_no] = point
    # Same-kind repeats are legal: a long occlusion gap splits a phase into
    # several segments (pinned US-F3 behavior), so group per kind.
    segments: dict[str, list[_Segment]] = {}
    for index, raw in enumerate(segments_raw):
        segment = _parse_segment(raw, f"{name}.segments[{index}]")
        segments.setdefault(segment.kind, []).append(segment)
    return points, segments


def _segment_report(
    segments_a: dict[str, list[_Segment]],
    segments_b: dict[str, list[_Segment]],
    frame_ts: Sequence[float],
    points3d: Sequence[Track3DPoint],
) -> tuple[SegmentQuality3D, ...]:
    """Per-kind 3D confidence for kinds present in BOTH payloads.

    Each camera's same-kind windows (long-gap splits, pinned US-F3 behavior)
    collapse to their envelope — earliest start, latest end, weakest
    confidence — before the two cameras' windows intersect; frames inside a
    split's gap carry no points, so coverage stays honest.
    """
    report: list[SegmentQuality3D] = []
    for kind, group_a in segments_a.items():
        group_b = segments_b.get(kind)
        if group_b is None:
            continue
        start_ms = max(min(s.start_ms for s in group_a), min(s.start_ms for s in group_b))
        end_ms = min(max(s.end_ms for s in group_a), max(s.end_ms for s in group_b))
        n_window = sum(1 for ts in frame_ts if start_ms <= ts <= end_ms)
        n_valid = sum(1 for point in points3d if start_ms <= point.ts_ms <= end_ms)
        coverage = n_valid / n_window if n_window else 0.0
        confidence = min(s.confidence for s in (*group_a, *group_b))
        report.append(
            SegmentQuality3D(
                kind=kind,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=confidence * coverage,
                n_points=n_valid,
            )
        )
    return tuple(report)


def triangulate_track(
    pair: StereoPair,
    track_a: Mapping[str, Any],
    track_b: Mapping[str, Any],
    *,
    max_reprojection_px: float = MAX_REPROJECTION_PX,
) -> Track3D:
    """Lift two synchronized track payloads into one 3D trajectory + QC report.

    ``track_a``/``track_b`` are the pinned US-F3 per-camera payloads
    (``track_a`` from ``pair.camera_a``). Points are matched by ``frame_no``;
    unmatched, sync-skewed, gap-bridged and QC-failing frames are dropped and
    counted — never interpolated in 3D. Timestamps of 3D points come from
    camera A (the pair's reference camera).
    """
    points_a, segments_a = _parse_track(track_a, "track_a")
    points_b, segments_b = _parse_track(track_b, "track_b")
    union_frames = set(points_a) | set(points_b)
    common_frames = sorted(set(points_a) & set(points_b))
    n_skewed = 0
    n_bridged = 0
    matched: list[tuple[_TrackPoint, _TrackPoint]] = []
    for frame_no in common_frames:
        point_a = points_a[frame_no]
        point_b = points_b[frame_no]
        if abs(point_a.ts_ms - point_b.ts_ms) > MAX_SYNC_SKEW_MS:
            n_skewed += 1
            continue
        if point_a.bridged or point_b.bridged:
            n_bridged += 1
            continue
        matched.append((point_a, point_b))
    results = triangulate_points(
        pair,
        [pair_points[0].px for pair_points in matched],
        [pair_points[1].px for pair_points in matched],
        max_reprojection_px=max_reprojection_px,
    )
    points3d: list[Track3DPoint] = []
    square_sum = 0.0
    n_residuals = 0
    n_invalid = 0
    for (point_a, _point_b), result in zip(matched, results, strict=True):
        if (
            not result.valid
            or result.xyz is None
            or result.reprojection_px_a is None
            or result.reprojection_px_b is None
        ):
            n_invalid += 1
            continue
        square_sum += result.reprojection_px_a**2 + result.reprojection_px_b**2
        n_residuals += 2
        points3d.append(
            Track3DPoint(
                frame_no=point_a.frame_no,
                ts_ms=point_a.ts_ms,
                x=result.xyz[0],
                y=result.xyz[1],
                z=result.xyz[2],
                reprojection_px=max(result.reprojection_px_a, result.reprojection_px_b),
            )
        )
    n_union = len(union_frames)
    matched_fraction = len(points3d) / n_union if n_union else 0.0
    frame_ts = [
        points_a[frame].ts_ms if frame in points_a else points_b[frame].ts_ms
        for frame in union_frames
    ]
    quality = TrackQuality3D(
        rms_reprojection_px=math.sqrt(square_sum / n_residuals) if n_residuals else None,
        matched_fraction=matched_fraction,
        n_frames_union=n_union,
        n_points_3d=len(points3d),
        n_dropped_unmatched=n_union - len(common_frames),
        n_dropped_skewed=n_skewed,
        n_dropped_bridged=n_bridged,
        n_dropped_invalid=n_invalid,
        low_overlap=matched_fraction < LOW_OVERLAP_FRACTION,
        segments=_segment_report(segments_a, segments_b, frame_ts, points3d),
    )
    return Track3D(points=tuple(points3d), quality=quality)


# ---------------------------------------------------------------------------
# Release height (US-F6: reproducibility in mind)
# ---------------------------------------------------------------------------


def release_height(
    track: Track3D, release_ms: float, *, window_ms: float = RELEASE_WINDOW_MS
) -> ReleaseHeight:
    """Estimate release height (pitch-frame z, meters) at ``release_ms``.

    Deterministic: fits z linearly in time over the 3D points within
    ``window_ms`` of release and evaluates at ``release_ms`` (a single point,
    or points sharing one timestamp, use the plain mean). The least-squares
    fit smooths single-frame jitter — the +/- 3 cm repeatability AC is
    verified on the deferred machine-feed study, not claimed here.
    ``confidence`` = (point-count factor, saturating at
    :data:`RELEASE_TARGET_POINTS`) x (1 / (1 + mean reprojection px)).
    """
    release = _require_number(release_ms, "release_ms")
    window = _require_number(window_ms, "window_ms")
    if window <= 0.0:
        raise TriangulationError(f"window_ms must be positive, got {window!r}")
    used = [point for point in track.points if abs(point.ts_ms - release) <= window]
    if not used:
        return ReleaseHeight(
            value_m=None,
            confidence=0.0,
            n_points=0,
            reason=f"no 3D points within {window:g} ms of release",
        )
    offsets_s = np.asarray([(point.ts_ms - release) / 1000.0 for point in used])
    heights = np.asarray([point.z for point in used])
    if len(used) >= 2 and float(np.ptp(offsets_s)) * 1000.0 > _TS_SPREAD_EPSILON_MS:
        design = np.column_stack([np.ones(len(used)), offsets_s])
        solution, _residuals, _rank, _sv = np.linalg.lstsq(design, heights, rcond=None)
        value = float(solution[0])
    else:
        value = float(heights.mean())
    mean_reprojection = float(np.mean([point.reprojection_px for point in used]))
    confidence = min(len(used) / RELEASE_TARGET_POINTS, 1.0) / (1.0 + mean_reprojection)
    return ReleaseHeight(value_m=value, confidence=confidence, n_points=len(used), reason=None)
