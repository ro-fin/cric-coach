"""Pitch geometry & the system-wide coordinate frame contract (US-C2).

Frame: origin = middle-stump base at the **striker's** end; +x toward the bowler
(down the pitch); +y toward the off side for a right-hand batter; +z up; meters.
Left-hand batters are handled at presentation/classification time (US-C5),
never by changing this frame.

Pitch dimensions per MCC law: 20.12 m stumps-to-stumps (22 yards), 3.05 m wide
(10 ft); popping crease 1.22 m (4 ft) in front of each stumps line; return
creases 1.32 m either side of middle stump.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

PITCH_LENGTH_M = 20.12  # striker's stumps → bowler's stumps
PITCH_WIDTH_M = 3.05
POPPING_CREASE_OFFSET_M = 1.22  # in front of the stumps line
RETURN_CREASE_HALF_SPAN_M = 1.32  # either side of middle stump
STUMPS_HALF_WIDTH_M = 0.1143  # 9 in overall stump spread / 2
BALL_DIAMETER_M = 0.073


@dataclass(frozen=True)
class PitchPoint:
    """A named, surveyable landmark on the pitch plane (z = 0)."""

    name: str
    x: float
    y: float

    @property
    def xy(self) -> tuple[float, float]:
        return (self.x, self.y)


def landmark_catalog() -> dict[str, PitchPoint]:
    """Named landmarks an operator can click / tape-measure (US-C2, US-C4)."""
    points = [
        PitchPoint("striker_middle_stump_base", 0.0, 0.0),
        PitchPoint("striker_off_stump_base", 0.0, STUMPS_HALF_WIDTH_M),
        PitchPoint("striker_leg_stump_base", 0.0, -STUMPS_HALF_WIDTH_M),
        PitchPoint("bowler_middle_stump_base", PITCH_LENGTH_M, 0.0),
        # Bowler-end stumps keep the striker's sides: off = +y, leg = -y.
        PitchPoint("bowler_off_stump_base", PITCH_LENGTH_M, STUMPS_HALF_WIDTH_M),
        PitchPoint("bowler_leg_stump_base", PITCH_LENGTH_M, -STUMPS_HALF_WIDTH_M),
        PitchPoint("striker_popping_crease_center", POPPING_CREASE_OFFSET_M, 0.0),
        PitchPoint(
            "striker_popping_crease_off",
            POPPING_CREASE_OFFSET_M,
            RETURN_CREASE_HALF_SPAN_M,
        ),
        PitchPoint(
            "striker_popping_crease_leg",
            POPPING_CREASE_OFFSET_M,
            -RETURN_CREASE_HALF_SPAN_M,
        ),
        PitchPoint(
            "bowler_popping_crease_center",
            PITCH_LENGTH_M - POPPING_CREASE_OFFSET_M,
            0.0,
        ),
        PitchPoint(
            "bowler_popping_crease_off",
            PITCH_LENGTH_M - POPPING_CREASE_OFFSET_M,
            RETURN_CREASE_HALF_SPAN_M,
        ),
        PitchPoint(
            "bowler_popping_crease_leg",
            PITCH_LENGTH_M - POPPING_CREASE_OFFSET_M,
            -RETURN_CREASE_HALF_SPAN_M,
        ),
        PitchPoint("pitch_edge_off_striker", 0.0, PITCH_WIDTH_M / 2),
        PitchPoint("pitch_edge_leg_striker", 0.0, -PITCH_WIDTH_M / 2),
        PitchPoint("pitch_edge_off_bowler", PITCH_LENGTH_M, PITCH_WIDTH_M / 2),
        PitchPoint("pitch_edge_leg_bowler", PITCH_LENGTH_M, -PITCH_WIDTH_M / 2),
    ]
    return {p.name: p for p in points}


@dataclass(frozen=True)
class PinholeCamera:
    """Synthetic pinhole camera for tests: projects pitch-frame 3D points to pixels.

    K: 3x3 intrinsics; R: 3x3 world→camera rotation; t: 3-vector translation
    (camera coords). No distortion — distortion is exercised separately.
    """

    camera_matrix: np.ndarray
    rotation: np.ndarray
    translation: np.ndarray

    def project(self, points_xyz: np.ndarray) -> np.ndarray:
        """Project Nx3 world points → Nx2 pixel coordinates."""
        pts = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
        cam = (self.rotation @ pts.T + self.translation.reshape(3, 1)).T
        if np.any(cam[:, 2] <= 1e-9):
            raise ValueError("point behind or on the camera plane")
        uvw = (self.camera_matrix @ cam.T).T
        return np.asarray(uvw[:, :2] / uvw[:, 2:3], dtype=np.float64)

    def project_pitch_xy(self, points_xy: np.ndarray) -> np.ndarray:
        """Project Nx2 pitch-plane points (z=0) → Nx2 pixels."""
        pts = np.asarray(points_xy, dtype=np.float64).reshape(-1, 2)
        xyz = np.column_stack([pts, np.zeros(len(pts))])
        return self.project(xyz)


def make_overhead_camera(
    *,
    height_m: float = 4.5,
    focal_px: float = 1400.0,
    cx: float = 960.0,
    cy: float = 540.0,
    look_at_x: float = PITCH_LENGTH_M / 2,
) -> PinholeCamera:
    """A plausible C4-style overhead/high-diagonal camera for synthetic scenes.

    Positioned above the pitch centerline offset toward the off side, pitched
    down to look at (look_at_x, 0, 0).
    """
    camera_pos = np.array([look_at_x, PITCH_WIDTH_M * 2.5, height_m])
    target = np.array([look_at_x, 0.0, 0.0])

    forward = target - camera_pos
    forward = forward / np.linalg.norm(forward)
    world_up = np.array([0.0, 0.0, 1.0])
    right = np.cross(forward, world_up)
    right = right / np.linalg.norm(right)
    down = np.cross(forward, right)

    rotation = np.vstack([right, down, forward])
    translation = -rotation @ camera_pos
    camera_matrix = np.array([[focal_px, 0, cx], [0, focal_px, cy], [0, 0, 1.0]])
    return PinholeCamera(camera_matrix, rotation, translation)
