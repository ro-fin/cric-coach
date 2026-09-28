"""US-C1: ChArUco intrinsic calibration on synthetic board sweeps + CLI wrapper."""

import importlib.util
import json
import math
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt
import pytest
from cricai_vision.intrinsics import (
    PARAMS_VERSION,
    BoardSpec,
    InsufficientCoverageError,
    IntrinsicsResult,
    calibrate_from_images,
    from_params,
    generate_board_image,
    to_params,
    undistort_pixel,
    undistort_pixels,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

BOARD = BoardSpec()
PX_PER_SQUARE = 160
PX_PER_M = PX_PER_SQUARE / BOARD.square_len_m
VIEW_SIZE = (1280, 720)  # (width, height)
TRUE_FX, TRUE_FY, TRUE_CX, TRUE_CY = 1200.0, 1150.0, 660.0, 350.0
TRUE_K = np.array([[TRUE_FX, 0.0, TRUE_CX], [0.0, TRUE_FY, TRUE_CY], [0.0, 0.0, 1.0]])
N_VIEWS = 12
CAPTURED_ON = "2026-07-06"


def _pose(i: int) -> tuple[list[float], list[float]]:
    """Deterministic board pose sweep: varied rotation, translation and scale."""
    rvec = [0.35 * math.sin(i), 0.3 * math.cos(1.3 * i + 0.5), 0.15 * math.sin(2.1 * i)]
    tvec = [
        -BOARD.squares_x * BOARD.square_len_m / 2 + 0.06 * math.sin(0.7 * i),
        -BOARD.squares_y * BOARD.square_len_m / 2 + 0.06 * math.cos(1.1 * i),
        1.1 + 0.2 * math.sin(0.9 * i),
    ]
    return rvec, tvec


def _homography(rvec: list[float], tvec: list[float]) -> npt.NDArray[np.float64]:
    """Board-image pixels -> camera pixels for a distortion-free pinhole pose."""
    rotation, _ = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64))
    columns = np.column_stack(
        [rotation[:, 0] / PX_PER_M, rotation[:, 1] / PX_PER_M, np.asarray(tvec, dtype=np.float64)]
    )
    return np.asarray(TRUE_K @ columns, dtype=np.float64)


@pytest.fixture(scope="module")
def board_image() -> npt.NDArray[np.uint8]:
    return generate_board_image(BOARD, px_per_square=PX_PER_SQUARE)


@pytest.fixture(scope="module")
def views(board_image: npt.NDArray[np.uint8]) -> list[npt.NDArray[Any]]:
    """N_VIEWS distortion-free renders of the board; view 0 is BGR, rest gray."""
    rendered: list[npt.NDArray[Any]] = [
        cv2.warpPerspective(
            board_image,
            _homography(*_pose(i)),
            VIEW_SIZE,
            flags=cv2.INTER_LINEAR,
            borderValue=255,
        )
        for i in range(N_VIEWS)
    ]
    rendered[0] = cv2.cvtColor(rendered[0], cv2.COLOR_GRAY2BGR)
    return rendered


@pytest.fixture(scope="module")
def result(views: list[npt.NDArray[Any]]) -> IntrinsicsResult:
    return calibrate_from_images(views, BOARD, captured_on=CAPTURED_ON)


def _blank(width: int = 1280, height: int = 720) -> npt.NDArray[np.uint8]:
    return np.full((height, width), 255, dtype=np.uint8)


def _masked_board(board_image: npt.NDArray[np.uint8], rows_visible: int) -> npt.NDArray[np.uint8]:
    """Board with all but the top ``rows_visible`` square-rows painted white."""
    masked = board_image.copy()
    masked[rows_visible * PX_PER_SQUARE :, :] = 255
    return masked


#: Known barrel lens for the distorted-sweep tests: k1, k2, p1, p2, k3.
KNOWN_DIST: tuple[float, float, float, float, float] = (-0.15, 0.05, 0.0, 0.0, 0.0)


def _distort_forward(
    uv: npt.NDArray[np.float64],
    camera_matrix: npt.NDArray[np.float64],
    dist: Sequence[float],
) -> npt.NDArray[np.float64]:
    """OpenCV forward distortion model (k1, k2, p1, p2, k3) on ideal pixels."""
    fx, fy = camera_matrix[0, 0], camera_matrix[1, 1]
    cx, cy = camera_matrix[0, 2], camera_matrix[1, 2]
    k1, k2, p1, p2, k3 = dist
    x = (uv[:, 0] - cx) / fx
    y = (uv[:, 1] - cy) / fy
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    x_d = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    y_d = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return np.column_stack([fx * x_d + cx, fy * y_d + cy])


def _board_corners_homogeneous_px() -> npt.NDArray[np.float64]:
    """All interior corner positions in board-image pixels, homogeneous."""
    board_xy = np.asarray(
        cv2.aruco.CharucoBoard(
            (BOARD.squares_x, BOARD.squares_y),
            BOARD.square_len_m,
            BOARD.marker_len_m,
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100),
        ).getChessboardCorners()
    )[:, :2]
    return np.column_stack([board_xy * PX_PER_M, np.ones(len(board_xy))])


# ---------------------------------------------------------------------------
# Calibration accuracy on synthetic sweeps
# ---------------------------------------------------------------------------


def test_recovers_known_intrinsics_within_tolerance(result: IntrinsicsResult) -> None:
    matrix = np.asarray(result.camera_matrix)
    assert abs(matrix[0, 0] - TRUE_FX) / TRUE_FX < 0.02
    assert abs(matrix[1, 1] - TRUE_FY) / TRUE_FY < 0.02
    assert abs(matrix[0, 2] - TRUE_CX) < 15.0
    assert abs(matrix[1, 2] - TRUE_CY) < 15.0
    assert matrix[2, 2] == 1.0


def test_meets_reprojection_error_acceptance(result: IntrinsicsResult) -> None:
    # US-C1 AC: mean reprojection error <= 1.0 px.
    assert 0.0 < result.reprojection_error_px <= 1.0


def test_result_provenance(result: IntrinsicsResult) -> None:
    assert result.n_views == N_VIEWS
    assert result.captured_on == CAPTURED_ON
    assert result.version == PARAMS_VERSION
    assert result.board_spec == BOARD
    assert len(result.dist_coeffs) >= 5


def test_recovered_distortion_is_near_zero(
    result: IntrinsicsResult, views: list[npt.NDArray[Any]]
) -> None:
    """Views were rendered without distortion; the model must agree where data was."""
    dist = np.asarray(result.dist_coeffs)
    assert abs(dist[0]) < 0.1  # k1
    assert abs(dist[2]) < 0.01 and abs(dist[3]) < 0.01  # tangential p1, p2

    # Undistorting the (analytically known) corner locations must be a no-op.
    board_px = _board_corners_homogeneous_px()
    observed: list[npt.NDArray[np.float64]] = []
    for i in range(N_VIEWS):
        projected = (_homography(*_pose(i)) @ board_px.T).T
        uv = projected[:, :2] / projected[:, 2:3]
        in_frame = (
            (uv[:, 0] >= 0)
            & (uv[:, 0] < VIEW_SIZE[0])
            & (uv[:, 1] >= 0)
            & (uv[:, 1] < VIEW_SIZE[1])
        )
        observed.append(uv[in_frame])
    points = np.vstack(observed).reshape(-1, 1, 2)
    camera_matrix = np.asarray(result.camera_matrix)
    undistorted = cv2.undistortPoints(points, camera_matrix, dist, P=camera_matrix)
    displacement = np.linalg.norm(
        np.asarray(undistorted).reshape(-1, 2) - points.reshape(-1, 2), axis=1
    )
    assert float(displacement.max()) < 0.75


# ---------------------------------------------------------------------------
# Distorted-board sweep: recovered distortion corrects a known lens (US-C1)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def distorted_views(views: list[npt.NDArray[Any]]) -> list[npt.NDArray[Any]]:
    """The 12-pose sweep pushed through the KNOWN_DIST barrel field.

    ``cv2.remap`` samples ``dst(p) = src(map(p))``, so synthesizing the
    distorted image needs the inverse map (distorted px -> ideal px). That is
    exactly what ``cv2.undistortPoints`` computes, evaluated once over the
    dense pixel grid and reused for every view: a corner whose ideal pixel is
    ``q`` then lands at forward-model position ``D(q)``, because
    ``dst(D(q)) = src(undistort(D(q))) = src(q)``.
    """
    width, height = VIEW_SIZE
    xs, ys = np.meshgrid(np.arange(width), np.arange(height))
    grid = np.column_stack([xs.ravel(), ys.ravel()]).astype(np.float64).reshape(-1, 1, 2)
    inverse = np.asarray(
        cv2.undistortPoints(grid, TRUE_K, np.asarray(KNOWN_DIST), P=TRUE_K),
        dtype=np.float32,
    ).reshape(height, width, 2)
    return [
        cv2.remap(
            view,
            inverse[..., 0],
            inverse[..., 1],
            cv2.INTER_LINEAR,
            borderValue=(255, 255, 255),
        )
        for view in views
    ]


@pytest.fixture(scope="module")
def distorted_result(distorted_views: list[npt.NDArray[Any]]) -> IntrinsicsResult:
    return calibrate_from_images(distorted_views, BOARD, captured_on=CAPTURED_ON)


def test_distorted_sweep_recovers_barrel_k1(distorted_result: IntrinsicsResult) -> None:
    """calibrate_from_images sees a known barrel lens and recovers its k1.

    Tolerance rationale: the 12-pose sweep has deliberately limited pose
    diversity (mild rotations near fronto-parallel), so k1 trades off against
    focal length and k2 in the joint fit; ~20% relative on k1 is what this
    geometry pins down. The guarantee that matters downstream is pixel-space
    correction, asserted to 1 px in the companion test below.
    """
    assert distorted_result.reprojection_error_px <= 1.0
    k1 = distorted_result.dist_coeffs[0]
    assert abs(k1 - KNOWN_DIST[0]) / abs(KNOWN_DIST[0]) < 0.2


def test_undistort_with_recovered_params_corrects_known_lens(
    distorted_result: IntrinsicsResult,
) -> None:
    """The downstream metric: recovered params undo the lens to within 1 px.

    Corner pixels are distorted ANALYTICALLY (forward model on the true
    projections), so this checks the recovered model against ground truth
    rather than against another cv2 inversion of itself.
    """
    params = to_params(distorted_result)
    board_px = _board_corners_homogeneous_px()
    ideal_all: list[npt.NDArray[np.float64]] = []
    distorted_all: list[npt.NDArray[np.float64]] = []
    for i in range(N_VIEWS):
        projected = (_homography(*_pose(i)) @ board_px.T).T
        ideal = projected[:, :2] / projected[:, 2:3]
        distorted = _distort_forward(ideal, TRUE_K, KNOWN_DIST)
        in_frame = (
            (distorted[:, 0] >= 0)
            & (distorted[:, 0] < VIEW_SIZE[0])
            & (distorted[:, 1] >= 0)
            & (distorted[:, 1] < VIEW_SIZE[1])
        )
        ideal_all.append(ideal[in_frame])
        distorted_all.append(distorted[in_frame])
    ideal_pts = np.vstack(ideal_all)
    distorted_pts = np.vstack(distorted_all)
    recovered = np.asarray(
        undistort_pixels(params, [(float(u), float(v)) for u, v in distorted_pts])
    )
    error = np.linalg.norm(recovered - ideal_pts, axis=1)
    assert float(error.max()) < 1.0


# ---------------------------------------------------------------------------
# Insufficient coverage: loud failures (US-C1 AC)
# ---------------------------------------------------------------------------


def test_blank_and_noise_views_fail_loudly() -> None:
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 256, size=(720, 1280), dtype=np.uint8)
    images = [_blank()] * 4 + [noise] * 4
    with pytest.raises(InsufficientCoverageError, match="only 0 of 8 views"):
        calibrate_from_images(images, BOARD, captured_on=CAPTURED_ON)


def test_empty_image_list_fails_loudly() -> None:
    with pytest.raises(InsufficientCoverageError, match="only 0 of 0 views"):
        calibrate_from_images([], BOARD, captured_on=CAPTURED_ON)


def test_too_few_detected_views_fail_loudly(views: list[npt.NDArray[Any]]) -> None:
    with pytest.raises(InsufficientCoverageError, match=r"only 3 of 3 views .* 8 such views"):
        calibrate_from_images(views[:3], BOARD, captured_on=CAPTURED_ON)


def test_views_with_too_few_corners_do_not_count(board_image: npt.NDArray[np.uint8]) -> None:
    # Two visible square-rows expose only 6 interior corners (< MIN_CORNERS_PER_VIEW).
    sliver = _masked_board(board_image, rows_visible=2)
    with pytest.raises(InsufficientCoverageError, match="only 0 of 8 views"):
        calibrate_from_images([sliver] * 8, BOARD, captured_on=CAPTURED_ON)


def test_low_aggregate_corner_coverage_fails_loudly(board_image: npt.NDArray[np.uint8]) -> None:
    # Four visible square-rows expose 18 of 54 corners in every view: 33% < 50%.
    partial = _masked_board(board_image, rows_visible=4)
    with pytest.raises(InsufficientCoverageError, match="collectively cover only 33%"):
        calibrate_from_images([partial] * 8, BOARD, captured_on=CAPTURED_ON)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_rejects_mismatched_view_sizes() -> None:
    with pytest.raises(ValueError, match="share one image size"):
        calibrate_from_images([_blank(1280, 720), _blank(640, 480)], BOARD, captured_on=CAPTURED_ON)


def test_rejects_nonpositive_min_views() -> None:
    with pytest.raises(ValueError, match="min_views must be >= 1"):
        calibrate_from_images([_blank()], BOARD, min_views=0, captured_on=CAPTURED_ON)


@pytest.mark.parametrize("coverage", [0.0, -0.5, 1.5])
def test_rejects_out_of_range_corner_coverage(coverage: float) -> None:
    with pytest.raises(ValueError, match="min_corner_coverage"):
        calibrate_from_images(
            [_blank()], BOARD, captured_on=CAPTURED_ON, min_corner_coverage=coverage
        )


@pytest.mark.parametrize("captured_on", ["", "not-a-date", "2026-13-40", "07/06/2026"])
def test_rejects_non_iso_captured_on(captured_on: str) -> None:
    with pytest.raises(ValueError, match="ISO date"):
        calibrate_from_images([_blank()], BOARD, captured_on=captured_on)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("squares_x", 2),
        ("squares_y", 2),
        ("marker_len_m", 0.0),
        ("marker_len_m", -0.06),
        ("square_len_m", 0.05),  # not > marker_len_m
        ("aruco_dict", "CharucoBoard"),  # real attribute, not a dictionary
        ("aruco_dict", "DICT_DOES_NOT_EXIST"),
    ],
)
def test_board_spec_validation(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        BoardSpec(**{field: value})  # type: ignore[arg-type]


def test_board_spec_interior_corners() -> None:
    assert BOARD.n_interior_corners == (BOARD.squares_x - 1) * (BOARD.squares_y - 1) == 54


def test_generate_board_image_geometry(board_image: npt.NDArray[np.uint8]) -> None:
    assert board_image.shape == (
        BOARD.squares_y * PX_PER_SQUARE,
        BOARD.squares_x * PX_PER_SQUARE,
    )
    assert board_image.dtype == np.uint8


def test_generate_board_image_rejects_nonpositive_scale() -> None:
    with pytest.raises(ValueError, match="px_per_square"):
        generate_board_image(BOARD, px_per_square=0)


# ---------------------------------------------------------------------------
# JSON params round-trip (stored in Calibration.params for kind=intrinsic)
# ---------------------------------------------------------------------------


def test_params_json_round_trip(result: IntrinsicsResult) -> None:
    params = to_params(result)
    assert params["version"] == PARAMS_VERSION
    restored = from_params(json.loads(json.dumps(params)))
    assert restored == result


def _valid_params(result: IntrinsicsResult) -> dict[str, Any]:
    params: dict[str, Any] = json.loads(json.dumps(to_params(result)))
    return params


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_from_params_rejects_wrong_version(result: IntrinsicsResult, version: object) -> None:
    params = _valid_params(result)
    if version is None:
        del params["version"]
    else:
        params["version"] = version
    with pytest.raises(ValueError, match="version"):
        from_params(params)


def _drop_key(params: dict[str, Any], key: str) -> dict[str, Any]:
    del params[key]
    return params


def _set_key(params: dict[str, Any], key: str, value: object) -> dict[str, Any]:
    params[key] = value
    return params


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: _drop_key(p, "camera_matrix"),
        lambda p: _drop_key(p, "board_spec"),
        lambda p: _drop_key(p, "captured_on"),
        lambda p: _set_key(p, "camera_matrix", [[1.0, 0.0], [0.0, 1.0]]),
        lambda p: _set_key(p, "camera_matrix", [[1.0, 0.0], [0.0]]),
        lambda p: _set_key(p, "dist_coeffs", "abc"),
        lambda p: _set_key(p, "n_views", "many"),
        lambda p: _set_key(p, "captured_on", "someday"),
        lambda p: _set_key(p, "board_spec", {"squares_x": 7}),
        lambda p: _set_key(
            p,
            "board_spec",
            {
                "squares_x": 7,
                "squares_y": 10,
                "square_len_m": 0.05,
                "marker_len_m": 0.06,
                "aruco_dict": "DICT_5X5_100",
            },
        ),
    ],
)
def test_from_params_rejects_malformed_payloads(result: IntrinsicsResult, mutate: Any) -> None:
    params = mutate(_valid_params(result))
    with pytest.raises(ValueError, match="malformed intrinsics params"):
        from_params(params)


# ---------------------------------------------------------------------------
# undistort_pixel / undistort_pixels: intrinsics applied to click coordinates
# ---------------------------------------------------------------------------


def _params_with_dist(dist: tuple[float, ...]) -> dict[str, Any]:
    """A valid intrinsics params payload with the true K and chosen coeffs."""
    return to_params(
        IntrinsicsResult(
            camera_matrix=(
                (TRUE_FX, 0.0, TRUE_CX),
                (0.0, TRUE_FY, TRUE_CY),
                (0.0, 0.0, 1.0),
            ),
            dist_coeffs=dist,
            reprojection_error_px=0.3,
            board_spec=BOARD,
            n_views=N_VIEWS,
            captured_on=CAPTURED_ON,
        )
    )


def test_undistort_zero_coeffs_is_identity() -> None:
    params = _params_with_dist((0.0, 0.0, 0.0, 0.0, 0.0))
    pixels = [(0.0, 0.0), (TRUE_CX, TRUE_CY), (1279.0, 719.0), (12.5, 703.25)]
    for px, out in zip(pixels, undistort_pixels(params, pixels), strict=True):
        assert out == pytest.approx(px, abs=1e-6)
    assert undistort_pixel(params, (400.25, 300.75)) == pytest.approx((400.25, 300.75), abs=1e-6)


def test_undistort_pixels_empty_input() -> None:
    assert undistort_pixels(_params_with_dist((0.0,) * 5), []) == []


def test_undistort_pixel_inverts_analytic_forward_model() -> None:
    """Distort ideal pixels with the forward model; undistort_pixel undoes it."""
    dist = (-0.12, 0.03, 0.0015, -0.0008, 0.01)
    params = _params_with_dist(dist)
    ideal = np.array(
        [[TRUE_CX, TRUE_CY], [200.0, 120.0], [1100.0, 640.0], [340.5, 500.25], [30.0, 690.0]]
    )
    distorted = _distort_forward(ideal, TRUE_K, dist)
    for ideal_px, dist_px in zip(ideal, distorted, strict=True):
        recovered = undistort_pixel(params, (float(dist_px[0]), float(dist_px[1])))
        assert recovered == pytest.approx((float(ideal_px[0]), float(ideal_px[1])), abs=0.5)


def test_undistort_displaces_off_center_pixels_under_distortion() -> None:
    """Non-zero coeffs must actually move an off-center pixel (not a no-op)."""
    params = _params_with_dist((-0.15, 0.05, 0.0, 0.0, 0.0))
    moved = undistort_pixel(params, (100.0, 80.0))
    assert math.hypot(moved[0] - 100.0, moved[1] - 80.0) > 5.0
    # The principal point is a fixed point of the radial model.
    center = undistort_pixel(params, (TRUE_CX, TRUE_CY))
    assert center == pytest.approx((TRUE_CX, TRUE_CY), abs=1e-6)


def test_undistort_rejects_malformed_params(result: IntrinsicsResult) -> None:
    params = _valid_params(result)
    del params["camera_matrix"]
    with pytest.raises(ValueError, match="malformed intrinsics params"):
        undistort_pixel(params, (10.0, 10.0))
    with pytest.raises(ValueError, match="version"):
        undistort_pixels({"version": 99}, [(10.0, 10.0)])


# ---------------------------------------------------------------------------
# CLI: scripts/calibrate_cameras.py (thin wrapper)
# ---------------------------------------------------------------------------


def _load_cli() -> ModuleType:
    path = REPO_ROOT / "scripts" / "calibrate_cameras.py"
    spec = importlib.util.spec_from_file_location("calibrate_cameras", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_calibrates_image_directory(
    views: list[npt.NDArray[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image_dir = tmp_path / "frames"
    image_dir.mkdir()
    for i, view in enumerate(views):
        assert cv2.imwrite(str(image_dir / f"view_{i:02d}.png"), view)
    (image_dir / "notes.txt").write_text("not an image")
    out = tmp_path / "camera.json"

    rc = _load_cli().main(
        ["--images", str(image_dir), "--captured-on", CAPTURED_ON, "--out", str(out)]
    )

    assert rc == 0
    assert f"wrote {out}" in capsys.readouterr().out
    restored = from_params(json.loads(out.read_text()))
    assert restored.reprojection_error_px <= 1.0
    assert restored.n_views == N_VIEWS
    assert restored.captured_on == CAPTURED_ON
    assert restored.board_spec == BOARD


def test_cli_calibrates_video_sweep(
    views: list[npt.NDArray[Any]],
    tmp_path: Path,
) -> None:
    video = tmp_path / "sweep.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter.fourcc(*"mp4v"), 30.0, VIEW_SIZE)
    assert writer.isOpened()
    for view in views:
        writer.write(view if view.ndim == 3 else cv2.cvtColor(view, cv2.COLOR_GRAY2BGR))
    writer.release()
    out = tmp_path / "camera.json"

    rc = _load_cli().main(
        [
            "--video",
            str(video),
            "--every",
            "1",
            "--captured-on",
            CAPTURED_ON,
            "--out",
            str(out),
        ]
    )

    assert rc == 0
    restored = from_params(json.loads(out.read_text()))
    assert restored.n_views == N_VIEWS
    assert restored.reprojection_error_px <= 1.0


def test_cli_exits_2_on_insufficient_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image_dir = tmp_path / "frames"
    image_dir.mkdir()
    for i in range(8):
        assert cv2.imwrite(str(image_dir / f"blank_{i}.png"), _blank())
    (image_dir / "corrupt.png").write_bytes(b"this is not a png")
    out = tmp_path / "camera.json"

    rc = _load_cli().main(
        ["--images", str(image_dir), "--captured-on", CAPTURED_ON, "--out", str(out)]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "warning: could not read" in err
    assert "error:" in err
    assert not out.exists()


def test_cli_rejects_nonpositive_every(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _load_cli().main(
            [
                "--video",
                str(tmp_path / "sweep.mp4"),
                "--every",
                "0",
                "--captured-on",
                CAPTURED_ON,
                "--out",
                str(tmp_path / "out.json"),
            ]
        )
    assert excinfo.value.code == 2
