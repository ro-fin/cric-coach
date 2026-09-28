"""Ultralytics YOLO adapter (US-F2): the real-model edge behind ``DetectionProvider``.

The adapter tracks the ultralytics **8.x** predict API surface — **8.4.90** is
the version the repo lockfile resolves for the ``detect`` extra:

* ``ultralytics.YOLO(weights)`` loads trained weights from a path string.
* ``model.predict(source=frames, imgsz=..., device=..., conf=..., verbose=False)``
  with ``source`` a list of HxWx3 BGR ``numpy`` arrays returns one
  ``ultralytics.engine.results.Results`` per input frame, in input order.
* ``Results.names`` maps class index -> class-name string (the training YAML names).
* ``Results.boxes.xywhn`` / ``.conf`` / ``.cls`` are per-box tensors (``.tolist()``)
  of normalized cx/cy/w/h, confidence and class index.

Ultralytics is an optional heavyweight dependency, so it is imported lazily in
:class:`YoloDetectionProvider.__init__` — constructing the provider without the
``detect`` extra installed raises an ImportError that says how to fix it. All
pipeline logic downstream consumes the foundation
:class:`~cricai_vision.detect.Detection` stream, and tests exercise this adapter
by injecting a stub ``ultralytics`` module into ``sys.modules`` whose classes
mirror the exact names, call signatures and result shapes listed above (never
real inference), so API drift in the dependency shows up as a stub/adapter
mismatch instead of only failing in production.

Frames come from an injected :class:`FrameSource` (video decode is a separate
concern owned by the tracking job), one BGR frame per grid slot of the window —
the same ``frame_no``/``ts_ms`` grid convention as ``FakeDetectionProvider``.
Class names outside the :class:`~cricai_data.enums.LabelClass` contract fail
loudly: a renamed training class must never be silently dropped.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from cricai_data.enums import LabelClass

from cricai_vision.detect import Detection, DetectionError

_INSTALL_HINT = (
    "ultralytics is required for YoloDetectionProvider; "
    "install the detect extra: pip install 'cricai-vision[detect]'"
)


def _import_ultralytics() -> Any:
    try:
        return importlib.import_module("ultralytics")
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc


def _clip01(value: float) -> float:
    """Guard float jitter at box edges: xywhn is normalized but not exactly clamped."""
    return min(max(float(value), 0.0), 1.0)


class FrameSource(Protocol):
    """Decoded BGR frames for one clip window, one per frame-grid slot."""

    def frames(self, *, start_ms: float, end_ms: float, fps: float) -> Sequence[Any]:
        """HxWx3 BGR arrays covering the window (video-timeline ms)."""
        ...


class YoloDetectionProvider:
    """Trained-weights YOLO detector behind the foundation ``DetectionProvider``.

    ``detect`` walks the same frame grid as ``FakeDetectionProvider``, batches
    the window's frames through one ``predict`` call, and translates every box
    to a normalized :class:`Detection` with its ``LabelClass`` resolved from the
    model's class names. ``version`` records weights + ultralytics provenance.
    """

    def __init__(
        self,
        *,
        weights_path: str | Path,
        frame_source: FrameSource,
        device: str = "cpu",
        imgsz: int = 640,
        conf: float = 0.25,
    ) -> None:
        if imgsz < 1:
            raise DetectionError(f"imgsz must be >= 1, got {imgsz!r}")
        if not 0.0 <= conf <= 1.0:
            raise DetectionError(f"conf must be within [0, 1], got {conf!r}")
        module = _import_ultralytics()
        self._model = module.YOLO(str(weights_path))
        self._frame_source = frame_source
        self._device = device
        self._imgsz = imgsz
        self._conf = conf
        release = str(getattr(module, "__version__", "unknown"))
        self.version = f"yolo-{Path(weights_path).stem}-ultralytics-{release}"

    def detect(self, *, start_ms: float, end_ms: float, fps: float) -> list[Detection]:
        if end_ms <= start_ms:
            raise DetectionError(f"empty window: start_ms={start_ms}, end_ms={end_ms}")
        if fps <= 0:
            raise DetectionError(f"fps must be positive, got {fps!r}")
        frame_ms = 1000.0 / fps
        n_frames = int((end_ms - start_ms) / frame_ms) + 1
        frames = list(self._frame_source.frames(start_ms=start_ms, end_ms=end_ms, fps=fps))
        if len(frames) != n_frames:
            raise DetectionError(
                f"frame source returned {len(frames)} frames for a {n_frames}-frame window"
            )
        results = self._model.predict(
            source=frames,
            imgsz=self._imgsz,
            device=self._device,
            conf=self._conf,
            verbose=False,
        )
        if len(results) != n_frames:
            raise DetectionError(
                f"ultralytics returned {len(results)} results for {n_frames} frames"
            )
        detections: list[Detection] = []
        # TrackingWindow's grid: round the start once, then count consecutive
        # slots — per-frame rounding duplicates/skips frame numbers whenever a
        # timestamp lands on a half-integer slot (round-half-to-even).
        first_frame = round(start_ms / frame_ms)
        for index, result in enumerate(results):
            ts_ms = start_ms + index * frame_ms
            detections.extend(self._translate(result, frame_no=first_frame + index, ts_ms=ts_ms))
        return detections

    def _translate(self, result: Any, *, frame_no: int, ts_ms: float) -> list[Detection]:
        """One Results object -> Detection list; unknown classes fail loudly."""
        names = result.names
        boxes = result.boxes
        translated: list[Detection] = []
        rows = zip(boxes.xywhn.tolist(), boxes.conf.tolist(), boxes.cls.tolist(), strict=True)
        for xywhn, score, cls in rows:
            class_index = int(cls)
            name = names.get(class_index)
            if name is None:
                raise DetectionError(
                    f"class index {class_index} missing from model names {sorted(names)}"
                )
            try:
                label = LabelClass(name)
            except ValueError as exc:
                raise DetectionError(
                    f"model emitted class {name!r} outside the LabelClass contract"
                ) from exc
            cx, cy, w, h = (_clip01(value) for value in xywhn)
            translated.append(
                Detection(
                    frame_no=frame_no,
                    ts_ms=ts_ms,
                    label=label,
                    cx=cx,
                    cy=cy,
                    w=w,
                    h=h,
                    score=_clip01(score),
                )
            )
        return translated
