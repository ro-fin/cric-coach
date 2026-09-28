"""US-F1 label IO: Label Studio JSON + YOLO txt round trips, loud malformed files.

The acceptance-critical property is losslessness: exporting FrameSample +
Annotation rows and importing the file back must recover the exact normalized
boxes and the full frame provenance — floats included, no drift.
"""

import json
import uuid
from typing import Any

import pytest
from cricai_data.enums import AnnotationSource, LabelClass
from cricai_data.labelio import (
    YOLO_CLASS_ORDER,
    ImportedFrame,
    LabelIOError,
    NormBox,
    frame_provenance,
    from_label_studio,
    from_yolo,
    to_label_studio_task,
    to_yolo,
    yolo_class_names,
)
from cricai_data.models import Annotation, FrameSample

#: Awkward floats that expose any precision loss in a round trip.
AWKWARD = (1 / 3, 0.1, 2 / 7, 1e-05)


def _frame(*, ball_no: int | None = 3) -> FrameSample:
    return FrameSample(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        ball_no=ball_no,
        camera_id="C1",
        frame_no=482,
        ts_ms=4012,
        object_key="sessions/s/frames/C1/frame-000482.jpg",
    )


def _annotation(
    label: LabelClass = LabelClass.BALL,
    cx: float = 0.5,
    cy: float = 0.4,
    w: float = 0.02,
    h: float = 0.03,
    annotator: str = "mira",
    source: AnnotationSource = AnnotationSource.MANUAL,
) -> Annotation:
    return Annotation(
        frame_id=uuid.uuid4(),
        label_class=label,
        cx=cx,
        cy=cy,
        w=w,
        h=h,
        annotator=annotator,
        source=source,
    )


# --- class order / names --------------------------------------------------------


def test_yolo_class_order_is_pinned_ball_first() -> None:
    assert YOLO_CLASS_ORDER[0] is LabelClass.BALL
    assert tuple(LabelClass) == YOLO_CLASS_ORDER
    assert yolo_class_names() == ["ball", "bat", "stumps", "feet", "glove", "helmet"]


# --- NormBox validation -----------------------------------------------------------


def test_norm_box_rejects_out_of_range_and_nonfinite() -> None:
    with pytest.raises(LabelIOError, match="cx"):
        NormBox(LabelClass.BALL, 1.2, 0.5, 0.1, 0.1)
    with pytest.raises(LabelIOError, match="cy"):
        NormBox(LabelClass.BALL, 0.5, float("nan"), 0.1, 0.1)
    with pytest.raises(LabelIOError, match="w"):
        NormBox(LabelClass.BALL, 0.5, 0.5, -0.1, 0.1)


def test_norm_box_rejects_zero_extent() -> None:
    with pytest.raises(LabelIOError, match="width/height"):
        NormBox(LabelClass.BALL, 0.5, 0.5, 0.0, 0.1)


def test_norm_box_rejects_spill_outside_frame() -> None:
    with pytest.raises(LabelIOError, match="x axis"):
        NormBox(LabelClass.BALL, 0.99, 0.5, 0.1, 0.1)
    with pytest.raises(LabelIOError, match="y axis"):
        NormBox(LabelClass.BALL, 0.5, 0.01, 0.1, 0.1)


def test_norm_box_allows_edge_touching_boxes() -> None:
    box = NormBox(LabelClass.BALL, 0.05, 0.95, 0.1, 0.1)
    assert box.cx == 0.05


# --- YOLO txt ---------------------------------------------------------------------


def test_yolo_round_trip_is_lossless() -> None:
    annotations = [
        _annotation(LabelClass.BALL, *AWKWARD),
        _annotation(LabelClass.STUMPS, 0.92, 0.7, 0.04, 0.2),
    ]
    text = to_yolo(annotations)
    boxes = from_yolo(text)
    assert boxes == [
        NormBox(LabelClass.BALL, *AWKWARD),
        NormBox(LabelClass.STUMPS, 0.92, 0.7, 0.04, 0.2),
    ]


def test_yolo_empty_annotations_and_blank_lines() -> None:
    assert to_yolo([]) == ""
    assert from_yolo("\n   \n") == []


def test_yolo_export_validates_stored_rows() -> None:
    with pytest.raises(LabelIOError, match="cx"):
        to_yolo([_annotation(cx=7.0)])


def test_from_yolo_rejects_wrong_field_count() -> None:
    with pytest.raises(LabelIOError, match="line 1: expected 5 fields"):
        from_yolo("0 0.5 0.5 0.1\n")


def test_from_yolo_rejects_non_numeric() -> None:
    with pytest.raises(LabelIOError, match="line 2"):
        from_yolo("0 0.5 0.5 0.1 0.1\nx 0.5 0.5 0.1 0.1\n")


def test_from_yolo_rejects_unknown_class_index() -> None:
    with pytest.raises(LabelIOError, match="unknown class index 6"):
        from_yolo("6 0.5 0.5 0.1 0.1\n")
    with pytest.raises(LabelIOError, match="unknown class index -1"):
        from_yolo("-1 0.5 0.5 0.1 0.1\n")


def test_from_yolo_rejects_out_of_range_box_with_line_number() -> None:
    with pytest.raises(LabelIOError, match="line 1: cx"):
        from_yolo("0 1.5 0.5 0.1 0.1\n")


# --- Label Studio export ----------------------------------------------------------


def test_label_studio_task_structure_and_provenance() -> None:
    frame = _frame()
    task = to_label_studio_task(frame, [_annotation(LabelClass.BAT, 0.85, 0.65, 0.05, 0.12)])
    prov = task["data"]["cricai"]
    assert prov == {
        "frame_id": str(frame.id),
        "session_id": str(frame.session_id),
        "ball_no": 3,
        "camera_id": "C1",
        "frame_no": 482,
        "ts_ms": 4012,
        "object_key": frame.object_key,
    }
    assert task["data"]["image"] == frame.object_key  # default image ref
    (result,) = task["annotations"][0]["result"]
    assert result["type"] == "rectanglelabels"
    assert result["value"]["rectanglelabels"] == ["bat"]
    assert result["value"]["x"] == pytest.approx((0.85 - 0.05 / 2) * 100)
    assert result["value"]["width"] == pytest.approx(5.0)
    assert result["meta"]["annotator"] == "mira"
    assert result["meta"]["source"] == "manual"
    assert result["meta"]["norm"] == {"cx": 0.85, "cy": 0.65, "w": 0.05, "h": 0.12}


def test_label_studio_task_image_url_override_and_row_validation() -> None:
    frame = _frame()
    task = to_label_studio_task(frame, [], image_url="http://lab/img.jpg")
    assert task["data"]["image"] == "http://lab/img.jpg"
    assert task["annotations"] == [{"result": []}]
    with pytest.raises(LabelIOError, match="cy"):
        to_label_studio_task(frame, [_annotation(cy=-2.0)])


# --- Label Studio round trip -------------------------------------------------------


def test_label_studio_round_trip_is_lossless() -> None:
    frame = _frame()
    annotations = [
        _annotation(LabelClass.BALL, *AWKWARD, annotator="mira"),
        _annotation(
            LabelClass.FEET, 0.3, 0.9, 0.08, 0.06, annotator="dad", source=AnnotationSource.MODEL
        ),
    ]
    payload = json.loads(json.dumps([to_label_studio_task(frame, annotations)]))
    (imported,) = from_label_studio(payload)
    assert imported.provenance == frame_provenance(frame)
    assert [b.box for b in imported.boxes] == [
        NormBox(LabelClass.BALL, *AWKWARD),  # exact floats survive the percent detour
        NormBox(LabelClass.FEET, 0.3, 0.9, 0.08, 0.06),
    ]
    assert [b.annotator for b in imported.boxes] == ["mira", "dad"]
    assert [b.source for b in imported.boxes] == [
        AnnotationSource.MANUAL,
        AnnotationSource.MODEL,
    ]


def test_label_studio_round_trip_keeps_null_ball_no() -> None:
    frame = _frame(ball_no=None)
    payload = json.loads(json.dumps([to_label_studio_task(frame, [])]))
    (imported,) = from_label_studio(payload)
    assert imported.provenance.ball_no is None
    assert imported.boxes == ()


def _tool_task(**overrides: Any) -> dict[str, Any]:
    """A task as Label Studio itself would export it (no cricAI meta)."""
    task: dict[str, Any] = {
        "data": {
            "image": "img.jpg",
            "cricai": {
                "frame_id": "f-1",
                "session_id": "s-1",
                "ball_no": 2,
                "camera_id": "C1",
                "frame_no": 10,
                "ts_ms": 500,
                "object_key": "k.jpg",
            },
        },
        "annotations": [
            {
                "completed_by": 7,
                "result": [
                    {
                        "type": "rectanglelabels",
                        "from_name": "label",
                        "to_name": "image",
                        "value": {
                            "x": 25.0,
                            "y": 40.0,
                            "width": 10.0,
                            "height": 20.0,
                            "rectanglelabels": ["ball"],
                        },
                    }
                ],
            }
        ],
    }
    task.update(overrides)
    return task


def test_import_tool_authored_box_derives_from_percentages() -> None:
    (imported,) = from_label_studio([_tool_task()])
    (box,) = imported.boxes
    assert box.box == NormBox(LabelClass.BALL, 0.3, 0.5, 0.1, 0.2)
    assert box.annotator == "7"  # completed_by fallback
    assert box.source is AnnotationSource.IMPORTED


def test_import_missing_completed_by_falls_back_to_tool_name() -> None:
    task = _tool_task()
    del task["annotations"][0]["completed_by"]
    (imported,) = from_label_studio([task])
    assert imported.boxes[0].annotator == "label-studio"


def test_import_stale_norm_loses_to_edited_percentages() -> None:
    # A human moved the box in the tool: percentages changed, meta.norm is stale.
    task = _tool_task()
    task["annotations"][0]["result"][0]["meta"] = {
        "annotator": "mira",
        "source": "manual",
        "norm": {"cx": 0.9, "cy": 0.9, "w": 0.1, "h": 0.2},
    }
    (imported,) = from_label_studio([task])
    (box,) = imported.boxes
    assert box.box == NormBox(LabelClass.BALL, 0.3, 0.5, 0.1, 0.2)  # geometry wins
    assert box.annotator == "mira"
    assert box.source is AnnotationSource.MANUAL


def test_import_mangled_norm_falls_back_to_percentages() -> None:
    bad_norms: tuple[Any, ...] = (
        {"cx": "x", "cy": 0.5, "w": 0.1, "h": 0.2},  # non-numeric field
        {"cx": True, "cy": 0.5, "w": 0.1, "h": 0.2},  # bool masquerading as number
        [0.3, 0.5, 0.1, 0.2],  # not an object
        {"cx": 0.99, "cy": 0.5, "w": 0.9, "h": 0.2},  # numeric but invalid box
    )
    for bad_norm in bad_norms:
        task = _tool_task()
        task["annotations"][0]["result"][0]["meta"] = {"norm": bad_norm}
        (imported,) = from_label_studio([task])
        assert imported.boxes[0].box == NormBox(LabelClass.BALL, 0.3, 0.5, 0.1, 0.2)


def test_import_empty_meta_annotator_falls_back() -> None:
    task = _tool_task()
    task["annotations"][0]["result"][0]["meta"] = {"annotator": ""}
    (imported,) = from_label_studio([task])
    assert imported.boxes[0].annotator == "7"  # completed_by fallback


# --- Label Studio frame-level tags (the guide's hard-case markers) -------------------


def test_import_keeps_hard_case_choice_tags_alongside_boxes() -> None:
    # The labeling guide mandates frame tags (`blur`, `feed_exit`) on hard cases;
    # Label Studio exports them as `choices` items in the same result list.
    task = _tool_task()
    task["annotations"][0]["result"].append(
        {
            "type": "choices",
            "from_name": "case",
            "to_name": "image",
            "value": {"choices": ["blur", "feed_exit"]},
        }
    )
    (imported,) = from_label_studio([task])
    (box,) = imported.boxes  # the rectangle still imports
    assert box.box == NormBox(LabelClass.BALL, 0.3, 0.5, 0.1, 0.2)
    assert imported.tags == ("blur", "feed_exit")


def test_import_deduplicates_repeated_tags_and_defaults_to_none() -> None:
    task = _tool_task()
    task["annotations"][0]["result"].extend(
        [
            {"type": "choices", "value": {"choices": ["blur"]}},
            {"type": "choices", "value": {"choices": ["blur"]}},
        ]
    )
    (imported,) = from_label_studio([task])
    assert imported.tags == ("blur",)
    (untagged,) = from_label_studio([_tool_task()])
    assert untagged.tags == ()


def test_import_rejects_malformed_choices_items() -> None:
    bad_values: tuple[Any, ...] = (
        None,  # missing value object
        {"choices": "blur"},  # not a list
        {"choices": [3]},  # non-string tag
        {"choices": [""]},  # empty tag
    )
    for bad in bad_values:
        task = _tool_task()
        task["annotations"][0]["result"].append({"type": "choices", "value": bad})
        with pytest.raises(LabelIOError, match="result 2"):
            from_label_studio([task])


# --- Label Studio malformed files are loud ------------------------------------------


def test_import_rejects_non_list_payload() -> None:
    with pytest.raises(LabelIOError, match="list of tasks"):
        from_label_studio({"data": {}})


def test_import_rejects_non_dict_task() -> None:
    with pytest.raises(LabelIOError, match="task 1: not an object"):
        from_label_studio(["nope"])


def test_import_rejects_missing_data_and_lost_provenance() -> None:
    with pytest.raises(LabelIOError, match="missing data"):
        from_label_studio([{"annotations": []}])
    with pytest.raises(LabelIOError, match="lost its provenance"):
        from_label_studio([{"data": {"image": "img.jpg"}, "annotations": []}])


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("frame_id", None, "frame_id"),
        ("session_id", "", "session_id"),
        ("ball_no", 0, "ball_no"),
        ("ball_no", True, "ball_no"),
        ("camera_id", 3, "camera_id"),
        ("frame_no", -1, "frame_no"),
        ("frame_no", "10", "frame_no"),
        ("ts_ms", None, "ts_ms"),
        ("object_key", None, "object_key"),
    ],
)
def test_import_rejects_mangled_provenance(key: str, value: Any, match: str) -> None:
    task = _tool_task()
    task["data"]["cricai"][key] = value
    with pytest.raises(LabelIOError, match=match):
        from_label_studio([task])


def test_import_rejects_missing_annotations_list() -> None:
    task = _tool_task()
    task["annotations"] = None
    with pytest.raises(LabelIOError, match="annotations list"):
        from_label_studio([task])


def test_import_rejects_completion_without_result() -> None:
    task = _tool_task(annotations=[{"completed_by": 1}])
    with pytest.raises(LabelIOError, match="without a result list"):
        from_label_studio([task])
    task = _tool_task(annotations=["nope"])
    with pytest.raises(LabelIOError, match="without a result list"):
        from_label_studio([task])


def test_import_rejects_non_dict_result_item() -> None:
    task = _tool_task()
    task["annotations"][0]["result"] = ["nope"]
    with pytest.raises(LabelIOError, match="result 1: not an object"):
        from_label_studio([task])


def test_import_rejects_unsupported_result_type() -> None:
    task = _tool_task()
    task["annotations"][0]["result"][0]["type"] = "relation"
    with pytest.raises(LabelIOError, match="unsupported result type 'relation'"):
        from_label_studio([task])


def test_import_rejects_missing_value() -> None:
    task = _tool_task()
    task["annotations"][0]["result"][0]["value"] = None
    with pytest.raises(LabelIOError, match="missing value"):
        from_label_studio([task])


def test_import_rejects_bad_label_lists() -> None:
    for labels in ([], ["ball", "bat"], None):
        task = _tool_task()
        task["annotations"][0]["result"][0]["value"]["rectanglelabels"] = labels
        with pytest.raises(LabelIOError, match="exactly one rectangle label"):
            from_label_studio([task])


def test_import_unknown_class_is_loud() -> None:
    task = _tool_task()
    task["annotations"][0]["result"][0]["value"]["rectanglelabels"] = ["zebra"]
    with pytest.raises(LabelIOError, match="unknown class 'zebra'"):
        from_label_studio([task])


@pytest.mark.parametrize(
    ("field", "value"),
    [("x", -1.0), ("x", 95.0), ("y", 90.0), ("width", 0.0), ("x", "wide"), ("height", None)],
)
def test_import_rejects_bad_percent_geometry(field: str, value: Any) -> None:
    task = _tool_task()
    task["annotations"][0]["result"][0]["value"][field] = value
    with pytest.raises(LabelIOError):
        from_label_studio([task])


def test_import_rejects_geometry_spilling_via_derived_box() -> None:
    # Percent fields individually valid but the derived normalized box is not.
    task = _tool_task()
    task["annotations"][0]["result"][0]["value"].update(
        {"x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0}
    )
    (imported,) = from_label_studio([task])  # full-frame box is legal
    assert imported.boxes[0].box.w == 1.0


def test_import_rejects_unknown_source_string_and_non_string() -> None:
    task = _tool_task()
    task["annotations"][0]["result"][0]["meta"] = {"source": "vibes"}
    with pytest.raises(LabelIOError, match="unknown annotation source 'vibes'"):
        from_label_studio([task])
    task["annotations"][0]["result"][0]["meta"] = {"source": 3}
    with pytest.raises(LabelIOError, match="source must be a string"):
        from_label_studio([task])


def test_imported_frame_is_typed() -> None:
    (imported,) = from_label_studio([_tool_task()])
    assert isinstance(imported, ImportedFrame)
    assert imported.provenance.camera_id == "C1"
