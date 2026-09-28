"""US-F6: stereo-pair calibration, DLT triangulation, 3D track + release height.

All scenes are synthetic: ground-truth 3D parabolic flights projected through
two known pinhole cameras (with OpenCV-model distortion and optional seeded
pixel noise), so every assertion checks the math against a known answer.
"""

import math
from collections.abc import Iterable, Mapping
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import pytest
from cricai_vision.geometry import make_overhead_camera
from cricai_vision.triangulate import (
    LOW_OVERLAP_FRACTION,
    MAX_REPROJECTION_PX,
    MAX_SYNC_SKEW_MS,
    MIN_BASELINE_M,
    PARAMS_VERSION,
    PITCH_FRAME,
    ReleaseHeight,
    RigidTransform,
    StereoGeometryError,
    StereoPair,
    Track3D,
    Track3DPoint,
    TrackQuality3D,
    TriangulationError,
    from_params,
    make_stereo_pair,
    release_height,
    to_params,
    triangulate_points,
    triangulate_track,
)

FloatArray = npt.NDArray[np.float64]
#: One synthetic camera: (K, dist, R world->camera, t).
Camera = tuple[FloatArray, FloatArray, FloatArray, FloatArray]

K_A: Final = np.array([[1400.0, 0.0, 960.0], [0.0, 1400.0, 540.0], [0.0, 0.0, 1.0]])
DIST_A: Final = np.array([-0.12, 0.02, 0.0005, -0.0003, 0.0])
DIST_B: Final = np.array([-0.08, 0.01, 0.0, 0.0, 0.0])
NO_DIST: Final = np.zeros(5)

C1_POSITION: Final = np.array([10.06, 6.0, 1.5])
C1_TARGET: Final = np.array([10.06, 0.0, 1.0])

FPS: Final = 120.0
FLIGHT_START_MS: Final = 12000.0
FLIGHT_START_FRAME: Final = 1440
N_FLIGHT: Final = 24

SEGMENTS_A: Final = (
    {"kind": "pre_bounce", "start_ms": 12000.0, "end_ms": 12100.0, "confidence": 0.9},
    {"kind": "post_bounce", "start_ms": 12100.0, "end_ms": 12200.0, "confidence": 0.7},
)
SEGMENTS_B: Final = (
    {"kind": "pre_bounce", "start_ms": 12000.0, "end_ms": 12100.0, "confidence": 0.8},
    {"kind": "post_bounce", "start_ms": 12100.0, "end_ms": 12200.0, "confidence": 0.75},
)


def _look_at(position: FloatArray, target: FloatArray) -> tuple[FloatArray, FloatArray]:
    """World->camera rotation + translation for a camera looking at ``target``."""
    forward = target - position
    forward = forward / np.linalg.norm(forward)
    world_up = np.array([0.0, 0.0, 1.0])
    right = np.cross(forward, world_up)
    right = right / np.linalg.norm(right)
    down = np.cross(forward, right)
    rotation = np.vstack([right, down, forward])
    return rotation, -rotation @ position


def _camera_a(dist: FloatArray = DIST_A) -> Camera:
    rotation, translation = _look_at(C1_POSITION, C1_TARGET)
    return K_A.copy(), dist.copy(), rotation, translation


def _camera_b(dist: FloatArray = DIST_B) -> Camera:
    pinhole = make_overhead_camera(focal_px=1200.0)
    return (
        np.asarray(pinhole.camera_matrix, dtype=np.float64),
        dist.copy(),
        np.asarray(pinhole.rotation, dtype=np.float64),
        np.asarray(pinhole.translation, dtype=np.float64),
    )


def _intrinsics(camera: Camera) -> dict[str, Any]:
    return {"camera_matrix": camera[0].tolist(), "dist_coeffs": camera[1].tolist()}


def _relative(camera_a: Camera, camera_b: Camera) -> RigidTransform:
    rotation = camera_b[2] @ camera_a[2].T
    return RigidTransform(rotation=rotation, translation=camera_b[3] - rotation @ camera_a[3])


def _pair_from_cameras(
    camera_a: Camera, camera_b: Camera, ids: tuple[str, str] = ("C1", "C4")
) -> StereoPair:
    return make_stereo_pair(
        camera_pair=ids,
        intrinsics_a=_intrinsics(camera_a),
        intrinsics_b=_intrinsics(camera_b),
        a_to_b=_relative(camera_a, camera_b),
        pitch_to_a=RigidTransform(rotation=camera_a[2], translation=camera_a[3]),
    )


def _pair_kwargs() -> dict[str, Any]:
    """Fresh valid make_stereo_pair kwargs for mutation in validation tests."""
    camera_a = _camera_a()
    camera_b = _camera_b()
    return {
        "camera_pair": ("C1", "C4"),
        "intrinsics_a": _intrinsics(camera_a),
        "intrinsics_b": _intrinsics(camera_b),
        "a_to_b": _relative(camera_a, camera_b),
        "pitch_to_a": RigidTransform(rotation=camera_a[2], translation=camera_a[3]),
    }


def _px(row: FloatArray) -> tuple[float, float]:
    """First projected pixel row as a plain (x, y) tuple."""
    return (float(row[0]), float(row[1]))


def _project(camera: Camera, xyz: FloatArray) -> FloatArray:
    """Independent OpenCV-model projector (deliberately allows negative depth)."""
    k, dist, rotation, translation = camera
    cam = xyz @ rotation.T + translation
    xn = cam[:, :2] / cam[:, 2:3]
    k1, k2, p1, p2, k3 = (float(c) for c in dist)
    x, y = xn[:, 0], xn[:, 1]
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return np.column_stack([k[0, 0] * xd + k[0, 2], k[1, 1] * yd + k[1, 2]])


def _flight() -> tuple[list[int], FloatArray, FloatArray]:
    """Ground-truth parabolic flight: (frame_nos, ts_ms, xyz in the pitch frame)."""
    seconds = np.arange(N_FLIGHT) / FPS
    xyz = np.column_stack(
        [
            13.0 - 30.0 * seconds,
            0.2 - 0.5 * seconds,
            2.0 - 2.0 * seconds - 4.905 * seconds**2,
        ]
    )
    ts_ms = FLIGHT_START_MS + 1000.0 * seconds
    frame_nos = [FLIGHT_START_FRAME + i for i in range(N_FLIGHT)]
    return frame_nos, np.asarray(ts_ms, dtype=np.float64), xyz


def _payload(
    camera: Camera,
    *,
    segments: Iterable[Mapping[str, Any]] = SEGMENTS_A,
    noise: float = 0.0,
    seed: int = 7,
    skip: frozenset[int] = frozenset(),
    bridged: frozenset[int] = frozenset(),
    ts_shift: Mapping[int, float] | None = None,
    px_shift: Mapping[int, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Pinned US-F3 track payload for the ground-truth flight on one camera."""
    frame_nos, ts_ms, xyz = _flight()
    pixels = _project(camera, xyz)
    if noise:
        pixels = pixels + np.random.default_rng(seed).normal(0.0, noise, pixels.shape)
    points = []
    for i, frame_no in enumerate(frame_nos):
        if frame_no in skip:
            continue
        shift = (ts_shift or {}).get(frame_no, 0.0)
        dx, dy = (px_shift or {}).get(frame_no, (0.0, 0.0))
        points.append(
            {
                "frame_no": frame_no,
                "ts_ms": float(ts_ms[i]) + shift,
                "px_x": float(pixels[i, 0]) + dx,
                "px_y": float(pixels[i, 1]) + dy,
                "score": 0.9,
                "bridged": frame_no in bridged,
            }
        )
    return {
        "points": points,
        "segments": [dict(segment) for segment in segments],
        "flags": {"identity_risk": False, "long_gap": False},
    }


# ---------------------------------------------------------------------------
# Stereo pair composition + validation
# ---------------------------------------------------------------------------


def test_pair_projection_matrices_and_baseline() -> None:
    camera_a = _camera_a(NO_DIST)
    camera_b = _camera_b(NO_DIST)
    pair = _pair_from_cameras(camera_a, camera_b)
    point = np.array([[10.0, 0.2, 1.4]])
    homogeneous = np.array([10.0, 0.2, 1.4, 1.0])
    for projection, camera in ((pair.projection_a, camera_a), (pair.projection_b, camera_b)):
        projected = projection @ homogeneous
        assert projected[:2] / projected[2] == pytest.approx(_project(camera, point)[0])
    overhead_position = np.array([10.06, 3.05 * 2.5, 4.5])
    assert pair.baseline_m == pytest.approx(float(np.linalg.norm(C1_POSITION - overhead_position)))
    assert pair.frame == PITCH_FRAME
    assert pair.camera_a == "C1"
    assert pair.camera_b == "C4"


def test_short_dist_coeffs_are_zero_padded() -> None:
    kwargs = _pair_kwargs()
    kwargs["intrinsics_a"] = dict(kwargs["intrinsics_a"], dist_coeffs=[-0.1])
    pair = make_stereo_pair(**kwargs)
    assert pair.dist_coeffs_a == (-0.1, 0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("camera_pair", "match"),
    [
        (("C1", "C1"), "distinct"),
        (("", "C4"), "non-empty"),
        (("C1",), "two non-empty"),
        ((1, "C4"), "two non-empty"),
    ],
)
def test_camera_pair_validation(camera_pair: tuple[Any, ...], match: str) -> None:
    kwargs = _pair_kwargs()
    kwargs["camera_pair"] = camera_pair
    with pytest.raises(TriangulationError, match=match):
        make_stereo_pair(**kwargs)


def _bad_intrinsics_cases() -> list[tuple[Any, str]]:
    good = _intrinsics(_camera_a())
    bad_bottom = _camera_a()[0].copy()
    bad_bottom[2, 2] = 2.0
    bad_focal = _camera_a()[0].copy()
    bad_focal[0, 0] = -1.0
    nan_matrix = _camera_a()[0].copy()
    nan_matrix[0, 2] = math.nan
    return [
        (42, "must be a mapping"),
        ({"dist_coeffs": [0.0]}, "must contain"),
        ({"camera_matrix": good["camera_matrix"]}, "must contain"),
        (dict(good, camera_matrix=[[1.0, 0.0], [0.0, 1.0]]), "shape"),
        (dict(good, camera_matrix="junk"), "numeric"),
        (dict(good, camera_matrix=nan_matrix.tolist()), "non-finite"),
        (dict(good, camera_matrix=bad_bottom.tolist()), "bottom row"),
        (dict(good, camera_matrix=bad_focal.tolist()), "focal"),
        (dict(good, dist_coeffs=[[0.1, 0.2]]), "flat sequence"),
        (dict(good, dist_coeffs=[0.1] * 6), "at most 5"),
        (dict(good, dist_coeffs=[math.inf]), "non-finite"),
        (dict(good, dist_coeffs="junk"), "flat sequence"),
    ]


@pytest.mark.parametrize(("block", "match"), _bad_intrinsics_cases())
def test_intrinsics_block_validation(block: Any, match: str) -> None:
    kwargs = _pair_kwargs()
    kwargs["intrinsics_b"] = block
    with pytest.raises(TriangulationError, match=match):
        make_stereo_pair(**kwargs)


@pytest.mark.parametrize(
    ("transform", "match"),
    [
        (RigidTransform(rotation=2.0 * np.eye(3), translation=(0.0, 1.0, 2.0)), "orthonormal"),
        (
            RigidTransform(rotation=np.diag([1.0, 1.0, -1.0]), translation=(0.0, 1.0, 2.0)),
            "reflection",
        ),
        (RigidTransform(rotation=np.eye(2), translation=(0.0, 1.0, 2.0)), "shape"),
        (
            RigidTransform(rotation=np.full((3, 3), math.nan), translation=(0.0, 1.0, 2.0)),
            "non-finite",
        ),
        (RigidTransform(rotation=np.eye(3), translation=(0.0, 1.0)), "shape"),
        (RigidTransform(rotation=np.eye(3), translation=(math.nan, 0.0, 1.0)), "non-finite"),
    ],
)
def test_pose_validation(transform: RigidTransform, match: str) -> None:
    kwargs = _pair_kwargs()
    kwargs["a_to_b"] = transform
    with pytest.raises(TriangulationError, match=match):
        make_stereo_pair(**kwargs)


def test_zero_baseline_is_degenerate_geometry() -> None:
    kwargs = _pair_kwargs()
    kwargs["a_to_b"] = RigidTransform(
        rotation=np.eye(3), translation=(MIN_BASELINE_M / 2, 0.0, 0.0)
    )
    with pytest.raises(StereoGeometryError, match="cannot triangulate"):
        make_stereo_pair(**kwargs)


# ---------------------------------------------------------------------------
# Params round-trip
# ---------------------------------------------------------------------------


def test_params_round_trip() -> None:
    pair = _pair_from_cameras(_camera_a(), _camera_b())
    params = to_params(pair)
    assert params["version"] == PARAMS_VERSION
    assert params["camera_pair"] == ["C1", "C4"]
    assert from_params(params) == pair


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"version": 2}, "version"),
        ({"frame": "camera_a"}, "frame contract"),
        ({"camera_pair": "C1C4"}, "sequence of two ids"),
        ({"camera_pair": 42}, "sequence of two ids"),
        ({"rotation": None}, "missing key"),
    ],
)
def test_from_params_rejects_malformed(mutation: dict[str, Any], match: str) -> None:
    params = to_params(_pair_from_cameras(_camera_a(), _camera_b()))
    for key, value in mutation.items():
        if value is None:
            del params[key]
        else:
            params[key] = value
    with pytest.raises(TriangulationError, match=match):
        from_params(params)


# ---------------------------------------------------------------------------
# DLT triangulation vs synthetic stereo scenes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_distortion", [False, True])
def test_exact_scene_recovers_ground_truth(with_distortion: bool) -> None:
    camera_a = _camera_a(DIST_A if with_distortion else NO_DIST)
    camera_b = _camera_b(DIST_B if with_distortion else NO_DIST)
    pair = _pair_from_cameras(camera_a, camera_b)
    _frames, _ts, xyz = _flight()
    pixels_a = [(float(u), float(v)) for u, v in _project(camera_a, xyz)]
    pixels_b = [(float(u), float(v)) for u, v in _project(camera_b, xyz)]
    results = triangulate_points(pair, pixels_a, pixels_b)
    assert len(results) == N_FLIGHT
    for truth, result in zip(xyz, results, strict=True):
        assert result.valid
        assert result.reason is None
        assert result.xyz == pytest.approx(tuple(truth), abs=1e-6)
        assert result.reprojection_px_a == pytest.approx(0.0, abs=1e-6)
        assert result.reprojection_px_b == pytest.approx(0.0, abs=1e-6)


def _noisy_mean_error(sigma: float) -> tuple[float, float]:
    """(mean 3D error m, mean reprojection px) for seeded pixel noise ``sigma``."""
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    _frames, _ts, xyz = _flight()
    rng = np.random.default_rng(42)
    pixels_a = _project(camera_a, xyz) + rng.normal(0.0, sigma, (N_FLIGHT, 2))
    pixels_b = _project(camera_b, xyz) + rng.normal(0.0, sigma, (N_FLIGHT, 2))
    results = triangulate_points(
        pair,
        [(float(u), float(v)) for u, v in pixels_a],
        [(float(u), float(v)) for u, v in pixels_b],
    )
    errors_3d = []
    errors_px = []
    for truth, result in zip(xyz, results, strict=True):
        assert result.valid
        assert result.xyz is not None
        assert result.reprojection_px_a is not None
        assert result.reprojection_px_b is not None
        errors_3d.append(float(np.linalg.norm(np.asarray(result.xyz) - truth)))
        errors_px.append(max(result.reprojection_px_a, result.reprojection_px_b))
    return float(np.mean(errors_3d)), float(np.mean(errors_px))


def test_noise_tolerance_and_honest_rms() -> None:
    error_low, rms_low = _noisy_mean_error(0.5)
    error_high, rms_high = _noisy_mean_error(2.0)
    assert 0.0 < error_low < 0.05  # well inside the 5 cm rig-test target
    assert rms_low > 0.0
    assert rms_high > rms_low  # more noise MUST report worse, never hide it
    assert error_high > error_low


def _facing_pair() -> tuple[StereoPair, Camera, Camera]:
    """C2/C3-style pair facing each other down the pitch (no distortion)."""
    rotation_a, translation_a = _look_at(np.array([-3.0, 0.0, 1.0]), np.array([10.0, 0.0, 1.0]))
    rotation_b, translation_b = _look_at(np.array([23.0, 0.0, 1.0]), np.array([10.0, 0.0, 1.0]))
    camera_a: Camera = (K_A.copy(), NO_DIST.copy(), rotation_a, translation_a)
    camera_b: Camera = (K_A.copy(), NO_DIST.copy(), rotation_b, translation_b)
    return _pair_from_cameras(camera_a, camera_b, ids=("C2", "C3")), camera_a, camera_b


@pytest.mark.parametrize(
    ("point", "reason"),
    [
        ((-8.0, 0.3, 1.2), "behind_camera_a"),
        ((30.0, 0.5, 1.2), "behind_camera_b"),
    ],
)
def test_behind_camera_points_flagged(point: tuple[float, float, float], reason: str) -> None:
    pair, camera_a, camera_b = _facing_pair()
    xyz = np.array([point])
    pixel_a = _px(_project(camera_a, xyz)[0])
    pixel_b = _px(_project(camera_b, xyz)[0])
    (result,) = triangulate_points(pair, [pixel_a], [pixel_b])
    assert not result.valid
    assert result.reason == reason
    assert result.xyz is not None  # the geometric solution is reported for debugging
    assert result.reprojection_px_a is None
    assert result.reprojection_px_b is None


def test_near_parallel_rays_flag_low_parallax() -> None:
    rotation_a, translation_a = _look_at(np.array([0.0, 6.0, 1.5]), np.array([10.06, 0.0, 1.0]))
    rotation_b, translation_b = _look_at(np.array([0.02, 6.0, 1.5]), np.array([10.08, 0.0, 1.0]))
    camera_a: Camera = (K_A.copy(), NO_DIST.copy(), rotation_a, translation_a)
    camera_b: Camera = (K_A.copy(), NO_DIST.copy(), rotation_b, translation_b)
    pair = _pair_from_cameras(camera_a, camera_b, ids=("C1", "C5"))
    forward = np.array([10.06, -6.0, -0.5])
    far_point = (np.array([0.0, 6.0, 1.5]) + 50.0 * forward / np.linalg.norm(forward))[None, :]
    pixel_a = _px(_project(camera_a, far_point)[0])
    pixel_b = _px(_project(camera_b, far_point)[0])
    (result,) = triangulate_points(pair, [pixel_a], [pixel_b])
    assert not result.valid
    assert result.reason == "low_parallax"
    assert result.xyz is None


def test_exactly_parallel_rays_hit_infinity_guard() -> None:
    k = [[1000.0, 0.0, 500.0], [0.0, 1000.0, 400.0], [0.0, 0.0, 1.0]]
    intrinsics = {"camera_matrix": k, "dist_coeffs": [0.0]}
    pair = make_stereo_pair(
        camera_pair=("C1", "C4"),
        intrinsics_a=intrinsics,
        intrinsics_b=intrinsics,
        a_to_b=RigidTransform(rotation=np.eye(3), translation=(-0.5, 0.0, 0.0)),
        pitch_to_a=RigidTransform(rotation=np.eye(3), translation=(0.0, 0.0, 0.0)),
    )
    (result,) = triangulate_points(pair, [(500.0, 400.0)], [(500.0, 400.0)])
    assert not result.valid
    assert result.reason == "low_parallax"
    assert result.xyz is None


def test_mismatched_correspondence_flags_high_reprojection() -> None:
    camera_a = _camera_a(NO_DIST)
    camera_b = _camera_b(NO_DIST)
    pair = _pair_from_cameras(camera_a, camera_b)
    pixel_a = _px(_project(camera_a, np.array([[10.0, 0.0, 1.5]]))[0])
    pixel_b = _px(_project(camera_b, np.array([[9.5, 0.3, 1.3]]))[0])
    (result,) = triangulate_points(pair, [pixel_a], [pixel_b])
    assert not result.valid
    assert result.reason == "high_reprojection"
    assert result.xyz is not None
    assert result.reprojection_px_a is not None
    assert result.reprojection_px_b is not None
    assert max(result.reprojection_px_a, result.reprojection_px_b) > MAX_REPROJECTION_PX
    # A permissive gate accepts the same pair — the threshold is what flags it.
    (loose,) = triangulate_points(pair, [pixel_a], [pixel_b], max_reprojection_px=1e6)
    assert loose.valid


def test_triangulate_points_input_validation() -> None:
    pair = _pair_from_cameras(_camera_a(), _camera_b())
    assert triangulate_points(pair, [], []) == []
    with pytest.raises(TriangulationError, match="lengths differ"):
        triangulate_points(pair, [(0.0, 0.0)], [])
    with pytest.raises(TriangulationError, match="non-finite"):
        triangulate_points(pair, [(math.nan, 0.0)], [(0.0, 0.0)])
    with pytest.raises(TriangulationError, match="shape"):
        triangulate_points(pair, [(1.0, 2.0, 3.0)], [(0.0, 0.0)])  # type: ignore[list-item]
    with pytest.raises(TriangulationError, match="positive"):
        triangulate_points(pair, [(0.0, 0.0)], [(0.0, 0.0)], max_reprojection_px=0.0)
    with pytest.raises(TriangulationError, match="finite number"):
        triangulate_points(pair, [(0.0, 0.0)], [(0.0, 0.0)], max_reprojection_px=math.nan)


# ---------------------------------------------------------------------------
# Track triangulation (pinned US-F3 payload contract)
# ---------------------------------------------------------------------------


def test_track_full_recovery_with_quality_report() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    track_a = _payload(camera_a, segments=SEGMENTS_A)
    track_b = _payload(camera_b, segments=SEGMENTS_B)
    track = triangulate_track(pair, track_a, track_b)
    assert track == triangulate_track(pair, track_a, track_b)  # deterministic
    _frames, ts_ms, xyz = _flight()
    assert len(track.points) == N_FLIGHT
    for point, truth, ts in zip(track.points, xyz, ts_ms, strict=True):
        assert (point.x, point.y, point.z) == pytest.approx(tuple(truth), abs=1e-6)
        assert point.ts_ms == pytest.approx(float(ts))
    quality = track.quality
    assert quality.matched_fraction == 1.0
    assert quality.n_frames_union == N_FLIGHT
    assert quality.n_points_3d == N_FLIGHT
    assert quality.n_dropped_unmatched == 0
    assert quality.n_dropped_skewed == 0
    assert quality.n_dropped_bridged == 0
    assert quality.n_dropped_invalid == 0
    assert not quality.low_overlap
    assert quality.rms_reprojection_px is not None
    assert quality.rms_reprojection_px == pytest.approx(0.0, abs=1e-6)
    pre, post = quality.segments
    assert (pre.kind, post.kind) == ("pre_bounce", "post_bounce")
    assert pre.n_points == 13  # ts 12000..12100 inclusive
    assert post.n_points == 12  # ts 12100..12191.67
    assert pre.confidence == pytest.approx(0.8)  # min(0.9, 0.8) * full coverage
    assert post.confidence == pytest.approx(0.7)


def test_track_unmatched_frames_dropped_never_interpolated() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    only_in_b = frozenset({FLIGHT_START_FRAME + 20, FLIGHT_START_FRAME + 21})
    only_in_a = frozenset({FLIGHT_START_FRAME + 5, FLIGHT_START_FRAME + 6, FLIGHT_START_FRAME + 7})
    track = triangulate_track(
        pair,
        _payload(camera_a, skip=only_in_b),
        _payload(camera_b, segments=SEGMENTS_B, skip=only_in_a),
    )
    assert track.quality.n_frames_union == N_FLIGHT
    assert track.quality.n_dropped_unmatched == 5
    assert track.quality.n_points_3d == N_FLIGHT - 5
    assert track.quality.matched_fraction == pytest.approx((N_FLIGHT - 5) / N_FLIGHT)
    dropped = only_in_a | only_in_b
    assert {point.frame_no for point in track.points} == {
        frame
        for frame in range(FLIGHT_START_FRAME, FLIGHT_START_FRAME + N_FLIGHT)
        if frame not in dropped
    }


def test_track_skewed_and_bridged_frames_dropped() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    skewed = {FLIGHT_START_FRAME + 2: MAX_SYNC_SKEW_MS * 2, FLIGHT_START_FRAME + 3: -25.0}
    bridged_a = frozenset({FLIGHT_START_FRAME + 8})
    bridged_b = frozenset({FLIGHT_START_FRAME + 15, FLIGHT_START_FRAME + 16})
    track = triangulate_track(
        pair,
        _payload(camera_a, bridged=bridged_a),
        _payload(camera_b, segments=SEGMENTS_B, ts_shift=skewed, bridged=bridged_b),
    )
    assert track.quality.n_dropped_skewed == 2
    assert track.quality.n_dropped_bridged == 3
    assert track.quality.n_points_3d == N_FLIGHT - 5
    excluded = set(skewed) | bridged_a | bridged_b
    assert excluded.isdisjoint({point.frame_no for point in track.points})


def test_track_invalid_3d_points_counted() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    corrupted = FLIGHT_START_FRAME + 10
    track = triangulate_track(
        pair,
        _payload(camera_a),
        _payload(camera_b, segments=SEGMENTS_B, px_shift={corrupted: (300.0, -200.0)}),
    )
    assert track.quality.n_dropped_invalid == 1
    assert track.quality.n_points_3d == N_FLIGHT - 1
    assert corrupted not in {point.frame_no for point in track.points}


def test_track_all_skewed_reports_empty_and_low_overlap() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    all_shift = dict.fromkeys(range(FLIGHT_START_FRAME, FLIGHT_START_FRAME + N_FLIGHT), 25.0)
    track = triangulate_track(
        pair,
        _payload(camera_a),
        _payload(camera_b, segments=SEGMENTS_B, ts_shift=all_shift),
    )
    assert track.points == ()
    assert track.quality.n_dropped_skewed == N_FLIGHT
    assert track.quality.matched_fraction == 0.0
    assert track.quality.rms_reprojection_px is None
    assert track.quality.low_overlap
    for segment in track.quality.segments:
        assert segment.confidence == 0.0
        assert segment.n_points == 0


def test_track_low_overlap_flag() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    keep_every_4th = frozenset(
        frame
        for frame in range(FLIGHT_START_FRAME, FLIGHT_START_FRAME + N_FLIGHT)
        if (frame - FLIGHT_START_FRAME) % 4
    )
    track = triangulate_track(
        pair,
        _payload(camera_a),
        _payload(camera_b, segments=SEGMENTS_B, skip=keep_every_4th),
    )
    assert track.quality.matched_fraction < LOW_OVERLAP_FRACTION
    assert track.quality.low_overlap


def test_track_segment_kind_matching_and_disjoint_windows() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    segments_a = (
        {"kind": "pre_bounce", "start_ms": 12000.0, "end_ms": 12050.0, "confidence": 0.9},
        {"kind": "post_contact", "start_ms": 12150.0, "end_ms": 12200.0, "confidence": 0.6},
    )
    segments_b = (
        {"kind": "pre_bounce", "start_ms": 12060.0, "end_ms": 12100.0, "confidence": 0.8},
    )
    track = triangulate_track(
        pair,
        _payload(camera_a, segments=segments_a),
        _payload(camera_b, segments=segments_b),
    )
    (pre,) = track.quality.segments  # post_contact absent from camera B -> not reported
    assert pre.kind == "pre_bounce"
    assert pre.start_ms == 12060.0
    assert pre.end_ms == 12050.0  # inverted window signals no temporal overlap
    assert pre.confidence == 0.0
    assert pre.n_points == 0


def test_track_long_gap_split_same_kind_segments_merge_into_envelopes() -> None:
    """A >5-frame occlusion legally splits one phase into several same-kind
    segments (pinned US-F3 tracker behavior): the parser must accept them and
    the report merges each camera's same-kind windows into their envelope."""
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    segments_a = (
        {"kind": "pre_bounce", "start_ms": 12000.0, "end_ms": 12040.0, "confidence": 0.9},
        {"kind": "pre_bounce", "start_ms": 12060.0, "end_ms": 12100.0, "confidence": 0.85},
        {"kind": "post_bounce", "start_ms": 12100.0, "end_ms": 12200.0, "confidence": 0.7},
    )
    segments_b = (
        {"kind": "pre_bounce", "start_ms": 12000.0, "end_ms": 12100.0, "confidence": 0.8},
        {"kind": "post_bounce", "start_ms": 12100.0, "end_ms": 12140.0, "confidence": 0.75},
        {"kind": "post_bounce", "start_ms": 12160.0, "end_ms": 12200.0, "confidence": 0.72},
    )
    track = triangulate_track(
        pair,
        _payload(camera_a, segments=segments_a),
        _payload(camera_b, segments=segments_b),
    )
    assert len(track.points) == N_FLIGHT  # every matched pair triangulates fine
    pre, post = track.quality.segments
    assert (pre.kind, post.kind) == ("pre_bounce", "post_bounce")
    assert (pre.start_ms, pre.end_ms) == (12000.0, 12100.0)  # A's envelope ∩ B
    assert pre.n_points == 13
    assert pre.confidence == pytest.approx(0.8)  # weakest of A (0.9/0.85) and B (0.8)
    assert (post.start_ms, post.end_ms) == (12100.0, 12200.0)
    assert post.n_points == 12
    assert post.confidence == pytest.approx(0.7)  # weakest of A (0.7) and B (0.75/0.72)


def test_track_empty_payloads() -> None:
    pair = _pair_from_cameras(_camera_a(), _camera_b())
    empty: dict[str, Any] = {"points": [], "segments": []}
    track = triangulate_track(pair, empty, empty)
    assert track.points == ()
    assert track.quality.n_frames_union == 0
    assert track.quality.matched_fraction == 0.0
    assert track.quality.rms_reprojection_px is None
    assert track.quality.low_overlap
    assert track.quality.segments == ()


def _valid_point(frame_no: int = 1440) -> dict[str, Any]:
    return {
        "frame_no": frame_no,
        "ts_ms": 12000.0,
        "px_x": 100.0,
        "px_y": 200.0,
        "score": 0.9,
        "bridged": False,
    }


def _valid_segment() -> dict[str, Any]:
    return {"kind": "pre_bounce", "start_ms": 12000.0, "end_ms": 12100.0, "confidence": 0.9}


def _bad_payload_cases() -> list[tuple[Any, str]]:
    return [
        (42, "payload mapping"),
        ({"segments": []}, "points must be a list"),
        ({"points": {}, "segments": []}, "points must be a list"),
        ({"points": "junk", "segments": []}, "points must be a list"),
        ({"points": [], "segments": None}, "segments must be a list"),
        ({"points": [7], "segments": []}, r"points\[0\] must be a mapping"),
        ({"points": [dict(_valid_point(), frame_no=None)], "segments": []}, "frame_no"),
        ({"points": [dict(_valid_point(), frame_no=True)], "segments": []}, "frame_no"),
        ({"points": [dict(_valid_point(), frame_no=1.5)], "segments": []}, "frame_no"),
        ({"points": [dict(_valid_point(), ts_ms=math.nan)], "segments": []}, "ts_ms"),
        ({"points": [dict(_valid_point(), px_x=None)], "segments": []}, "px_x"),
        ({"points": [dict(_valid_point(), px_y="9")], "segments": []}, "px_y"),
        ({"points": [dict(_valid_point(), score=1.5)], "segments": []}, "score"),
        ({"points": [dict(_valid_point(), score=-0.1)], "segments": []}, "score"),
        ({"points": [dict(_valid_point(), bridged=None)], "segments": []}, "bridged"),
        ({"points": [dict(_valid_point(), bridged="no")], "segments": []}, "bridged"),
        ({"points": [_valid_point(), _valid_point()], "segments": []}, "duplicate frame_no"),
        ({"points": [], "segments": [7]}, r"segments\[0\] must be a mapping"),
        ({"points": [], "segments": [dict(_valid_segment(), kind=None)]}, "kind"),
        ({"points": [], "segments": [dict(_valid_segment(), kind="flight")]}, "kind"),
        (
            {"points": [], "segments": [dict(_valid_segment(), start_ms=12200.0)]},
            "start_ms must be <=",
        ),
        ({"points": [], "segments": [dict(_valid_segment(), confidence=2.0)]}, "confidence"),
    ]


@pytest.mark.parametrize(("payload", "match"), _bad_payload_cases())
def test_track_payload_validation(payload: Any, match: str) -> None:
    pair = _pair_from_cameras(_camera_a(), _camera_b())
    good: dict[str, Any] = {"points": [], "segments": []}
    with pytest.raises(TriangulationError, match=match):
        triangulate_track(pair, payload, good)
    with pytest.raises(TriangulationError, match="track_b"):
        triangulate_track(pair, good, payload)


# ---------------------------------------------------------------------------
# Release height
# ---------------------------------------------------------------------------


def _hand_track(points: list[tuple[float, float, float]]) -> Track3D:
    """Track3D from (ts_ms, z, reprojection_px) triples; x/y are irrelevant."""
    track_points = tuple(
        Track3DPoint(frame_no=i, ts_ms=ts, x=10.0, y=0.0, z=z, reprojection_px=reproj)
        for i, (ts, z, reproj) in enumerate(points)
    )
    quality = TrackQuality3D(
        rms_reprojection_px=0.0,
        matched_fraction=1.0,
        n_frames_union=len(track_points),
        n_points_3d=len(track_points),
        n_dropped_unmatched=0,
        n_dropped_skewed=0,
        n_dropped_bridged=0,
        n_dropped_invalid=0,
        low_overlap=False,
        segments=(),
    )
    return Track3D(points=track_points, quality=quality)


def test_release_height_from_triangulated_flight() -> None:
    camera_a = _camera_a()
    camera_b = _camera_b()
    pair = _pair_from_cameras(camera_a, camera_b)
    track = triangulate_track(pair, _payload(camera_a), _payload(camera_b, segments=SEGMENTS_B))
    result = release_height(track, FLIGHT_START_MS)
    assert result == release_height(track, FLIGHT_START_MS)  # reproducible
    assert result.reason is None
    assert result.value_m is not None
    assert result.value_m == pytest.approx(2.0, abs=0.01)  # ground-truth release z
    assert result.n_points == 7  # one-sided 50 ms window at flight start
    assert result.confidence > 0.9


def test_release_height_linear_fit_is_exact_on_linear_z() -> None:
    track = _hand_track([(11960.0 + 20.0 * i, 1.8 + 0.01 * i, 0.0) for i in range(5)])
    result = release_height(track, 12000.0)
    assert result.value_m == pytest.approx(1.8 + 0.01 * 2.0)  # interpolated at release
    assert result.n_points == 5
    assert result.confidence == pytest.approx(1.0)


def test_release_height_no_points_in_window() -> None:
    track = _hand_track([(15000.0, 1.5, 0.0)])
    result = release_height(track, 12000.0)
    assert result == ReleaseHeight(
        value_m=None, confidence=0.0, n_points=0, reason="no 3D points within 50 ms of release"
    )


def test_release_height_single_point_uses_plain_mean() -> None:
    track = _hand_track([(12010.0, 2.15, 1.0)])
    result = release_height(track, 12000.0)
    assert result.value_m == pytest.approx(2.15)
    assert result.n_points == 1
    assert result.confidence == pytest.approx((1 / 4) / (1.0 + 1.0))


def test_release_height_identical_timestamps_use_plain_mean() -> None:
    track = _hand_track([(12000.0, 1.0, 0.0), (12000.0, 2.0, 0.0)])
    result = release_height(track, 12000.0)
    assert result.value_m == pytest.approx(1.5)


def test_release_height_confidence_monotone_in_quality_and_count() -> None:
    sharp = release_height(_hand_track([(12000.0 + i, 2.0, 0.0) for i in range(4)]), 12000.0)
    blurry = release_height(_hand_track([(12000.0 + i, 2.0, 2.0) for i in range(4)]), 12000.0)
    sparse = release_height(_hand_track([(12000.0 + i, 2.0, 0.0) for i in range(2)]), 12000.0)
    assert sharp.confidence > blurry.confidence  # worse reprojection -> less confident
    assert sharp.confidence > sparse.confidence  # fewer points -> less confident


def test_release_height_validation() -> None:
    track = _hand_track([(12000.0, 2.0, 0.0)])
    with pytest.raises(TriangulationError, match="positive"):
        release_height(track, 12000.0, window_ms=0.0)
    with pytest.raises(TriangulationError, match="positive"):
        release_height(track, 12000.0, window_ms=-5.0)
    with pytest.raises(TriangulationError, match="finite number"):
        release_height(track, math.nan)
    with pytest.raises(TriangulationError, match="finite number"):
        release_height(track, 12000.0, window_ms=math.nan)
