"""Object-detection foundation (US-F2/F3): dataclasses, provider protocol, fake.

Tracking (US-F3) and downstream consumers depend on :class:`Detection` streams and
never on a concrete detector, so tests run on :class:`FakeDetectionProvider`'s
seeded synthetic scenes and the real ultralytics adapter (story f2's
``cricai_vision.yolo_detect``) stays a thin, swappable edge.

Boxes are normalized YOLO-style ``cx/cy/w/h`` in [0, 1] so the same payloads
work at any resolution; ``ts_ms`` mirrors the video-timeline convention used by
``ball_events`` (reference camera C1).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Protocol

from cricai_data.enums import LabelClass


class DetectionError(ValueError):
    """Raised for invalid detection inputs (bad boxes, empty windows)."""


@dataclass(frozen=True)
class Detection:
    """One detected box on one frame."""

    frame_no: int
    ts_ms: float
    label: LabelClass
    cx: float
    cy: float
    w: float
    h: float
    score: float

    def __post_init__(self) -> None:
        for name in ("cx", "cy", "w", "h", "score"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise DetectionError(f"{name} must be finite and within [0, 1], got {value!r}")


class DetectionProvider(Protocol):
    """Detects objects on the frames of one clip window.

    Implementations yield detections ordered by ``frame_no``; a frame with no
    detections simply yields nothing for that frame (absence is data — the
    tracker's gap-bridging handles it, US-F3).
    """

    #: Identifier recorded in provenance columns (detector_version).
    version: str

    def detect(self, *, start_ms: float, end_ms: float, fps: float) -> list[Detection]:
        """Detect objects on every frame of the window (video-timeline ms)."""
        ...


@dataclass
class FakeDetectionProvider:
    """Seeded, deterministic synthetic detector — the only provider tests use.

    Emits a parametric ball flight (horizontal sweep with a parabolic bounce at
    ``bounce_at`` fraction of the window), static bat/stumps boxes, optional
    per-frame dropout (missed detections) and decoy balls (the second ball
    lying in the net, US-F3 identity stress). Identical construction arguments
    always produce identical output.
    """

    seed: int = 7
    dropout: float = 0.0  # fraction of ball frames with no ball detection
    decoys: int = 0  # static decoy balls per frame
    bounce_at: float = 0.6  # fraction of the window where the ball bounces
    version: str = "fake-detect-1"
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0.0 <= self.dropout < 1.0:
            raise DetectionError(f"dropout must be within [0, 1), got {self.dropout!r}")
        if not 0.0 < self.bounce_at < 1.0:
            raise DetectionError(f"bounce_at must be within (0, 1), got {self.bounce_at!r}")
        if self.decoys < 0:
            raise DetectionError(f"decoys must be >= 0, got {self.decoys!r}")
        self._rng = random.Random(self.seed)

    def detect(self, *, start_ms: float, end_ms: float, fps: float) -> list[Detection]:
        if end_ms <= start_ms:
            raise DetectionError(f"empty window: start_ms={start_ms}, end_ms={end_ms}")
        if fps <= 0:
            raise DetectionError(f"fps must be positive, got {fps!r}")
        frame_ms = 1000.0 / fps
        n_frames = int((end_ms - start_ms) / frame_ms) + 1
        # TrackingWindow's grid: round the start once, then count consecutive
        # slots — per-frame rounding duplicates/skips frame numbers whenever a
        # timestamp lands on a half-integer slot (round-half-to-even).
        first_frame = round(start_ms / frame_ms)
        detections: list[Detection] = []
        for index in range(n_frames):
            ts_ms = start_ms + index * frame_ms
            frame_no = first_frame + index
            progress = index / max(n_frames - 1, 1)
            if self._rng.random() >= self.dropout:
                detections.append(self._ball(frame_no, ts_ms, progress))
            detections.extend(self._statics(frame_no, ts_ms))
        return detections

    def _ball(self, frame_no: int, ts_ms: float, progress: float) -> Detection:
        """Ball sweeps left→right; height is a pre-bounce descent then rebound."""
        cx = 0.1 + 0.8 * progress
        if progress <= self.bounce_at:
            cy = 0.3 + 0.5 * (progress / self.bounce_at)
        else:
            cy = 0.8 - 0.4 * ((progress - self.bounce_at) / (1.0 - self.bounce_at))
        return Detection(
            frame_no=frame_no,
            ts_ms=ts_ms,
            label=LabelClass.BALL,
            cx=cx,
            cy=cy,
            w=0.02,
            h=0.02,
            score=0.9,
        )

    def _statics(self, frame_no: int, ts_ms: float) -> list[Detection]:
        boxes = [
            Detection(frame_no, ts_ms, LabelClass.STUMPS, 0.92, 0.7, 0.04, 0.2, 0.95),
            Detection(frame_no, ts_ms, LabelClass.BAT, 0.85, 0.65, 0.05, 0.12, 0.85),
        ]
        boxes.extend(
            Detection(frame_no, ts_ms, LabelClass.BALL, 0.15 + 0.05 * decoy, 0.95, 0.02, 0.02, 0.8)
            for decoy in range(self.decoys)
        )
        return boxes
