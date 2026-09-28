"""US-E5 overlay tests: pixel-precise skeleton placement (<= 3 px AC), visibility
skipping, undistortion-aware drawing, anchor-sync frame math, ghost blending, and
the age-appropriate no-text safety guarantee."""

import inspect
import math
from typing import Any, get_type_hints

import numpy as np
import numpy.typing as npt
import pytest
from cricai_data.enums import AnchorPoint
from cricai_vision import overlay
from cricai_vision.intrinsics import undistort_pixel
from cricai_vision.overlay import (
    DEFAULT_CONFIG,
    SKELETON_BONES,
    ClipAnchors,
    OverlayConfig,
    SyncPlan,
    anchor_sync,
    draw_skeleton,
    ghost_overlay,
)
from cricai_vision.pose import LANDMARK_NAMES, FakePoseProvider, Landmark, PoseFrame

#: Pinhole used by the seeded intrinsics params (mirrors the intrinsics
#: ``to_params`` payload shape used across the API tests).
FOCAL_PX = 1000.0
CENTER_X_PX = 960.0
CENTER_Y_PX = 540.0
K1 = -0.2


def _black_frame() -> npt.NDArray[np.uint8]:
    return np.zeros((1080, 1920, 3), dtype=np.uint8)


def _stance_frame() -> PoseFrame:
    """FakePoseProvider stance with zero sway: landmark pixels equal the anchors."""
    return FakePoseProvider(sway_px=0.0).extract([None], fps=50.0).frames[0]


def _replace_landmark(
    frame: PoseFrame,
    name: str,
    *,
    xy: tuple[float, float] | None = None,
    visibility: float | None = None,
) -> PoseFrame:
    index = LANDMARK_NAMES[name]
    landmarks = list(frame.landmarks)
    old = landmarks[index]
    landmarks[index] = Landmark(
        image_xy=xy if xy is not None else old.image_xy,
        world_xyz=old.world_xyz,
        visibility=visibility if visibility is not None else old.visibility,
    )
    return PoseFrame(frame_no=frame.frame_no, landmarks=tuple(landmarks))


def _shift_pose(frame: PoseFrame, dx: float, dy: float) -> PoseFrame:
    landmarks = tuple(
        Landmark(
            image_xy=(lm.image_xy[0] + dx, lm.image_xy[1] + dy),
            world_xyz=lm.world_xyz,
            visibility=lm.visibility,
        )
        for lm in frame.landmarks
    )
    return PoseFrame(frame_no=frame.frame_no, landmarks=landmarks)


def _color_mask(image: npt.NDArray[np.uint8], color: tuple[int, int, int]) -> npt.NDArray[np.bool_]:
    mask: npt.NDArray[np.bool_] = np.all(image == np.asarray(color, dtype=np.uint8), axis=-1)
    return mask


def _centroid(
    image: npt.NDArray[np.uint8],
    color: tuple[int, int, int],
    near: tuple[float, float],
    *,
    window: int = 6,
) -> tuple[float, float]:
    """Centroid of ``color`` pixels within ``window`` px of ``near`` (must exist)."""
    cx, cy = round(near[0]), round(near[1])
    region = image[cy - window : cy + window + 1, cx - window : cx + window + 1]
    ys, xs = np.nonzero(_color_mask(region, color))
    assert xs.size > 0, f"no pixels of color {color} within {window} px of {near}"
    return (cx - window + float(xs.mean()), cy - window + float(ys.mean()))


def _distorted(px: tuple[float, float], k1: float) -> tuple[float, float]:
    """Forward OpenCV radial model: the distorted pixel a lens shows for ``px``."""
    x = (px[0] - CENTER_X_PX) / FOCAL_PX
    y = (px[1] - CENTER_Y_PX) / FOCAL_PX
    factor = 1.0 + k1 * (x * x + y * y)
    return (FOCAL_PX * x * factor + CENTER_X_PX, FOCAL_PX * y * factor + CENTER_Y_PX)


def _intrinsic_params(k1: float) -> dict[str, Any]:
    """Seeded intrinsics dict in the ``intrinsics.to_params`` payload shape."""
    return {
        "version": 1,
        "camera_matrix": [
            [FOCAL_PX, 0.0, CENTER_X_PX],
            [0.0, FOCAL_PX, CENTER_Y_PX],
            [0.0, 0.0, 1.0],
        ],
        "dist_coeffs": [k1, 0.0, 0.0, 0.0, 0.0],
        "reprojection_error_px": 0.3,
        "board_spec": {
            "squares_x": 7,
            "squares_y": 10,
            "square_len_m": 0.08,
            "marker_len_m": 0.06,
            "aruco_dict": "DICT_5X5_100",
        },
        "n_views": 10,
        "captured_on": "2026-07-01",
    }


def test_bone_list_covers_only_known_landmarks() -> None:
    for name_a, name_b in SKELETON_BONES:
        assert name_a in LANDMARK_NAMES
        assert name_b in LANDMARK_NAMES
        assert name_a != name_b


def test_joint_centers_within_3px_of_landmarks() -> None:
    """US-E5 alignment AC: drawn joint centers sit on the landmark image_xy."""
    pose = _stance_frame()
    out = draw_skeleton(_black_frame(), pose)
    for name in LANDMARK_NAMES:
        expected = pose.landmark(name).image_xy
        got = _centroid(out, DEFAULT_CONFIG.joint_color, expected)
        assert math.dist(got, expected) <= 3.0, f"{name} drawn {got}, landmark {expected}"


def test_draw_skeleton_is_pure_with_respect_to_input() -> None:
    frame = _black_frame()
    before = frame.copy()
    out = draw_skeleton(frame, _stance_frame())
    assert out is not frame
    assert np.array_equal(frame, before)
    assert np.any(out != 0)


def test_bones_drawn_between_visible_joints() -> None:
    pose = _stance_frame()
    out = draw_skeleton(_black_frame(), pose)
    knee = pose.landmark("left_knee").image_xy
    ankle = pose.landmark("left_ankle").image_xy
    midpoint = ((knee[0] + ankle[0]) / 2, (knee[1] + ankle[1]) / 2)
    _centroid(out, DEFAULT_CONFIG.bone_color, midpoint, window=2)  # asserts pixels exist


def test_low_visibility_landmark_skipped_never_fabricated() -> None:
    pose = _replace_landmark(_stance_frame(), "left_wrist", visibility=0.2)
    out = draw_skeleton(_black_frame(), pose)
    wrist = pose.landmark("left_wrist").image_xy
    x, y = round(wrist[0]), round(wrist[1])
    window = out[y - 10 : y + 11, x - 10 : x + 11]
    assert not np.any(window), "skipped joint (and its bone) must leave no pixels"
    # The rest of the skeleton still renders.
    _centroid(out, DEFAULT_CONFIG.joint_color, pose.landmark("left_elbow").image_xy)


def test_invisible_nose_removes_head_line() -> None:
    pose = _replace_landmark(_stance_frame(), "nose", visibility=0.0)
    out = draw_skeleton(_black_frame(), pose)
    assert not np.any(_color_mask(out, DEFAULT_CONFIG.head_line_color))


def test_head_line_is_horizontal_through_nose() -> None:
    pose = _stance_frame()
    out = draw_skeleton(_black_frame(), pose)
    nose_y = round(pose.landmark("nose").image_xy[1])
    head = np.asarray(DEFAULT_CONFIG.head_line_color, dtype=np.uint8)
    assert np.array_equal(out[nose_y, 5], head)
    assert np.array_equal(out[nose_y, 1900], head)
    assert not np.any(out[nose_y - 5, 5])  # a horizontal line, not a band


def test_contact_marker_optional() -> None:
    pose = _stance_frame()
    config = OverlayConfig(contact_point_px=(700.0, 800.0))
    out = draw_skeleton(_black_frame(), pose, config=config)
    got = _centroid(out, config.contact_marker_color, (700.0, 800.0), window=8)
    assert math.dist(got, (700.0, 800.0)) <= 3.0

    plain = draw_skeleton(_black_frame(), pose)
    assert not np.any(_color_mask(plain, DEFAULT_CONFIG.contact_marker_color))


def test_undistortion_applied_before_drawing() -> None:
    """US-E5 testing note: overlay coordinates transform after undistortion."""
    params = _intrinsic_params(K1)
    ideal = (1500.0, 900.0)  # far from center: distortion shifts it ~45 px
    observed = _distorted(ideal, K1)
    pose = _replace_landmark(_stance_frame(), "right_wrist", xy=observed)
    config = OverlayConfig(intrinsics_params=params)
    out = draw_skeleton(_black_frame(), pose, config=config)

    expected = undistort_pixel(params, observed)
    got = _centroid(out, config.joint_color, expected)
    assert math.dist(got, expected) <= 3.0
    assert math.dist(got, observed) > 10.0, "raw distorted pixel must not be used"


def test_malformed_intrinsics_params_raise() -> None:
    config = OverlayConfig(intrinsics_params={"version": 99})
    with pytest.raises(ValueError, match="version"):
        draw_skeleton(_black_frame(), _stance_frame(), config=config)


@pytest.mark.parametrize(
    "bad_frame",
    [np.zeros((32, 32), dtype=np.uint8), np.zeros((32, 32, 4), dtype=np.uint8)],
)
def test_non_bgr_frame_rejected(bad_frame: npt.NDArray[np.uint8]) -> None:
    with pytest.raises(ValueError, match="HxWx3"):
        draw_skeleton(bad_frame, _stance_frame())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_visibility": -0.1},
        {"min_visibility": 1.5},
        {"ghost_alpha": 0.0},
        {"ghost_alpha": 1.0},
        {"bone_thickness": 0},
        {"joint_radius": 0},
        {"head_line_thickness": 0},
        {"contact_marker_radius": -1},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OverlayConfig(**kwargs)


def test_anchor_sync_release_frames_are_clip_local() -> None:
    """Anchors rebase onto each trimmed clip's own timeline (US-E5).

    Ball A: session release 61,000 ms in a clip trimmed at 58,000 ms; ball B
    (another session's reference ball): release 125,000 ms in a clip trimmed at
    123,500 ms. At 50 fps the clip-local anchors are (61000-58000)/1000*50 = 150
    and (125000-123500)/1000*50 = 75 - nowhere near the session-absolute frame
    numbers 3050/6250.
    """
    a = ClipAnchors(clip_start_ms=58000.0, release_ms=61000.0)
    b = ClipAnchors(clip_start_ms=123500.0, release_ms=125000.0)
    plan = anchor_sync(a, b, anchor=AnchorPoint.RELEASE, fps=50.0)
    assert plan == SyncPlan(offset_frames_b_vs_a=-75, anchor_frame_a=150, anchor_frame_b=75)


def test_anchor_sync_equal_clip_gaps_give_zero_offset() -> None:
    """Two balls released 2 s into their clips frame-lock with no offset, no
    matter how far apart their session timestamps are (60 = 2000/1000*30)."""
    a = ClipAnchors(clip_start_ms=8500.0, release_ms=10500.0)
    b = ClipAnchors(clip_start_ms=198500.0, release_ms=200500.0)
    plan = anchor_sync(a, b, anchor=AnchorPoint.RELEASE, fps=30.0)
    assert plan == SyncPlan(offset_frames_b_vs_a=0, anchor_frame_a=60, anchor_frame_b=60)


def test_anchor_sync_contact_frame_math() -> None:
    # a: (10400 - 8500) / 1000 * 100 = 190; b: (200500 - 198500) / 1000 * 100 = 200.
    a = ClipAnchors(clip_start_ms=8500.0, release_ms=10000.0, contact_ms=10400.0)
    b = ClipAnchors(clip_start_ms=198500.0, release_ms=200000.0, contact_ms=200500.0)
    plan = anchor_sync(a, b, anchor=AnchorPoint.CONTACT, fps=100.0)
    assert plan == SyncPlan(offset_frames_b_vs_a=10, anchor_frame_a=190, anchor_frame_b=200)


def test_anchor_sync_rounds_clip_relative_ms_to_nearest_frame() -> None:
    # a: (3005 - 2000) ms -> 30.15 frames rounds to 30; b: release at frame 0.
    a = ClipAnchors(clip_start_ms=2000.0, release_ms=3005.0)
    b = ClipAnchors(clip_start_ms=500.0, release_ms=500.0)
    plan = anchor_sync(a, b, anchor=AnchorPoint.RELEASE, fps=30.0)
    assert plan.anchor_frame_a == 30
    assert plan.anchor_frame_b == 0
    assert plan.offset_frames_b_vs_a == -30


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (ClipAnchors(0.0, 1000.0, None), ClipAnchors(0.0, 2000.0, 2400.0)),
        (ClipAnchors(0.0, 1000.0, 1400.0), ClipAnchors(0.0, 2000.0, None)),
    ],
)
def test_anchor_sync_contact_requires_both_contacts(a: ClipAnchors, b: ClipAnchors) -> None:
    with pytest.raises(ValueError, match="contact_ms"):
        anchor_sync(a, b, anchor=AnchorPoint.CONTACT, fps=50.0)


def test_anchor_sync_rejects_non_positive_fps() -> None:
    with pytest.raises(ValueError, match="fps"):
        anchor_sync(
            ClipAnchors(0.0, 1000.0), ClipAnchors(0.0, 2000.0), anchor=AnchorPoint.RELEASE, fps=0.0
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"clip_start_ms": -1.0, "release_ms": 1000.0}, "clip_start_ms"),
        ({"clip_start_ms": 5000.0, "release_ms": 4000.0}, "precedes clip start"),
        (
            {"clip_start_ms": 0.0, "release_ms": 2000.0, "contact_ms": 1500.0},
            "precedes release_ms",
        ),
    ],
)
def test_clip_anchors_rejects_anchors_outside_clip_or_out_of_order(
    kwargs: dict[str, float], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        ClipAnchors(**kwargs)


def test_ghost_overlay_blends_reference_at_reduced_alpha() -> None:
    frame = _black_frame()
    current = _stance_frame()
    reference = _shift_pose(current, 200.0, 0.0)
    out = ghost_overlay(frame, current, reference)
    base = draw_skeleton(frame, current)
    assert not np.array_equal(out, base), "ghost must change pixels"

    # Ghost joint (reference right_wrist at 1230, 610) over black blends to
    # alpha * joint_color.
    ghost_px = out[610, 1230].astype(int)
    expected = [round(DEFAULT_CONFIG.ghost_alpha * c) for c in DEFAULT_CONFIG.joint_color]
    assert all(abs(int(g) - e) <= 1 for g, e in zip(ghost_px, expected, strict=True))

    # Pixels the ghost does not touch keep the full-strength current overlay.
    current_px = out[610, 890].astype(int)
    assert all(
        abs(int(g) - c) <= 1 for g, c in zip(current_px, DEFAULT_CONFIG.joint_color, strict=True)
    )
    assert np.array_equal(frame, _black_frame()), "input frame must not be mutated"


def test_ghost_overlay_keeps_contact_marker_current_only() -> None:
    config = OverlayConfig(contact_point_px=(700.0, 800.0))
    out = ghost_overlay(
        _black_frame(), _stance_frame(), _shift_pose(_stance_frame(), 200.0, 0.0), config=config
    )
    marker_px = out[800, 700].astype(int)
    # Full strength: the marker belongs to the current ball, never the ghost.
    assert all(
        abs(int(g) - c) <= 1 for g, c in zip(marker_px, config.contact_marker_color, strict=True)
    )


@pytest.mark.safety
def test_overlay_surface_is_technique_geometry_only() -> None:
    """SAFETY (US-E5 age-appropriate AC) - never weaken.

    Overlays must never carry body-weight/appearance commentary: the config
    exposes no free-text hook, the module renders no text glyphs, and a full
    render contains nothing but the configured technique-geometry colors.
    """
    hints = get_type_hints(OverlayConfig)
    assert str not in hints.values(), "OverlayConfig must expose no free-text field"

    source = inspect.getsource(overlay)
    assert "putText" not in source, "overlay module must never render text"
    assert "FONT_" not in source, "overlay module must never reference font glyphs"
    assert "technique" in (overlay.__doc__ or "").lower()

    config = OverlayConfig(contact_point_px=(700.0, 800.0))
    out = draw_skeleton(_black_frame(), _stance_frame(), config=config)
    rendered = {(int(b), int(g), int(r)) for b, g, r in np.unique(out.reshape(-1, 3), axis=0)}
    allowed = {
        (0, 0, 0),
        config.bone_color,
        config.joint_color,
        config.head_line_color,
        config.contact_marker_color,
    }
    assert rendered <= allowed, f"unexpected non-technique pixels: {rendered - allowed}"
