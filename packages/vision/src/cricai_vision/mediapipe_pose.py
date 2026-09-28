"""MediaPipe Pose adapter (US-E1): the real-model edge behind ``PoseProvider``.

Targets the MediaPipe **Tasks** API — the only pose surface modern wheels ship
(the legacy ``mediapipe.solutions`` API was removed from the package in
0.10.30). The adapter tracks the Tasks API surface of **mediapipe 0.10.35**,
the version the repo lockfile resolves for the ``pose`` extra:

* ``mediapipe.Image(image_format=mediapipe.ImageFormat.SRGB, data=...)``
* ``mediapipe.tasks.BaseOptions(model_asset_path=...)``
* ``mediapipe.tasks.vision.PoseLandmarkerOptions(base_options, running_mode,
  num_poses, min_pose_detection_confidence, min_pose_presence_confidence,
  min_tracking_confidence, ...)``
* ``mediapipe.tasks.vision.RunningMode.VIDEO``
* ``mediapipe.tasks.vision.PoseLandmarker.create_from_options`` /
  ``detect_for_video(image, timestamp_ms)`` / ``close``
* ``PoseLandmarkerResult.pose_landmarks`` / ``pose_world_landmarks`` — lists of
  per-person landmark lists (``x``/``y``/``z``/``visibility`` per landmark).

MediaPipe is an optional heavyweight dependency, so it is imported lazily in
:class:`MediaPipePoseProvider.__init__` — constructing the provider without the
``pose`` extra installed raises an ImportError that says how to fix it. All
pipeline logic downstream consumes the foundation
:class:`~cricai_vision.pose.PoseTrack`, and tests exercise this adapter by
injecting a stub ``mediapipe`` module into ``sys.modules`` whose classes mirror
the exact names, call signatures and result shapes listed above (never real
inference), so API drift in the dependency shows up as a stub/adapter mismatch.

Per frame, every detected person becomes a
:class:`~cricai_vision.person_select.PersonCandidate` and the batter is chosen
by :func:`~cricai_vision.person_select.select_batter` (crease-zone prior +
track continuity), so a coach or parent wandering into frame is rejected rather
than tracked. Frames with no plausible batter are emitted with zero visibility —
flagged, never fabricated (US-E1 AC). Model name and version are recorded on
every track (US-E1: output schema versioned; model name/version per run).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

import numpy as np

from cricai_vision.person_select import Box, PersonCandidate, select_batter
from cricai_vision.pose import N_LANDMARKS, Landmark, PoseError, PoseFrame, PoseTrack

MODEL_NAME = "mediapipe-pose"

#: Where the batter stands in the side-on reference framing (normalized x0, y0, x1, y1).
DEFAULT_CREASE_ZONE: Box = (0.3, 0.2, 0.7, 0.95)

#: People the landmarker looks for per frame: batter plus a few bystanders
#: (coach behind the net, a parent, a sibling) for ``select_batter`` to reject.
DEFAULT_NUM_POSES = 4

_INSTALL_HINT = (
    "mediapipe is required for MediaPipePoseProvider; "
    "install the pose extra: pip install 'cricai-vision[pose]'"
)

#: Landmark placeholder for frames where no batter was selected (zero visibility).
_ABSENT_LANDMARK = Landmark(image_xy=(0.0, 0.0), world_xyz=(0.0, 0.0, 0.0), visibility=0.0)


def _import_mediapipe() -> Any:
    try:
        return importlib.import_module("mediapipe")
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc


def _absent_frame(frame_no: int) -> PoseFrame:
    """A flagged 'no batter here' frame: all landmarks at zero visibility."""
    return PoseFrame(frame_no=frame_no, landmarks=(_ABSENT_LANDMARK,) * N_LANDMARKS)


def _clip01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _candidate_from_landmarks(landmarks: Sequence[Any]) -> PersonCandidate:
    """Bounding box + mean visibility of one person's normalized-landmark list."""
    xs = [_clip01(float(lm.x)) for lm in landmarks]
    ys = [_clip01(float(lm.y)) for lm in landmarks]
    visibilities = [float(lm.visibility) for lm in landmarks]
    return PersonCandidate(
        bbox=(min(xs), min(ys), max(xs), max(ys)),
        mean_visibility=sum(visibilities) / len(visibilities),
    )


def _translate_frame(
    image_pose: Sequence[Any],
    world_pose: Sequence[Any] | None,
    frame_no: int,
    width: int,
    height: int,
) -> PoseFrame:
    """One person's landmark lists -> foundation frame: image px, world meters."""
    if len(image_pose) != N_LANDMARKS:
        raise PoseError(f"mediapipe returned {len(image_pose)} landmarks, expected {N_LANDMARKS}")
    landmarks = []
    for index in range(N_LANDMARKS):
        image_lm = image_pose[index]
        if world_pose is not None:
            world_lm = world_pose[index]
            world_xyz = (float(world_lm.x), float(world_lm.y), float(world_lm.z))
        else:
            world_xyz = (0.0, 0.0, 0.0)
        landmarks.append(
            Landmark(
                image_xy=(float(image_lm.x) * width, float(image_lm.y) * height),
                world_xyz=world_xyz,
                visibility=float(image_lm.visibility),
            )
        )
    return PoseFrame(frame_no=frame_no, landmarks=tuple(landmarks))


class MediaPipePoseProvider:
    """Real MediaPipe pose landmarker behind the foundation ``PoseProvider`` protocol.

    ``extract`` runs a Tasks-API ``PoseLandmarker`` in VIDEO mode over BGR
    ndarray frames, selects the batter among the detected people per frame, and
    translates the selected person's landmarks into the foundation dataclasses
    (image px = normalized coords x frame size, world meters, visibility).
    ``model_version`` is the installed mediapipe version.

    ``model_asset_path`` points at a pose landmarker ``.task`` bundle
    (lite/full/heavy); the Tasks API selects model complexity by asset, which
    replaces the legacy API's ``model_complexity`` knob. The niche
    ``min_pose_presence_confidence`` option is left at the Tasks API default.
    """

    model_name = MODEL_NAME

    def __init__(
        self,
        *,
        model_asset_path: str,
        crease_zone: Box = DEFAULT_CREASE_ZONE,
        num_poses: int = DEFAULT_NUM_POSES,
        min_pose_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
    ) -> None:
        self._mediapipe = _import_mediapipe()
        self.model_version = str(getattr(self._mediapipe, "__version__", "unknown"))
        self._model_asset_path = model_asset_path
        self._crease_zone = crease_zone
        self._num_poses = num_poses
        self._min_pose_detection_confidence = min_pose_detection_confidence
        self._min_tracking_confidence = min_tracking_confidence

    def _make_landmarker(self) -> Any:
        """A fresh VIDEO-mode landmarker: per-ball windows never share tracker state."""
        tasks = self._mediapipe.tasks
        options = tasks.vision.PoseLandmarkerOptions(
            base_options=tasks.BaseOptions(model_asset_path=self._model_asset_path),
            running_mode=tasks.vision.RunningMode.VIDEO,
            num_poses=self._num_poses,
            min_pose_detection_confidence=self._min_pose_detection_confidence,
            min_tracking_confidence=self._min_tracking_confidence,
        )
        return tasks.vision.PoseLandmarker.create_from_options(options)

    def extract(self, frames: Sequence[Any], *, fps: float) -> PoseTrack:
        """Extract the batter's pose track from BGR ndarray frames."""
        if fps <= 0:
            raise PoseError(f"fps must be positive, got {fps}")
        landmarker = self._make_landmarker()
        out: list[PoseFrame] = []
        confidences: list[float] = []
        previous: PersonCandidate | None = None
        last_timestamp_ms = -1
        try:
            for frame_no, frame in enumerate(frames):
                height, width = int(frame.shape[0]), int(frame.shape[1])
                image = self._mediapipe.Image(
                    image_format=self._mediapipe.ImageFormat.SRGB,
                    # BGR -> RGB, materialized C-contiguously: the 0.10.35
                    # binding copies raw bytes from the buffer pointer with no
                    # stride info, so a negative-stride reversed view would be
                    # read shifted and channel-scrambled (plus 2 bytes out of
                    # bounds) instead of channel-reversed.
                    data=np.ascontiguousarray(frame[:, :, ::-1]),
                )
                timestamp_ms = round(frame_no * 1000.0 / fps)
                if timestamp_ms <= last_timestamp_ms:
                    timestamp_ms = last_timestamp_ms + 1  # VIDEO mode: strictly increasing
                last_timestamp_ms = timestamp_ms
                result = landmarker.detect_for_video(image, timestamp_ms)
                poses = result.pose_landmarks
                if not poses:
                    out.append(_absent_frame(frame_no))
                    continue  # keep `previous`: brief occlusion must not drop the track
                candidates = [_candidate_from_landmarks(pose) for pose in poses]
                selection = select_batter(
                    candidates, crease_zone=self._crease_zone, previous=previous
                )
                if selection.candidate is None or selection.confidence is None:
                    out.append(_absent_frame(frame_no))  # flagged, never fabricated
                    continue
                previous = selection.candidate
                confidences.append(selection.confidence)
                pose_index = candidates.index(selection.candidate)
                world_poses = result.pose_world_landmarks
                world_pose = world_poses[pose_index] if pose_index < len(world_poses) else None
                out.append(_translate_frame(poses[pose_index], world_pose, frame_no, width, height))
        finally:
            landmarker.close()
        subject_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return PoseTrack(
            model_name=self.model_name,
            model_version=self.model_version,
            fps=fps,
            frames=tuple(out),
            subject_confidence=subject_confidence,
        )
