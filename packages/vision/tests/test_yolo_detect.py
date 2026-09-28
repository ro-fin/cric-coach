"""US-F2 ultralytics adapter tests: translation + lazy import, all over a stub.

Real ultralytics never runs here — a fake ``ultralytics`` module is injected
into ``sys.modules``, per the Phase 3/4 test contract. The stub mirrors the
REAL API surface of ultralytics 8.x (8.4.90 in the repo lockfile for the
``detect`` extra): ``YOLO(weights)`` construction,
``predict(source=..., imgsz=..., device=..., conf=..., verbose=...)`` returning
one Results per input frame, ``Results.names`` as dict[int, str] and
``Results.boxes.xywhn/.conf/.cls`` exposing ``.tolist()`` (numpy stands in for
torch tensors — same ``.tolist()`` surface). The fake ``predict`` enforces the
real input contract (list of HxWx3 ndarray frames, keyword-only options,
verbose off) so adapter drift — wrong source type, a lost ``verbose=False``,
positional options — fails these tests instead of only failing in production.
"""

import sys
import types
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import numpy as np
import pytest
from cricai_data.enums import LabelClass
from cricai_vision.detect import DetectionError, DetectionProvider
from cricai_vision.track import TrackingWindow
from cricai_vision.yolo_detect import YoloDetectionProvider

WEIGHTS = "models/ball-detector.pt"

#: The class-name table our detectors are trained with (LabelClass values).
NAMES = {0: "ball", 1: "bat", 2: "stumps"}


@dataclass
class _FakeBoxes:
    """Mirrors ``ultralytics.engine.results.Boxes``: per-box tensors (numpy stands
    in for torch — identical ``.tolist()`` surface)."""

    xywhn: Any
    conf: Any
    cls: Any

    @classmethod
    def of(cls, rows: Sequence[tuple[float, float, float, float, float, int]]) -> "_FakeBoxes":
        return cls(
            xywhn=np.array([row[:4] for row in rows], dtype=np.float64).reshape(len(rows), 4),
            conf=np.array([row[4] for row in rows], dtype=np.float64),
            cls=np.array([row[5] for row in rows], dtype=np.float64),
        )


@dataclass
class _FakeResults:
    """Mirrors ``ultralytics.engine.results.Results`` (detect-task subset)."""

    boxes: _FakeBoxes
    names: dict[int, str] = field(default_factory=lambda: dict(NAMES))


def _empty() -> _FakeResults:
    return _FakeResults(boxes=_FakeBoxes.of([]))


class _StubModule(types.ModuleType):
    YOLO: Any
    __version__: str


def _install_stub(
    monkeypatch: pytest.MonkeyPatch,
    batches: Sequence[Sequence[_FakeResults]],
    *,
    version: str | None = "8.4.90",
) -> list[Any]:
    """Inject a fake ``ultralytics`` module; returns the created YOLO instances."""
    created: list[Any] = []
    queued = [list(batch) for batch in batches]

    class FakeYOLO:
        """Mirrors ``ultralytics.YOLO`` (predict subset of the 8.x surface)."""

        def __init__(self, model: str) -> None:
            assert isinstance(model, str)  # the adapter must stringify Path weights
            self.model = model
            self.calls: list[dict[str, Any]] = []
            created.append(self)

        def predict(
            self,
            source: Any = None,
            *,
            imgsz: int = 640,
            device: Any = None,
            conf: float = 0.25,
            verbose: bool = True,
        ) -> list[_FakeResults]:
            # Real predict accepts many source types; the adapter's contract is a
            # list of decoded HxWx3 ndarray frames — enforce it so drift fails here.
            assert isinstance(source, list)
            assert all(isinstance(frame, np.ndarray) and frame.ndim == 3 for frame in source)
            assert isinstance(imgsz, int) and not isinstance(imgsz, bool)
            assert isinstance(device, str)
            assert isinstance(conf, float)
            assert verbose is False  # a chatty detector in the worker is a bug
            self.calls.append(
                {"n_frames": len(source), "imgsz": imgsz, "device": device, "conf": conf}
            )
            return queued.pop(0)

    module = _StubModule("ultralytics")
    module.YOLO = FakeYOLO
    if version is not None:
        module.__version__ = version
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    return created


@dataclass
class _ListFrameSource:
    """Test FrameSource: hands back a fixed list of frames and records the window."""

    count: int
    windows: list[tuple[float, float, float]] = field(default_factory=list)

    def frames(self, *, start_ms: float, end_ms: float, fps: float) -> Sequence[Any]:
        self.windows.append((start_ms, end_ms, fps))
        return [np.zeros((4, 6, 3), dtype=np.uint8) for _ in range(self.count)]


def _provider(count: int = 1, **kwargs: Any) -> tuple[YoloDetectionProvider, _ListFrameSource]:
    source = _ListFrameSource(count=count)
    provider = YoloDetectionProvider(weights_path=WEIGHTS, frame_source=source, **kwargs)
    return provider, source


def test_translates_boxes_to_normalized_detections(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = [
        _FakeResults(
            boxes=_FakeBoxes.of([(0.5, 0.6, 0.02, 0.03, 0.9, 0), (0.8, 0.7, 0.1, 0.2, 0.85, 1)])
        ),
        _FakeResults(boxes=_FakeBoxes.of([(0.52, 0.58, 0.02, 0.03, 0.88, 2)])),
        _empty(),
    ]
    created = _install_stub(monkeypatch, [batch])
    provider, source = _provider(count=3, device="cuda:0", imgsz=1280, conf=0.4)
    typed: DetectionProvider = provider  # protocol conformance (mypy-checked)
    detections = typed.detect(start_ms=1000.0, end_ms=1020.0, fps=100.0)

    assert [d.label for d in detections] == [LabelClass.BALL, LabelClass.BAT, LabelClass.STUMPS]
    ball, bat, stumps = detections
    assert (ball.frame_no, ball.ts_ms) == (100, 1000.0)
    assert (ball.cx, ball.cy, ball.w, ball.h, ball.score) == (0.5, 0.6, 0.02, 0.03, 0.9)
    assert (bat.frame_no, bat.ts_ms) == (100, 1000.0)
    assert (stumps.frame_no, stumps.ts_ms) == (101, 1010.0)
    assert source.windows == [(1000.0, 1020.0, 100.0)]
    assert created[0].model == WEIGHTS
    assert created[0].calls == [{"n_frames": 3, "imgsz": 1280, "device": "cuda:0", "conf": 0.4}]
    assert typed.version == "yolo-ball-detector-ultralytics-8.4.90"


def test_defaults_and_path_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_stub(monkeypatch, [[_empty()]])
    source = _ListFrameSource(count=1)
    provider = YoloDetectionProvider(weights_path=Path(WEIGHTS), frame_source=source)
    assert provider.detect(start_ms=0.0, end_ms=5.0, fps=100.0) == []
    assert created[0].calls == [{"n_frames": 1, "imgsz": 640, "device": "cpu", "conf": 0.25}]


def test_frame_grid_matches_tracking_window_at_half_integer_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fps=50 with start_ms=4010 (slot 200.5): frame numbers must follow the
    window grid round(start/frame_ms) + index, never per-frame rounding whose
    round-half-to-even duplicates/skips slots (review yolo_detect.py:126)."""
    batch = [_FakeResults(boxes=_FakeBoxes.of([(0.5, 0.5, 0.02, 0.02, 0.9, 0)])) for _ in range(3)]
    _install_stub(monkeypatch, [batch])
    provider, _ = _provider(count=3)
    window = TrackingWindow(start_ms=4010.0, end_ms=4050.0, fps=50.0, width=1920, height=1080)
    detections = provider.detect(start_ms=4010.0, end_ms=4050.0, fps=50.0)
    assert [d.frame_no for d in detections] == list(
        range(window.first_frame, window.last_frame + 1)
    )
    assert [d.ts_ms for d in detections] == [4010.0, 4030.0, 4050.0]


def test_missing_ultralytics_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ultralytics", cast(ModuleType, None))
    with pytest.raises(ImportError, match=r"cricai-vision\[detect\]"):
        _provider()


def test_missing_version_recorded_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(monkeypatch, [], version=None)
    provider, _ = _provider()
    assert provider.version == "yolo-ball-detector-ultralytics-unknown"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"imgsz": 0}, "imgsz"), ({"conf": 1.5}, "conf"), ({"conf": -0.1}, "conf")],
)
def test_rejects_bad_construction(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], match: str
) -> None:
    _install_stub(monkeypatch, [])
    with pytest.raises(DetectionError, match=match):
        _provider(**kwargs)


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "fps", "match"),
    [
        (1000.0, 1000.0, 30.0, "empty window"),
        (2000.0, 1000.0, 30.0, "empty window"),
        (0.0, 1000.0, 0.0, "fps"),
        (0.0, 1000.0, -25.0, "fps"),
    ],
)
def test_rejects_bad_windows_before_decoding_or_predicting(
    monkeypatch: pytest.MonkeyPatch, start_ms: float, end_ms: float, fps: float, match: str
) -> None:
    created = _install_stub(monkeypatch, [])
    provider, source = _provider()
    with pytest.raises(DetectionError, match=match):
        provider.detect(start_ms=start_ms, end_ms=end_ms, fps=fps)
    assert source.windows == []  # rejected before any frame was requested
    assert created[0].calls == []


def test_frame_count_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_stub(monkeypatch, [])
    provider, _ = _provider(count=2)  # window below needs 3 frames
    with pytest.raises(DetectionError, match="returned 2 frames for a 3-frame window"):
        provider.detect(start_ms=0.0, end_ms=20.0, fps=100.0)
    assert created[0].calls == []  # never reached the model


def test_result_count_drift_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dependency drift: predict returning fewer results than frames must fail
    # loudly instead of silently shifting every later frame's timestamps.
    _install_stub(monkeypatch, [[_empty(), _empty()]])
    provider, _ = _provider(count=3)
    with pytest.raises(DetectionError, match="returned 2 results for 3 frames"):
        provider.detect(start_ms=0.0, end_ms=20.0, fps=100.0)


def test_unknown_class_name_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _FakeResults(
        boxes=_FakeBoxes.of([(0.5, 0.5, 0.1, 0.1, 0.9, 0)]),
        names={0: "wicketkeeper"},
    )
    _install_stub(monkeypatch, [[result]])
    provider, _ = _provider()
    with pytest.raises(DetectionError, match="'wicketkeeper' outside the LabelClass contract"):
        provider.detect(start_ms=0.0, end_ms=5.0, fps=100.0)


def test_class_index_missing_from_names_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _FakeResults(boxes=_FakeBoxes.of([(0.5, 0.5, 0.1, 0.1, 0.9, 9)]))
    _install_stub(monkeypatch, [[result]])
    provider, _ = _provider()
    with pytest.raises(DetectionError, match="class index 9 missing"):
        provider.detect(start_ms=0.0, end_ms=5.0, fps=100.0)


def test_edge_boxes_are_clipped_against_float_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    # xywhn is normalized by image size; boxes clipped at the frame edge can land
    # a hair outside [0, 1] in float32 — the adapter clamps instead of crashing.
    result = _FakeResults(boxes=_FakeBoxes.of([(1.0000001, -0.0000001, 0.02, 0.02, 0.9, 0)]))
    _install_stub(monkeypatch, [[result]])
    provider, _ = _provider()
    detection = provider.detect(start_ms=0.0, end_ms=5.0, fps=100.0)[0]
    assert (detection.cx, detection.cy) == (1.0, 0.0)
