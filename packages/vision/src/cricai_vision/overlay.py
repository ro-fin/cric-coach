"""Skeleton overlay rendering and comparison anchor-sync math (US-E5).

Render layer for annotated clips: skeleton bones and joints from a
:class:`cricai_vision.pose.PoseFrame`, a horizontal head-line through the
nose, an optional contact-marker point, and an optional ghost overlay of a
reference ball's skeleton at reduced alpha. :func:`anchor_sync` provides the
pure frame-lock math the Phase 6 comparison UI steps through (anchored at
release or contact per the US-E5 AC).

Coordinates: landmark ``image_xy`` pixels are drawn as-is unless
``OverlayConfig.intrinsics_params`` carries a ``kind=intrinsic``
:func:`cricai_vision.intrinsics.to_params` payload, in which case every drawn
point is first mapped through
:func:`cricai_vision.intrinsics.undistort_pixel` so overlays land on the
lens-corrected image (US-E5 testing note: coordinate transforms after
undistortion). Landmarks below ``min_visibility`` are skipped entirely - a
joint the model did not see is never fabricated.

AGE-APPROPRIATE PRESENTATION (US-E5 AC, safety-tested, never weaken):
overlays draw TECHNIQUE geometry only - bones, joints, head-line, contact
marker. No body-weight or appearance annotations exist anywhere in this
module, no text is ever rendered, and :class:`OverlayConfig` deliberately
exposes no free-text hook beyond these technique markers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt
from cricai_data.enums import AnchorPoint

from cricai_vision.intrinsics import undistort_pixel
from cricai_vision.pose import LANDMARK_NAMES, PoseFrame

#: Skeleton bones as pairs of :data:`cricai_vision.pose.LANDMARK_NAMES` keys.
#: A bone is drawn only when BOTH of its joints clear ``min_visibility``.
SKELETON_BONES: tuple[tuple[str, str], ...] = (
    ("left_shoulder", "right_shoulder"),
    ("left_shoulder", "left_elbow"),
    ("left_elbow", "left_wrist"),
    ("right_shoulder", "right_elbow"),
    ("right_elbow", "right_wrist"),
    ("left_shoulder", "left_hip"),
    ("right_shoulder", "right_hip"),
    ("left_hip", "right_hip"),
    ("left_hip", "left_knee"),
    ("left_knee", "left_ankle"),
    ("left_ankle", "left_heel"),
    ("left_heel", "left_foot_index"),
    ("right_hip", "right_knee"),
    ("right_knee", "right_ankle"),
    ("right_ankle", "right_heel"),
    ("right_heel", "right_foot_index"),
)

_FILLED = -1  # cv2 thickness sentinel for filled circles


@dataclass(frozen=True)
class OverlayConfig:
    """Technique-geometry drawing options (colors are BGR).

    There is intentionally NO text/annotation field here (US-E5
    age-appropriate AC): the config can only control the technique markers
    below, so no caller can attach commentary to a rendered frame.
    """

    bone_color: tuple[int, int, int] = (0, 200, 0)
    joint_color: tuple[int, int, int] = (0, 0, 255)
    head_line_color: tuple[int, int, int] = (255, 200, 0)
    contact_marker_color: tuple[int, int, int] = (0, 165, 255)
    bone_thickness: int = 2
    joint_radius: int = 4
    head_line_thickness: int = 1
    contact_marker_radius: int = 6
    min_visibility: float = 0.5
    ghost_alpha: float = 0.35
    contact_point_px: tuple[float, float] | None = None
    intrinsics_params: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_visibility <= 1.0:
            raise ValueError(f"min_visibility must be in [0, 1], got {self.min_visibility}")
        if not 0.0 < self.ghost_alpha < 1.0:
            raise ValueError(f"ghost_alpha must be in (0, 1), got {self.ghost_alpha}")
        for name in (
            "bone_thickness",
            "joint_radius",
            "head_line_thickness",
            "contact_marker_radius",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1, got {getattr(self, name)}")


#: Shared default config (module-level so it is built exactly once).
DEFAULT_CONFIG = OverlayConfig()


@dataclass(frozen=True)
class ClipAnchors:
    """One side of a US-E5 comparison: a trimmed per-ball clip plus its anchors.

    All three values are video-timeline milliseconds on that side's own source
    recording (``Clip`` / ``BallEvent`` semantics — the two sides may come from
    different sessions). ``clip_start_ms`` is where the trimmed clip artifact
    begins on that timeline (``Clip.start_ms``: event start minus pre-roll,
    clamped at the video start), so clip frame 0 shows instant
    ``clip_start_ms``, not 0. ``release_ms``/``contact_ms`` are the ball's
    ``BallEvent`` anchor instants; ``contact_ms`` is ``None`` for a left ball.

    Construction validates that the anchors lie inside the clip
    (``clip_start_ms <= release_ms <= contact_ms`` when contact is present) so
    the frame math in :func:`anchor_sync` can never target an instant the
    trimmed artifact does not contain.
    """

    clip_start_ms: float
    release_ms: float
    contact_ms: float | None = None

    def __post_init__(self) -> None:
        if self.clip_start_ms < 0:
            raise ValueError(f"clip_start_ms must be >= 0, got {self.clip_start_ms}")
        if self.release_ms < self.clip_start_ms:
            raise ValueError(
                f"release_ms ({self.release_ms}) precedes clip start ({self.clip_start_ms}); "
                "anchors must lie inside the trimmed clip"
            )
        if self.contact_ms is not None and self.contact_ms < self.release_ms:
            raise ValueError(
                f"contact_ms ({self.contact_ms}) precedes release_ms ({self.release_ms}); "
                "event ordering requires release_ms <= contact_ms"
            )


@dataclass(frozen=True)
class SyncPlan:
    """Frame-lock plan aligning clip B to clip A at a shared anchor instant.

    ``anchor_frame_a``/``anchor_frame_b`` are frame indices on each trimmed
    clip's OWN timeline: frame 0 is the first frame of the clip artifact
    (instant ``ClipAnchors.clip_start_ms`` of its source recording). The
    Phase 6 comparison view shows frame ``f`` of clip A next to frame
    ``f + offset_frames_b_vs_a`` of clip B; at ``f = anchor_frame_a`` both
    sides display their anchor instant, and stepping either side keeps the
    pair frame-locked (US-E5).
    """

    offset_frames_b_vs_a: int
    anchor_frame_a: int
    anchor_frame_b: int


def _copy_frame(frame: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Validate the BGR frame and return a copy - inputs are never mutated."""
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError(f"frame must be an HxWx3 BGR image, got shape {frame.shape}")
    return frame.copy()


def _maybe_undistort(config: OverlayConfig, xy: tuple[float, float]) -> tuple[float, float]:
    if config.intrinsics_params is None:
        return xy
    return undistort_pixel(config.intrinsics_params, xy)


def _to_point(xy: tuple[float, float]) -> tuple[int, int]:
    return (round(xy[0]), round(xy[1]))


def _drawable_points(pose: PoseFrame, config: OverlayConfig) -> dict[str, tuple[int, int]]:
    """Named landmarks that clear ``min_visibility``, as drawable pixel points.

    Low-visibility landmarks are dropped, not guessed: an overlay must only
    show what the pose model actually saw (US-E5 alignment AC).
    """
    points: dict[str, tuple[int, int]] = {}
    for name in LANDMARK_NAMES:
        landmark = pose.landmark(name)
        if landmark.visibility < config.min_visibility:
            continue
        points[name] = _to_point(_maybe_undistort(config, landmark.image_xy))
    return points


def draw_skeleton(
    frame: npt.NDArray[np.uint8],
    pose: PoseFrame,
    *,
    config: OverlayConfig = DEFAULT_CONFIG,
) -> npt.NDArray[np.uint8]:
    """Render the technique skeleton for one pose frame onto a copy of ``frame``.

    Draw order (bottom to top): head-line, bones, joints, contact marker, so a
    joint's filled circle is centered exactly on its (optionally undistorted)
    ``image_xy`` - the US-E5 <= 3 px alignment AC is asserted pixel-precisely
    in tests. All primitives use ``LINE_8`` (no anti-aliasing) so rendered
    pixels carry exactly the configured technique colors.
    """
    out = _copy_frame(frame)
    points = _drawable_points(pose, config)

    nose = points.get("nose")
    if nose is not None:  # head-line: horizontal reference through the nose
        cv2.line(
            out,
            (0, nose[1]),
            (out.shape[1] - 1, nose[1]),
            config.head_line_color,
            config.head_line_thickness,
            lineType=cv2.LINE_8,
        )
    for name_a, name_b in SKELETON_BONES:
        point_a = points.get(name_a)
        point_b = points.get(name_b)
        if point_a is None or point_b is None:
            continue  # never fabricate a bone toward an unseen joint
        cv2.line(
            out, point_a, point_b, config.bone_color, config.bone_thickness, lineType=cv2.LINE_8
        )
    for point in points.values():
        cv2.circle(
            out, point, config.joint_radius, config.joint_color, _FILLED, lineType=cv2.LINE_8
        )
    if config.contact_point_px is not None:
        contact = _to_point(_maybe_undistort(config, config.contact_point_px))
        cv2.circle(
            out,
            contact,
            config.contact_marker_radius,
            config.contact_marker_color,
            _FILLED,
            lineType=cv2.LINE_8,
        )
    return out


def ghost_overlay(
    frame: npt.NDArray[np.uint8],
    pose_current: PoseFrame,
    pose_reference: PoseFrame,
    *,
    config: OverlayConfig = DEFAULT_CONFIG,
) -> npt.NDArray[np.uint8]:
    """Current skeleton at full strength plus the reference ball's as a ghost.

    The reference skeleton (US-E5 'optional ghost overlay' of a model-example
    ball) blends in at ``config.ghost_alpha``; pixels the ghost does not touch
    keep the fully-rendered current overlay. The contact marker belongs to the
    current ball only, so it is suppressed on the ghost pass.
    """
    base = draw_skeleton(frame, pose_current, config=config)
    ghost_config = replace(config, contact_point_px=None)
    with_reference = draw_skeleton(base, pose_reference, config=ghost_config)
    blended = cv2.addWeighted(
        with_reference, config.ghost_alpha, base, 1.0 - config.ghost_alpha, 0.0
    )
    return np.asarray(blended, dtype=np.uint8)


def _clip_local_frame(clip_relative_ms: float, fps: float) -> int:
    """Milliseconds past the clip start -> nearest clip-local frame index."""
    return round(clip_relative_ms * fps / 1000.0)


def anchor_sync(a: ClipAnchors, b: ClipAnchors, *, anchor: AnchorPoint, fps: float) -> SyncPlan:
    """Pure frame-lock math for comparing two trimmed per-ball clips (US-E5).

    ``a`` and ``b`` each describe one side's clip and anchor instants (see
    :class:`ClipAnchors`); ``fps`` is the shared clip frame rate. Per-ball
    clips are ffmpeg trims that begin at ``Clip.start_ms`` — an offset that
    differs per ball (pre-roll clamped at the video start) and per session —
    so ``BallEvent`` milliseconds are rebased onto each clip's own timeline
    before frame conversion::

        anchor_frame = round((anchor_ms - clip_start_ms) * fps / 1000)

    The returned :class:`SyncPlan` is therefore directly consumable by the
    Phase 6 comparison UI: seek clip A to ``anchor_frame_a``, clip B to
    ``anchor_frame_b``, then step both sides keeping B ahead of A by
    ``offset_frames_b_vs_a`` frames.

    Raises ``ValueError`` when ``fps`` is not positive, or when ``anchor`` is
    contact and either ball has no contact instant - a left ball cannot be
    contact-anchored, and guessing one would silently desynchronize the
    comparison.
    """
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    if anchor is AnchorPoint.CONTACT:
        a_ms, b_ms = a.contact_ms, b.contact_ms
        if a_ms is None or b_ms is None:
            raise ValueError(
                "anchor=contact requires contact_ms on both events "
                f"(a={a.contact_ms}, b={b.contact_ms}); anchor at release instead"
            )
    else:
        a_ms, b_ms = a.release_ms, b.release_ms
    anchor_frame_a = _clip_local_frame(a_ms - a.clip_start_ms, fps)
    anchor_frame_b = _clip_local_frame(b_ms - b.clip_start_ms, fps)
    return SyncPlan(
        offset_frames_b_vs_a=anchor_frame_b - anchor_frame_a,
        anchor_frame_a=anchor_frame_a,
        anchor_frame_b=anchor_frame_b,
    )
