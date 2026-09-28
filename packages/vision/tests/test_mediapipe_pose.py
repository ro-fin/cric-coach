"""US-E1 MediaPipe adapter tests: translation + selection + lazy import, all over a stub.

Real MediaPipe never runs here — a fake ``mediapipe`` module is injected into
``sys.modules``, per the Phase 3 test contract. The stub mirrors the REAL
Tasks API surface of mediapipe 0.10.35 (the version the lockfile resolves for
the ``pose`` extra): class names, call signatures and result shapes match the
installed wheel's ``mediapipe.Image`` / ``mediapipe.ImageFormat`` /
``mediapipe.tasks.BaseOptions`` / ``mediapipe.tasks.vision.PoseLandmarker*``
one-to-one, so adapter/dependency drift (like the removed legacy
``mediapipe.solutions`` API) fails these tests instead of only failing in
production. The fake landmarker also records every image it receives and
enforces VIDEO-mode invariants (strictly increasing integer timestamps), and
the fake ``Image`` enforces the real constructor's C-contiguity data contract,
so losing the BGR->RGB conversion, its contiguous materialization, or the
running-mode wiring is caught here.
"""

import enum
import sys
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, cast

import numpy as np
import pytest
from cricai_vision.mediapipe_pose import (
    DEFAULT_CREASE_ZONE,
    DEFAULT_NUM_POSES,
    MODEL_NAME,
    MediaPipePoseProvider,
)
from cricai_vision.pose import N_LANDMARKS, PoseError, PoseProvider

#: Any string path works against the stub; the real API loads a .task bundle from it.
MODEL_ASSET = "models/pose_landmarker_full.task"


@dataclass
class _NormalizedLandmark:
    """Mirrors ``mediapipe.tasks.python.components.containers.landmark.NormalizedLandmark``."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    visibility: float | None = None
    presence: float | None = None
    name: str | None = None


@dataclass
class _WorldLandmark:
    """Mirrors ``...containers.landmark.Landmark`` (world coordinates, meters)."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    visibility: float | None = None
    presence: float | None = None
    name: str | None = None


@dataclass
class _PoseLandmarkerResult:
    """Mirrors ``mediapipe.tasks.python.vision.PoseLandmarkerResult``: per-person lists."""

    pose_landmarks: list[list[_NormalizedLandmark]]
    pose_world_landmarks: list[list[_WorldLandmark]]
    segmentation_masks: list[Any] | None = None


@dataclass
class _BaseOptions:
    """Mirrors ``mediapipe.tasks.BaseOptions``."""

    model_asset_path: str | None = None
    model_asset_buffer: bytes | None = None
    delegate: Any | None = None


class _RunningMode(enum.Enum):
    """Mirrors ``mediapipe.tasks.vision.RunningMode`` (VisionTaskRunningMode)."""

    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    LIVE_STREAM = "LIVE_STREAM"


@dataclass
class _PoseLandmarkerOptions:
    """Mirrors ``mediapipe.tasks.vision.PoseLandmarkerOptions`` (same fields/defaults)."""

    base_options: _BaseOptions
    running_mode: _RunningMode = _RunningMode.IMAGE
    num_poses: int = 1
    min_pose_detection_confidence: float = 0.5
    min_pose_presence_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    output_segmentation_masks: bool = False
    result_callback: Any | None = None


class _ImageFormat(enum.Enum):
    """Mirrors the ``mediapipe.ImageFormat`` members the adapter may use."""

    SRGB = 1
    SRGBA = 2


class _FakeImage:
    """Mirrors ``mediapipe.Image(image_format=..., data=...)``; snapshots the pixels.

    The real 0.10.35 constructor copies raw bytes from the buffer pointer with
    no stride information (``MpImageCreateFromUint8Data`` receives only format,
    width and height), so it requires C-contiguous pixel data: a negative-stride
    view (e.g. a bare ``frame[:, :, ::-1]``) is silently copied byte-shifted and
    channel-scrambled, with a 2-byte out-of-bounds read. The stub mirrors that
    data contract loudly — non-contiguous input raises instead of corrupting —
    which pins the adapter's ``np.ascontiguousarray`` materialization. The
    snapshot is what lets tests verify the channel order handed to the model.
    """

    def __init__(self, *, image_format: _ImageFormat, data: Any) -> None:
        assert image_format is _ImageFormat.SRGB  # the adapter must hand the model RGB
        if not data.flags["C_CONTIGUOUS"]:
            raise ValueError(
                "mediapipe.Image requires C-contiguous pixel data; the real "
                "0.10.35 binding would silently copy corrupted bytes from a "
                "non-contiguous view"
            )
        self.image_format = image_format
        self.data: Any = np.array(data, copy=True)

    def numpy_view(self) -> Any:
        return self.data

    @property
    def width(self) -> int:
        return int(self.data.shape[1])

    @property
    def height(self) -> int:
        return int(self.data.shape[0])

    @property
    def channels(self) -> int:
        return int(self.data.shape[2])


class _StubModule(types.ModuleType):
    Image: Any
    ImageFormat: Any
    tasks: Any
    __version__: str


def _install_stub(
    monkeypatch: pytest.MonkeyPatch,
    results: Sequence[_PoseLandmarkerResult],
    *,
    version: str | None = "0.10.35",
) -> list[Any]:
    """Inject a fake ``mediapipe`` module; returns the created PoseLandmarker instances."""
    created: list[Any] = []

    @dataclass
    class FakePoseLandmarker:
        """Mirrors ``mediapipe.tasks.vision.PoseLandmarker`` (VIDEO-mode subset)."""

        options: _PoseLandmarkerOptions
        images: list[_FakeImage] = field(default_factory=list)
        timestamps: list[int] = field(default_factory=list)
        closed: bool = False

        def __post_init__(self) -> None:
            self._results = iter(results)
            created.append(self)

        @classmethod
        def create_from_options(cls, options: _PoseLandmarkerOptions) -> "FakePoseLandmarker":
            assert isinstance(options, _PoseLandmarkerOptions)
            assert isinstance(options.base_options, _BaseOptions)
            return cls(options)

        def detect_for_video(
            self,
            image: _FakeImage,
            timestamp_ms: int,
            image_processing_options: Any | None = None,
        ) -> _PoseLandmarkerResult:
            # Real detect_for_video requires VIDEO mode, an mp.Image, and
            # strictly increasing integer timestamps — enforce all three.
            assert image_processing_options is None  # the adapter never passes options
            assert self.options.running_mode is _RunningMode.VIDEO
            assert isinstance(image, _FakeImage)
            assert isinstance(timestamp_ms, int) and not isinstance(timestamp_ms, bool)
            if self.timestamps:
                assert timestamp_ms > self.timestamps[-1]
            self.images.append(image)
            self.timestamps.append(timestamp_ms)
            return next(self._results)

        def close(self) -> None:
            self.closed = True

    module = _StubModule("mediapipe")
    module.Image = _FakeImage
    module.ImageFormat = _ImageFormat
    module.tasks = types.SimpleNamespace(
        BaseOptions=_BaseOptions,
        vision=types.SimpleNamespace(
            PoseLandmarker=FakePoseLandmarker,
            PoseLandmarkerOptions=_PoseLandmarkerOptions,
            PoseLandmarkerResult=_PoseLandmarkerResult,
            RunningMode=_RunningMode,
        ),
    )
    if version is not None:
        module.__version__ = version
    monkeypatch.setitem(sys.modules, "mediapipe", module)
    return created


def _provider(**kwargs: Any) -> MediaPipePoseProvider:
    return MediaPipePoseProvider(model_asset_path=MODEL_ASSET, **kwargs)


def _person(
    cx: float, cy: float, *, spread: float = 0.05, visibility: float = 0.95
) -> list[_NormalizedLandmark]:
    """33 landmark points spread around (cx, cy) in normalized coordinates."""
    return [
        _NormalizedLandmark(
            x=cx + ((i % 3) - 1) * spread,
            y=cy + ((i % 5) - 2) * spread * 0.5,
            z=i / 100.0,
            visibility=visibility,
        )
        for i in range(N_LANDMARKS)
    ]


def _world_person() -> list[_WorldLandmark]:
    return [
        _WorldLandmark(x=i * 0.01, y=i * 0.02, z=i * 0.03, visibility=0.95)
        for i in range(N_LANDMARKS)
    ]


def _frame(height: int = 1080, width: int = 1920) -> Any:
    return np.zeros((height, width, 3), dtype=np.uint8)


#: A batter standing inside DEFAULT_CREASE_ZONE.
def _batter() -> list[_NormalizedLandmark]:
    assert DEFAULT_CREASE_ZONE == (0.3, 0.2, 0.7, 0.95)
    return _person(0.5, 0.6)


def _detected(
    image: list[_NormalizedLandmark], world: list[_WorldLandmark] | None = None
) -> _PoseLandmarkerResult:
    """One detected person, shaped exactly like a real PoseLandmarkerResult."""
    return _PoseLandmarkerResult(
        pose_landmarks=[image],
        pose_world_landmarks=[world] if world is not None else [],
    )


_NOBODY = _PoseLandmarkerResult(pose_landmarks=[], pose_world_landmarks=[])


def test_translates_landmarks_and_records_model_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_pts = _batter()
    world_pts = _world_person()
    created = _install_stub(monkeypatch, [_detected(image_pts, world_pts)])
    provider: PoseProvider = _provider(
        num_poses=2,
        min_pose_detection_confidence=0.6,
        min_tracking_confidence=0.8,
    )
    track = provider.extract([_frame()], fps=120.0)
    assert track.model_name == MODEL_NAME == "mediapipe-pose"
    assert track.model_version == "0.10.35"
    assert track.fps == 120.0
    assert track.frame_count == 1
    for index in (0, 32):
        landmark = track.frames[0].landmarks[index]
        assert landmark.image_xy[0] == pytest.approx(cast(float, image_pts[index].x) * 1920)
        assert landmark.image_xy[1] == pytest.approx(cast(float, image_pts[index].y) * 1080)
        assert landmark.world_xyz == (
            world_pts[index].x,
            world_pts[index].y,
            world_pts[index].z,
        )
        assert landmark.visibility == 0.95
    assert 0.0 < track.subject_confidence <= 1.0
    options = created[0].options
    assert options.base_options.model_asset_path == MODEL_ASSET
    assert options.running_mode.name == "VIDEO"
    assert options.num_poses == 2
    assert options.min_pose_detection_confidence == 0.6
    assert options.min_pose_presence_confidence == 0.5  # left at the API default
    assert options.min_tracking_confidence == 0.8
    assert options.output_segmentation_masks is False
    assert created[0].closed is True


def test_frames_are_converted_bgr_to_rgb(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_stub(monkeypatch, [_detected(_batter(), _world_person())])
    # Non-square frame with distinct per-channel planes: any lost or wrong-axis
    # reversal changes what the recorded image contains (or its shape).
    frame = np.zeros((4, 6, 3), dtype=np.uint8)
    frame[:, :, 0] = 10  # blue plane (BGR input)
    frame[:, :, 1] = 20  # green plane
    frame[:, :, 2] = 30  # red plane
    _provider().extract([frame], fps=30.0)
    received = created[0].images[0]
    assert received.data.shape == (4, 6, 3)
    assert (received.data[:, :, 0] == 30).all()  # red first: the model got RGB
    assert (received.data[:, :, 1] == 20).all()
    assert (received.data[:, :, 2] == 10).all()  # blue last


def test_video_timestamps_follow_fps_and_stay_strictly_increasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _detected(_batter(), _world_person())
    created = _install_stub(monkeypatch, [result, result, result])
    provider = _provider()
    provider.extract([_frame(2, 2)] * 3, fps=120.0)
    assert created[0].timestamps == [0, 8, 17]  # round(frame_no * 1000 / fps)
    # High-speed capture: rounding alone would repeat 0 ms, which VIDEO mode
    # rejects — the adapter bumps to keep timestamps strictly increasing.
    provider.extract([_frame(2, 2)] * 3, fps=4000.0)
    assert created[1].timestamps == [0, 1, 2]


def test_no_detection_yields_flagged_frame_and_keeps_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detected = _detected(_batter(), _world_person())
    created = _install_stub(monkeypatch, [detected, _NOBODY, detected])
    track = _provider().extract([_frame()] * 3, fps=60.0)
    assert track.frame_count == 3
    # The occluded frame is flagged with zero visibility, never fabricated.
    assert all(lm.visibility == 0.0 for lm in track.frames[1].landmarks)
    assert track.frames[1].landmark("nose").image_xy == (0.0, 0.0)
    # The batter is re-selected after the occlusion (previous track retained).
    assert track.frames[2].landmark("nose").visibility == 0.95
    assert track.availability() == pytest.approx(2 / 3)
    assert created[0].options.num_poses == DEFAULT_NUM_POSES == 4


def test_person_outside_crease_zone_is_rejected_not_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intruder = _person(0.06, 0.08, spread=0.02, visibility=0.9)
    _install_stub(monkeypatch, [_detected(intruder, _world_person())])
    track = _provider().extract([_frame()], fps=30.0)
    assert track.subject_confidence == 0.0
    assert track.availability() == 0.0
    assert all(lm.visibility == 0.0 for lm in track.frames[0].landmarks)


def test_batter_selected_among_multiple_detected_people(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intruder = _person(0.06, 0.08, spread=0.02, visibility=0.9)
    batter = _batter()
    result = _PoseLandmarkerResult(
        pose_landmarks=[intruder, batter],
        pose_world_landmarks=[_world_person(), _world_person()],
    )
    _install_stub(monkeypatch, [result])
    track = _provider().extract([_frame()], fps=30.0)
    nose = track.frames[0].landmark("nose")
    assert nose.image_xy[0] == pytest.approx(cast(float, batter[0].x) * 1920)
    assert nose.image_xy[1] == pytest.approx(cast(float, batter[0].y) * 1080)
    assert track.subject_confidence > 0.0


def test_missing_world_landmarks_fall_back_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(monkeypatch, [_detected(_batter(), world=None)])
    track = _provider().extract([_frame()], fps=30.0)
    nose = track.frames[0].landmark("nose")
    assert nose.world_xyz == (0.0, 0.0, 0.0)
    assert nose.visibility == 0.95


def test_wrong_landmark_count_raises_and_landmarker_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truncated = _batter()[:10]
    created = _install_stub(monkeypatch, [_detected(truncated)])
    with pytest.raises(PoseError, match="expected 33"):
        _provider().extract([_frame()], fps=30.0)
    assert created[0].closed is True


def test_missing_mediapipe_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mediapipe", cast(ModuleType, None))
    with pytest.raises(ImportError, match=r"cricai-vision\[pose\]"):
        _provider()


def test_non_positive_fps_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_stub(monkeypatch, [])
    provider = _provider()
    with pytest.raises(PoseError, match="fps must be positive"):
        provider.extract([], fps=0.0)
    assert created == []  # rejected before any landmarker was created


def test_missing_version_recorded_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_stub(monkeypatch, [], version=None)
    track = _provider().extract([], fps=120.0)
    assert track.model_version == "unknown"
    assert track.frame_count == 0
    assert track.subject_confidence == 0.0
    assert created[0].closed is True
