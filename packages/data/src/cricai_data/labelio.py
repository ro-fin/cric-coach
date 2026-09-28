"""Label Studio JSON + YOLO-txt import/export for annotations (US-F1).

The labeling tool itself is not embedded (phase-4 plan decision): frames sampled
by ``cricai_worker.sample_frames`` are exported as Label Studio tasks
(``scripts/export_label_tasks.py``), labeled wherever the operator runs the
tool, and imported back (``POST /annotations/import``). Two invariants make the
round trip trustworthy:

1. **Lossless boxes.** Label Studio's native geometry is top-left percentages,
   and converting normalized ``cx/cy/w/h`` there and back loses float bits.
   Every exported result therefore carries the exact normalized box under
   ``meta.norm``; import prefers it whenever it still agrees with the
   percentage geometry (within :data:`NORM_AGREEMENT_EPS`) and falls back to
   the percentages when a human moved the box in the tool (the stale ``norm``
   then disagrees and loses). YOLO lines use ``repr`` floats, which Python
   round-trips exactly.
2. **Provenance never separates from pixels.** Every task embeds the frame's
   full provenance (``frame_id``/``session_id``/``ball_no``/``camera_id``/
   ``frame_no``/``ts_ms``/``object_key``) under ``data.cricai``; a file whose
   provenance was lost or mangled is rejected loudly, never guessed at.

Unknown classes and malformed files always raise :class:`LabelIOError` naming
the offending task/line — silent skips would corrupt training data invisibly.
Frame-level ``choices`` results (the labeling guide's hard-case tags: ``blur``,
``feed_exit``, ...) are not boxes: they ride along on
:attr:`ImportedFrame.tags` instead of being rejected.

A second task channel (US-I6) round-trips DELIVERY-level variation-intent
labels — one ``choices`` task per (session, ball) under the distinct
``data.cricai_delivery`` provenance key — see the "Delivery-label tasks"
section below. The two channels share the invariants but never mix.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cricai_data.enums import AnnotationSource, BowlingVariation, LabelClass
from cricai_data.models import Annotation, DeliveryLabel, FrameSample

#: Pinned YOLO class-index order (line ``0`` is always ball). Extending is
#: append-only: reordering would silently relabel every existing txt file.
YOLO_CLASS_ORDER: tuple[LabelClass, ...] = tuple(LabelClass)

#: Max |meta.norm - percent-derived| for the exact export box to win on import.
#: Float error of the percent round trip is ~1e-13; the smallest human edit in
#: the tool (one pixel on an 8K frame) moves the box by ~1e-4 normalized.
NORM_AGREEMENT_EPS = 1e-9

_NORM_FIELDS = ("cx", "cy", "w", "h")
_PROVENANCE_KEY = "cricai"


class LabelIOError(ValueError):
    """Malformed labeling file, unknown class, or invalid box — always loud."""


def yolo_class_names() -> list[str]:
    """Class names in pinned index order (``data.yaml`` / ``classes.txt``)."""
    return [label.value for label in YOLO_CLASS_ORDER]


@dataclass(frozen=True)
class NormBox:
    """One normalized YOLO-convention box: center x/y + width/height in [0, 1]."""

    label_class: LabelClass
    cx: float
    cy: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in _NORM_FIELDS:
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise LabelIOError(f"{name} must be finite and within [0, 1], got {value!r}")
        if self.w == 0.0 or self.h == 0.0:
            raise LabelIOError("box width/height must be > 0")
        for center, extent, axis in ((self.cx, self.w, "x"), (self.cy, self.h, "y")):
            if center - extent / 2 < -NORM_AGREEMENT_EPS or center + extent / 2 > (
                1.0 + NORM_AGREEMENT_EPS
            ):
                raise LabelIOError(f"box spills outside the frame on the {axis} axis")


@dataclass(frozen=True)
class FrameProvenance:
    """Identity of one sampled frame — travels with its labels everywhere."""

    frame_id: str
    session_id: str
    ball_no: int | None
    camera_id: str
    frame_no: int
    ts_ms: int
    object_key: str


@dataclass(frozen=True)
class ImportedBox:
    """One box coming back from the labeling tool, with annotator provenance."""

    box: NormBox
    annotator: str
    source: AnnotationSource


@dataclass(frozen=True)
class ImportedFrame:
    """One task's worth of labels re-attached to its frame provenance.

    ``tags`` are the frame-level hard-case markers the labeling guide mandates
    (``blur``, ``feed_exit``, ...), exported by Label Studio as ``choices``
    results; first-occurrence order, deduplicated.
    """

    provenance: FrameProvenance
    boxes: tuple[ImportedBox, ...]
    tags: tuple[str, ...] = ()


# --- YOLO txt -----------------------------------------------------------------


def to_yolo(annotations: Sequence[Annotation]) -> str:
    """Annotations of one frame -> YOLO txt (``class cx cy w h`` per line).

    Floats are written with ``repr`` so :func:`from_yolo` recovers them exactly;
    stored rows are re-validated on the way out so a corrupt box fails the
    export loudly instead of poisoning a training set.
    """
    lines = []
    for annotation in annotations:
        box = NormBox(
            annotation.label_class, annotation.cx, annotation.cy, annotation.w, annotation.h
        )
        index = YOLO_CLASS_ORDER.index(box.label_class)
        lines.append(f"{index} {box.cx!r} {box.cy!r} {box.w!r} {box.h!r}")
    return "".join(f"{line}\n" for line in lines)


def from_yolo(text: str) -> list[NormBox]:
    """Parse YOLO txt back into boxes; every malformed line is a loud error."""
    boxes: list[NormBox] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) != 5:
            raise LabelIOError(f"line {line_no}: expected 5 fields, got {len(tokens)}")
        try:
            index = int(tokens[0])
            values = [float(token) for token in tokens[1:]]
        except ValueError as exc:
            raise LabelIOError(f"line {line_no}: {exc}") from exc
        if not 0 <= index < len(YOLO_CLASS_ORDER):
            raise LabelIOError(f"line {line_no}: unknown class index {index}")
        try:
            boxes.append(NormBox(YOLO_CLASS_ORDER[index], *values))
        except LabelIOError as exc:
            raise LabelIOError(f"line {line_no}: {exc}") from exc
    return boxes


# --- Label Studio JSON ---------------------------------------------------------


def frame_provenance(frame: FrameSample) -> FrameProvenance:
    """Provenance record of a sampled frame (US-F1: label -> session/ball)."""
    return FrameProvenance(
        frame_id=str(frame.id),
        session_id=str(frame.session_id),
        ball_no=frame.ball_no,
        camera_id=frame.camera_id,
        frame_no=frame.frame_no,
        ts_ms=frame.ts_ms,
        object_key=frame.object_key,
    )


def to_label_studio_task(
    frame: FrameSample,
    annotations: Sequence[Annotation],
    *,
    image_url: str | None = None,
) -> dict[str, Any]:
    """One frame + its annotations -> a Label Studio task dict.

    ``image_url`` overrides the image reference (defaults to the object key —
    operators map it to their local-files serving root when importing).
    """
    provenance = frame_provenance(frame)
    results = []
    for annotation in annotations:
        box = NormBox(
            annotation.label_class, annotation.cx, annotation.cy, annotation.w, annotation.h
        )
        results.append(
            {
                "type": "rectanglelabels",
                "from_name": "label",
                "to_name": "image",
                "value": {
                    "x": (box.cx - box.w / 2) * 100.0,
                    "y": (box.cy - box.h / 2) * 100.0,
                    "width": box.w * 100.0,
                    "height": box.h * 100.0,
                    "rectanglelabels": [box.label_class.value],
                },
                "meta": {
                    "annotator": annotation.annotator,
                    "source": annotation.source.value,
                    "norm": {"cx": box.cx, "cy": box.cy, "w": box.w, "h": box.h},
                },
            }
        )
    return {
        "data": {
            "image": image_url if image_url is not None else provenance.object_key,
            _PROVENANCE_KEY: {
                "frame_id": provenance.frame_id,
                "session_id": provenance.session_id,
                "ball_no": provenance.ball_no,
                "camera_id": provenance.camera_id,
                "frame_no": provenance.frame_no,
                "ts_ms": provenance.ts_ms,
                "object_key": provenance.object_key,
            },
        },
        "annotations": [{"result": results}],
    }


def from_label_studio(payload: object) -> list[ImportedFrame]:
    """Parse a Label Studio export (list of tasks) back into typed frames.

    Accepts both our own exports (exact ``meta.norm`` boxes win) and the tool's
    edited output (percent geometry wins once it disagrees with a stale norm).
    Frame-level ``choices`` tags land on :attr:`ImportedFrame.tags`.
    """
    if not isinstance(payload, list):
        raise LabelIOError("Label Studio payload must be a list of tasks")
    frames: list[ImportedFrame] = []
    for task_no, task in enumerate(payload, start=1):
        where = f"task {task_no}"
        if not isinstance(task, dict):
            raise LabelIOError(f"{where}: not an object")
        boxes, tags = _labels_from(task.get("annotations"), where)
        frames.append(
            ImportedFrame(
                provenance=_provenance_from(task.get("data"), where),
                boxes=boxes,
                tags=tags,
            )
        )
    return frames


def _provenance_from(data: object, where: str) -> FrameProvenance:
    if not isinstance(data, dict):
        raise LabelIOError(f"{where}: missing data object")
    raw = data.get(_PROVENANCE_KEY)
    if not isinstance(raw, dict):
        raise LabelIOError(
            f"{where}: frame lost its provenance (no data.{_PROVENANCE_KEY}); refusing to guess"
        )
    ball_no = raw.get("ball_no")
    return FrameProvenance(
        frame_id=_req_str(raw.get("frame_id"), f"{where}: frame_id"),
        session_id=_req_str(raw.get("session_id"), f"{where}: session_id"),
        ball_no=None if ball_no is None else _req_int(ball_no, f"{where}: ball_no", minimum=1),
        camera_id=_req_str(raw.get("camera_id"), f"{where}: camera_id"),
        frame_no=_req_int(raw.get("frame_no"), f"{where}: frame_no"),
        ts_ms=_req_int(raw.get("ts_ms"), f"{where}: ts_ms"),
        object_key=_req_str(raw.get("object_key"), f"{where}: object_key"),
    )


def _labels_from(
    annotations: object, where: str
) -> tuple[tuple[ImportedBox, ...], tuple[str, ...]]:
    if not isinstance(annotations, list):
        raise LabelIOError(f"{where}: missing annotations list")
    boxes: list[ImportedBox] = []
    tags: list[str] = []
    for completion in annotations:
        if not isinstance(completion, dict) or not isinstance(completion.get("result"), list):
            raise LabelIOError(f"{where}: annotation without a result list")
        fallback = completion.get("completed_by")
        annotator = str(fallback) if fallback is not None else "label-studio"
        for item_no, item in enumerate(completion["result"], start=1):
            item_where = f"{where} result {item_no}"
            if not isinstance(item, dict):
                raise LabelIOError(f"{item_where}: not an object")
            if item.get("type") == "choices":  # frame-level hard-case tag, not a box
                tags.extend(_tags_from(item, item_where))
            else:
                boxes.append(_box_from(item, item_where, annotator))
    return tuple(boxes), tuple(dict.fromkeys(tags))


def _tags_from(item: dict[str, Any], where: str) -> list[str]:
    value = item.get("value")
    if not isinstance(value, dict):
        raise LabelIOError(f"{where}: missing value object")
    choices = value.get("choices")
    if not isinstance(choices, list) or not all(
        isinstance(choice, str) and choice for choice in choices
    ):
        raise LabelIOError(f"{where}: choices must be a list of non-empty tag strings")
    return list(choices)


def _box_from(item: dict[str, Any], where: str, fallback_annotator: str) -> ImportedBox:
    if item.get("type") != "rectanglelabels":
        raise LabelIOError(f"{where}: unsupported result type {item.get('type')!r}")
    value = item.get("value")
    if not isinstance(value, dict):
        raise LabelIOError(f"{where}: missing value object")
    raw_meta = item.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    box = _norm_box(_label_from(value, where), value, meta.get("norm"), where)
    raw_annotator = meta.get("annotator")
    return ImportedBox(
        box=box,
        annotator=raw_annotator
        if isinstance(raw_annotator, str) and raw_annotator
        else fallback_annotator,
        source=_source_from(meta.get("source"), where),
    )


def _label_from(value: dict[str, Any], where: str) -> LabelClass:
    labels = value.get("rectanglelabels")
    if not isinstance(labels, list) or len(labels) != 1:
        raise LabelIOError(f"{where}: expected exactly one rectangle label")
    try:
        return LabelClass(labels[0])
    except ValueError as exc:
        raise LabelIOError(f"{where}: unknown class {labels[0]!r}") from exc


def _norm_box(label: LabelClass, value: dict[str, Any], norm: object, where: str) -> NormBox:
    x = _req_number(value.get("x"), f"{where}: x")
    y = _req_number(value.get("y"), f"{where}: y")
    width = _req_number(value.get("width"), f"{where}: width")
    height = _req_number(value.get("height"), f"{where}: height")
    for name, low, extent in (("x", x, width), ("y", y, height)):
        if low < 0.0 or extent <= 0.0 or low + extent > 100.0 + NORM_AGREEMENT_EPS:
            raise LabelIOError(f"{where}: {name}/{name}-extent outside [0, 100]")
    # The bounds check above makes the derived box valid; the clamp only trims
    # the ~1e-11 float dust a boundary-hugging percent box can leave past 1.0.
    derived = NormBox(
        label,
        min(max((x + width / 2) / 100.0, 0.0), 1.0),
        min(max((y + height / 2) / 100.0, 0.0), 1.0),
        min(width / 100.0, 1.0),
        min(height / 100.0, 1.0),
    )
    exact = _exact_norm(label, norm)
    if exact is not None and all(
        abs(getattr(exact, name) - getattr(derived, name)) <= NORM_AGREEMENT_EPS
        for name in _NORM_FIELDS
    ):
        return exact  # our own export, untouched: recover the exact floats
    return derived


def _exact_norm(label: LabelClass, norm: object) -> NormBox | None:
    if not isinstance(norm, dict):
        return None
    values: list[float] = []
    for name in _NORM_FIELDS:
        value = norm.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None  # mangled meta: fall back to the tool's percent geometry
        values.append(float(value))
    try:
        return NormBox(label, *values)
    except LabelIOError:
        return None


def _source_from(raw: object, where: str) -> AnnotationSource:
    if raw is None:
        return AnnotationSource.IMPORTED  # tool-authored box round-tripping in
    if isinstance(raw, str):
        try:
            return AnnotationSource(raw)
        except ValueError as exc:
            raise LabelIOError(f"{where}: unknown annotation source {raw!r}") from exc
    raise LabelIOError(f"{where}: annotation source must be a string")


# --- Delivery-label tasks (US-I6) ----------------------------------------------
#
# A second, independent task channel next to the frame boxes: one task per
# DELIVERY (session + ball), carrying the bowler/coach variation intent as a
# Label Studio ``choices`` result. The two channels share the same invariants —
# provenance travels with the label, malformed files fail loudly — but use
# distinct provenance keys so a delivery task can never be mistaken for a frame
# task (either import rejects the other channel's files by name).

#: Provenance key of the delivery-label channel (``data.cricai_delivery``).
_DELIVERY_PROVENANCE_KEY = "cricai_delivery"

#: Allowed intent provenance values (mirrors ``delivery_labels.source``).
DELIVERY_LABEL_SOURCES: tuple[str, ...] = ("manual", "model")

_DELIVERY_FROM_NAME = "variation_intent"
_DELIVERY_TO_NAME = "delivery"


@dataclass(frozen=True)
class DeliveryProvenance:
    """Identity of one labeled delivery — travels with its label everywhere."""

    session_id: str
    ball_no: int


@dataclass(frozen=True)
class ImportedDelivery:
    """One delivery-label task coming back from the labeling tool (US-I6).

    ``variation_intent`` is the human ground truth being round-tripped.
    ``variation_detected`` rides along for review context only: importers must
    never write it back — model output is produced exclusively by the
    classifier worker job (contract #5: intent and detection never conflated).
    """

    provenance: DeliveryProvenance
    variation_intent: BowlingVariation
    labeler: str
    source: str
    variation_detected: BowlingVariation | None = None


def delivery_provenance(label: DeliveryLabel) -> DeliveryProvenance:
    """Provenance record of one delivery label (US-I6: label -> session/ball)."""
    return DeliveryProvenance(session_id=str(label.session_id), ball_no=label.ball_no)


def to_delivery_label_task(label: DeliveryLabel) -> dict[str, Any]:
    """One delivery label -> a Label Studio ``choices`` task dict (US-I6).

    The intent choice is the labeled value; labeler/source (and the current
    ``variation_detected``, informational only) ride in ``meta`` so the round
    trip is lossless.
    """
    provenance = delivery_provenance(label)
    detected = label.variation_detected
    return {
        "data": {
            _DELIVERY_PROVENANCE_KEY: {
                "session_id": provenance.session_id,
                "ball_no": provenance.ball_no,
            },
        },
        "annotations": [
            {
                "result": [
                    {
                        "type": "choices",
                        "from_name": _DELIVERY_FROM_NAME,
                        "to_name": _DELIVERY_TO_NAME,
                        "value": {"choices": [label.variation_intent.value]},
                        "meta": {
                            "labeler": label.labeler,
                            "source": label.source,
                            "variation_detected": None if detected is None else detected.value,
                        },
                    }
                ]
            }
        ],
    }


def deliveries_from_label_studio(payload: object) -> list[ImportedDelivery]:
    """Parse a delivery-label export (list of tasks) back into typed deliveries.

    Accepts our own exports and tool-edited output; every malformed task —
    including a frame-channel task (``data.cricai``) landing in this channel —
    is a loud :class:`LabelIOError`, never a guess (US-I6).
    """
    if not isinstance(payload, list):
        raise LabelIOError("delivery-label payload must be a list of tasks")
    deliveries: list[ImportedDelivery] = []
    for task_no, task in enumerate(payload, start=1):
        where = f"task {task_no}"
        if not isinstance(task, dict):
            raise LabelIOError(f"{where}: not an object")
        deliveries.append(_delivery_from_task(task, where))
    return deliveries


def _delivery_provenance_from(data: object, where: str) -> DeliveryProvenance:
    if not isinstance(data, dict):
        raise LabelIOError(f"{where}: missing data object")
    raw = data.get(_DELIVERY_PROVENANCE_KEY)
    if not isinstance(raw, dict):
        raise LabelIOError(
            f"{where}: delivery lost its provenance (no data.{_DELIVERY_PROVENANCE_KEY}); "
            "refusing to guess"
        )
    return DeliveryProvenance(
        session_id=_req_str(raw.get("session_id"), f"{where}: session_id"),
        ball_no=_req_int(raw.get("ball_no"), f"{where}: ball_no", minimum=1),
    )


def _delivery_choice_result(task: dict[str, Any], where: str) -> tuple[dict[str, Any], object]:
    """The task's single intent ``choices`` result and its annotator fallback."""
    annotations = task.get("annotations")
    if not isinstance(annotations, list) or len(annotations) != 1:
        raise LabelIOError(f"{where}: expected exactly one annotation")
    completion = annotations[0]
    if not isinstance(completion, dict) or not isinstance(completion.get("result"), list):
        raise LabelIOError(f"{where}: annotation without a result list")
    results = completion["result"]
    if len(results) != 1 or not isinstance(results[0], dict):
        raise LabelIOError(f"{where}: expected exactly one result item")
    item: dict[str, Any] = results[0]
    if item.get("type") != "choices" or item.get("from_name") != _DELIVERY_FROM_NAME:
        raise LabelIOError(f"{where}: expected one {_DELIVERY_FROM_NAME} choices result")
    return item, completion.get("completed_by")


def _delivery_from_task(task: dict[str, Any], where: str) -> ImportedDelivery:
    provenance = _delivery_provenance_from(task.get("data"), where)
    item, fallback = _delivery_choice_result(task, where)
    choices = _tags_from(item, where)
    if len(choices) != 1:
        raise LabelIOError(f"{where}: expected exactly one variation choice, got {len(choices)}")
    intent = _variation_from(choices[0], f"{where}: variation_intent")
    raw_meta = item.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    raw_labeler = meta.get("labeler")
    if isinstance(raw_labeler, str) and raw_labeler:
        labeler = raw_labeler
    else:
        labeler = str(fallback) if fallback is not None else "label-studio"
    detected = meta.get("variation_detected")
    return ImportedDelivery(
        provenance=provenance,
        variation_intent=intent,
        labeler=labeler,
        source=_delivery_source_from(meta.get("source"), where),
        variation_detected=(
            None if detected is None else _variation_from(detected, f"{where}: variation_detected")
        ),
    )


def _variation_from(raw: object, where: str) -> BowlingVariation:
    if not isinstance(raw, str):
        raise LabelIOError(f"{where}: variation must be a string, got {raw!r}")
    try:
        return BowlingVariation(raw)
    except ValueError as exc:
        raise LabelIOError(f"{where}: unknown variation {raw!r}") from exc


def _delivery_source_from(raw: object, where: str) -> str:
    if raw is None:
        return "manual"  # tool-authored intent round-tripping in: a human labeled it
    if isinstance(raw, str) and raw in DELIVERY_LABEL_SOURCES:
        return raw
    raise LabelIOError(f"{where}: unknown delivery label source {raw!r}")


def _req_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise LabelIOError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _req_int(value: object, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LabelIOError(f"{where}: expected an integer >= {minimum}, got {value!r}")
    return value


def _req_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise LabelIOError(f"{where}: expected a finite number, got {value!r}")
    return float(value)
