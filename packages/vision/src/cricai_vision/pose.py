"""Pose extraction foundation (US-E1): dataclasses, provider protocol, deterministic fake.

All metric code (US-E2/E3) consumes :class:`PoseTrack` and never a provider directly,
so tests run on :class:`FakePoseProvider`'s seeded synthetic skeletons and the real
MediaPipe adapter (``cricai_vision.mediapipe_pose``) stays a thin, swappable edge.

Landmark indices follow the MediaPipe Pose 33-point topology; the subset named in
:data:`LANDMARK_NAMES` is what metric formulas reference.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

#: Number of landmarks per frame (MediaPipe Pose topology).
N_LANDMARKS = 33

#: Named indices used by metric formulas (US-E2/E3). Full topology documented upstream.
LANDMARK_NAMES: dict[str, int] = {
    "nose": 0,
    "left_shoulder": 11,
    "right_shoulder": 12,
    "left_elbow": 13,
    "right_elbow": 14,
    "left_wrist": 15,
    "right_wrist": 16,
    "left_hip": 23,
    "right_hip": 24,
    "left_knee": 25,
    "right_knee": 26,
    "left_ankle": 27,
    "right_ankle": 28,
    "left_heel": 29,
    "right_heel": 30,
    "left_foot_index": 31,
    "right_foot_index": 32,
}


class PoseError(ValueError):
    """Raised for malformed pose payloads or invalid provider inputs."""


@dataclass(frozen=True)
class Landmark:
    """One landmark observation: image px, world meters, and visibility in [0, 1]."""

    image_xy: tuple[float, float]
    world_xyz: tuple[float, float, float]
    visibility: float


@dataclass(frozen=True)
class PoseFrame:
    """All 33 landmarks for one video frame."""

    frame_no: int
    landmarks: tuple[Landmark, ...]

    def __post_init__(self) -> None:
        if len(self.landmarks) != N_LANDMARKS:
            raise PoseError(f"pose frame needs {N_LANDMARKS} landmarks, got {len(self.landmarks)}")

    def landmark(self, name: str) -> Landmark:
        """Look up a landmark by its canonical name."""
        try:
            return self.landmarks[LANDMARK_NAMES[name]]
        except KeyError as exc:
            raise PoseError(f"unknown landmark name: {name!r}") from exc


@dataclass(frozen=True)
class PoseTrack:
    """Per-ball pose track: the common foundation for every technique metric (US-E1)."""

    model_name: str
    model_version: str
    fps: float
    frames: tuple[PoseFrame, ...]
    subject_confidence: float

    def __post_init__(self) -> None:
        if self.fps <= 0:
            raise PoseError(f"fps must be positive, got {self.fps}")
        if not 0.0 <= self.subject_confidence <= 1.0:
            raise PoseError(f"subject_confidence must be in [0, 1], got {self.subject_confidence}")

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def availability(self, *, min_visibility: float = 0.5) -> float:
        """Fraction of frames whose median-landmark visibility clears the bar (US-E1 AC)."""
        if not self.frames:
            return 0.0
        visible = sum(
            1
            for frame in self.frames
            if sorted(lm.visibility for lm in frame.landmarks)[N_LANDMARKS // 2] >= min_visibility
        )
        return visible / len(self.frames)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe payload stored in the object store (schema versioned)."""
        return {
            "version": 1,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "fps": self.fps,
            "subject_confidence": self.subject_confidence,
            "frames": [
                {
                    "frame_no": frame.frame_no,
                    "landmarks": [
                        {
                            "image_xy": list(lm.image_xy),
                            "world_xyz": list(lm.world_xyz),
                            "visibility": lm.visibility,
                        }
                        for lm in frame.landmarks
                    ],
                }
                for frame in self.frames
            ],
        }


def track_from_payload(payload: dict[str, Any]) -> PoseTrack:
    """Inverse of :meth:`PoseTrack.to_payload`; raises :class:`PoseError` when malformed."""
    if payload.get("version") != 1:
        raise PoseError(f"unsupported pose payload version: {payload.get('version')!r}")
    try:
        frames = tuple(
            PoseFrame(
                frame_no=int(frame["frame_no"]),
                landmarks=tuple(
                    Landmark(
                        image_xy=(float(lm["image_xy"][0]), float(lm["image_xy"][1])),
                        world_xyz=(
                            float(lm["world_xyz"][0]),
                            float(lm["world_xyz"][1]),
                            float(lm["world_xyz"][2]),
                        ),
                        visibility=float(lm["visibility"]),
                    )
                    for lm in frame["landmarks"]
                ),
            )
            for frame in payload["frames"]
        )
        return PoseTrack(
            model_name=str(payload["model_name"]),
            model_version=str(payload["model_version"]),
            fps=float(payload["fps"]),
            frames=frames,
            subject_confidence=float(payload["subject_confidence"]),
        )
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        # ValueError covers non-numeric strings in numeric fields (float()/int()).
        raise PoseError(f"malformed pose payload: {exc}") from exc


class PoseProvider(Protocol):
    """Edge adapter contract: frames of one ball's window -> a batter pose track."""

    def extract(self, frames: Sequence[Any], *, fps: float) -> PoseTrack:
        """Extract the batter's track. ``frames`` are BGR ndarrays (or provider-specific)."""
        ...


class FakePoseProvider:
    """Deterministic synthetic skeletons for tests and offline development.

    A seeded generator produces a plausible right-handed batting stance whose head
    sways sinusoidally, so metric tests get known geometry with controllable motion.
    """

    model_name = "fake-pose"
    model_version = "1"

    def __init__(self, *, seed: int = 0, sway_px: float = 4.0, visibility: float = 0.95) -> None:
        self._seed = seed
        self._sway_px = sway_px
        self._visibility = visibility

    def _base_points(self) -> dict[int, tuple[float, float]]:
        """Anchor image points (px) of a side-on batting stance at 1920x1080."""
        anchors = {
            "nose": (960.0, 300.0),
            "left_shoulder": (930.0, 420.0),
            "right_shoulder": (990.0, 420.0),
            "left_elbow": (900.0, 520.0),
            "right_elbow": (1020.0, 520.0),
            "left_wrist": (890.0, 610.0),
            "right_wrist": (1030.0, 610.0),
            "left_hip": (940.0, 640.0),
            "right_hip": (980.0, 640.0),
            "left_knee": (930.0, 800.0),
            "right_knee": (990.0, 800.0),
            "left_ankle": (920.0, 950.0),
            "right_ankle": (1000.0, 950.0),
            "left_heel": (915.0, 965.0),
            "right_heel": (1005.0, 965.0),
            "left_foot_index": (905.0, 975.0),
            "right_foot_index": (1015.0, 975.0),
        }
        return {LANDMARK_NAMES[name]: xy for name, xy in anchors.items()}

    def _frames(self, n_frames: int) -> Iterator[PoseFrame]:
        base = self._base_points()
        for frame_no in range(n_frames):
            sway = self._sway_px * math.sin(2 * math.pi * (frame_no + self._seed) / 48.0)
            landmarks = []
            for index in range(N_LANDMARKS):
                x, y = base.get(index, (960.0, 540.0))
                x += sway if index == LANDMARK_NAMES["nose"] else sway * 0.25
                landmarks.append(
                    Landmark(
                        image_xy=(x, y),
                        world_xyz=((x - 960.0) / 1000.0, (y - 540.0) / 1000.0, 0.0),
                        visibility=self._visibility,
                    )
                )
            yield PoseFrame(frame_no=frame_no, landmarks=tuple(landmarks))

    def extract(self, frames: Sequence[Any], *, fps: float) -> PoseTrack:
        if fps <= 0:
            raise PoseError(f"fps must be positive, got {fps}")
        return PoseTrack(
            model_name=self.model_name,
            model_version=self.model_version,
            fps=fps,
            frames=tuple(self._frames(len(frames))),
            subject_confidence=0.99,
        )
