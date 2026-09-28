"""US-I6 delivery-label task channel: lossless round trip, loud malformed files.

The channel mirrors the frame-box invariants: provenance travels with every
label, malformed/mixed-channel files raise :class:`LabelIOError` naming the
task, and intent (human ground truth) never conflates with detection.
"""

import json
import uuid
from typing import Any

import pytest
from cricai_data.enums import BowlingVariation
from cricai_data.labelio import (
    DELIVERY_LABEL_SOURCES,
    DeliveryProvenance,
    ImportedDelivery,
    LabelIOError,
    deliveries_from_label_studio,
    delivery_provenance,
    from_label_studio,
    to_delivery_label_task,
)
from cricai_data.models import DeliveryLabel

SESSION_ID = uuid.uuid4()


def _label(
    *,
    ball_no: int = 4,
    intent: BowlingVariation = BowlingVariation.LEG_BREAK,
    detected: BowlingVariation | None = BowlingVariation.GOOGLY,
    labeler: str = "coach",
    source: str = "manual",
) -> DeliveryLabel:
    return DeliveryLabel(
        id=uuid.uuid4(),
        session_id=SESSION_ID,
        ball_no=ball_no,
        variation_intent=intent,
        variation_detected=detected,
        labeler=labeler,
        source=source,
    )


def _task(**overrides: Any) -> dict[str, Any]:
    task = to_delivery_label_task(_label())
    task.update(overrides)
    return task


def _result_item(task: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = task["annotations"][0]["result"][0]
    return item


# --- export shape ---------------------------------------------------------------


def test_task_carries_provenance_choice_and_meta() -> None:
    task = to_delivery_label_task(_label())
    assert task["data"]["cricai_delivery"] == {"session_id": str(SESSION_ID), "ball_no": 4}
    item = _result_item(task)
    assert item["type"] == "choices"
    assert item["from_name"] == "variation_intent"
    assert item["to_name"] == "delivery"
    assert item["value"]["choices"] == ["leg_break"]
    assert item["meta"] == {
        "labeler": "coach",
        "source": "manual",
        "variation_detected": "googly",
    }


def test_task_serializes_null_detection() -> None:
    task = to_delivery_label_task(_label(detected=None))
    assert _result_item(task)["meta"]["variation_detected"] is None


def test_delivery_provenance_function() -> None:
    assert delivery_provenance(_label(ball_no=9)) == DeliveryProvenance(
        session_id=str(SESSION_ID), ball_no=9
    )


def test_delivery_label_sources_are_pinned() -> None:
    assert DELIVERY_LABEL_SOURCES == ("manual", "model")


# --- round trip -------------------------------------------------------------------


def test_round_trip_is_lossless_through_json() -> None:
    labels = [
        _label(ball_no=1, intent=BowlingVariation.LEG_BREAK, detected=BowlingVariation.LEG_BREAK),
        _label(ball_no=2, intent=BowlingVariation.GOOGLY, detected=None, labeler="parent"),
        _label(ball_no=3, intent=BowlingVariation.SLIDER, detected=None, source="model"),
    ]
    payload = json.loads(json.dumps([to_delivery_label_task(label) for label in labels]))
    deliveries = deliveries_from_label_studio(payload)
    assert deliveries == [
        ImportedDelivery(
            provenance=DeliveryProvenance(str(SESSION_ID), 1),
            variation_intent=BowlingVariation.LEG_BREAK,
            labeler="coach",
            source="manual",
            variation_detected=BowlingVariation.LEG_BREAK,
        ),
        ImportedDelivery(
            provenance=DeliveryProvenance(str(SESSION_ID), 2),
            variation_intent=BowlingVariation.GOOGLY,
            labeler="parent",
            source="manual",
            variation_detected=None,
        ),
        ImportedDelivery(
            provenance=DeliveryProvenance(str(SESSION_ID), 3),
            variation_intent=BowlingVariation.SLIDER,
            labeler="coach",
            source="model",
            variation_detected=None,
        ),
    ]


def test_channels_reject_each_other_loudly() -> None:
    delivery_task = to_delivery_label_task(_label())
    with pytest.raises(LabelIOError, match=r"no data\.cricai\)"):
        from_label_studio([delivery_task])
    frame_task = {"data": {"cricai": {}}, "annotations": [{"result": []}]}
    with pytest.raises(LabelIOError, match=r"no data\.cricai_delivery"):
        deliveries_from_label_studio([frame_task])


# --- tool-edited output fallbacks ---------------------------------------------------


def test_labeler_falls_back_to_completed_by_then_tool_default() -> None:
    task = _task()
    item = _result_item(task)
    del item["meta"]["labeler"]
    task["annotations"][0]["completed_by"] = 7
    assert deliveries_from_label_studio([task])[0].labeler == "7"
    del task["annotations"][0]["completed_by"]
    assert deliveries_from_label_studio([task])[0].labeler == "label-studio"


def test_empty_labeler_string_falls_back() -> None:
    task = _task()
    _result_item(task)["meta"]["labeler"] = ""
    assert deliveries_from_label_studio([task])[0].labeler == "label-studio"


def test_missing_meta_defaults_to_manual_tool_annotator() -> None:
    task = _task()
    del _result_item(task)["meta"]
    delivery = deliveries_from_label_studio([task])[0]
    assert delivery.source == "manual"
    assert delivery.labeler == "label-studio"
    assert delivery.variation_detected is None


def test_model_source_round_trips() -> None:
    task = _task()
    _result_item(task)["meta"]["source"] = "model"
    assert deliveries_from_label_studio([task])[0].source == "model"


# --- loud errors --------------------------------------------------------------------


def test_payload_must_be_a_list() -> None:
    with pytest.raises(LabelIOError, match="must be a list"):
        deliveries_from_label_studio({"data": {}})


def test_task_must_be_an_object() -> None:
    with pytest.raises(LabelIOError, match="task 1: not an object"):
        deliveries_from_label_studio(["nope"])


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda task: task.pop("data"), "missing data object"),
        (lambda task: task["data"].pop("cricai_delivery"), "lost its provenance"),
        (
            lambda task: task["data"]["cricai_delivery"].pop("session_id"),
            "session_id: expected a non-empty string",
        ),
        (
            lambda task: task["data"]["cricai_delivery"].update(ball_no=0),
            "ball_no: expected an integer >= 1",
        ),
        (lambda task: task.pop("annotations"), "exactly one annotation"),
        (lambda task: task.update(annotations=[]), "exactly one annotation"),
        (lambda task: task.update(annotations=[{}]), "annotation without a result list"),
        (lambda task: task.update(annotations=["x"]), "annotation without a result list"),
        (
            lambda task: task["annotations"][0].update(result=[]),
            "exactly one result item",
        ),
        (
            lambda task: task["annotations"][0].update(result=["x"]),
            "exactly one result item",
        ),
        (
            lambda task: _result_item(task).update(type="rectanglelabels"),
            "expected one variation_intent choices result",
        ),
        (
            lambda task: _result_item(task).update(from_name="other"),
            "expected one variation_intent choices result",
        ),
        (lambda task: _result_item(task).pop("value"), "missing value object"),
        (
            lambda task: _result_item(task)["value"].update(choices="leg_break"),
            "choices must be a list",
        ),
        (
            lambda task: _result_item(task)["value"].update(choices=["leg_break", "googly"]),
            "exactly one variation choice, got 2",
        ),
        (
            lambda task: _result_item(task)["value"].update(choices=["off_break"]),
            "unknown variation 'off_break'",
        ),
        (
            lambda task: _result_item(task)["meta"].update(variation_detected=3),
            "variation_detected: variation must be a string",
        ),
        (
            lambda task: _result_item(task)["meta"].update(variation_detected="doosra"),
            "unknown variation 'doosra'",
        ),
        (
            lambda task: _result_item(task)["meta"].update(source="guessed"),
            "unknown delivery label source",
        ),
        (
            lambda task: _result_item(task)["meta"].update(source=1),
            "unknown delivery label source",
        ),
    ],
)
def test_malformed_tasks_fail_loudly(mutate: Any, match: str) -> None:
    task = _task()
    mutate(task)
    with pytest.raises(LabelIOError, match=match):
        deliveries_from_label_studio([task])
