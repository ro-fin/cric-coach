"""US-I6 trainer generalization: parameterized key sets, detector defaults intact.

`build_eval_report` gained ``headline_keys`` / ``class_fraction_keys`` /
``extra_report_keys`` so a classifier model type registers its own metrics
(``variation_agreement_3way``, ``confusion_matrix``). The load-bearing property
is byte-identical detector output for callers passing nothing.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from cricai_vision.train import (
    CLASS_ROW_FRACTION_KEYS,
    CLASS_ROW_KEYS,
    HEADLINE_METRIC_KEYS,
    REPORT_SCHEMA_VERSION,
    RunResult,
    TrainingError,
    build_eval_report,
)

CLASSIFIER_HEADLINE = {"variation_agreement_3way": 0.84, "unclear_rate": 0.1}

CLASSIFIER_ROW: dict[str, Any] = {
    "label": "leg_break",
    "precision": 0.9,
    "recall": 0.82,
    "support": 41,
}

CONFUSION = {"leg_break": {"leg_break": 34, "top_spinner": 3, "googly": 2, "unknown": 2}}


def _classifier_kwargs(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "per_class": overrides.pop("per_class", [dict(CLASSIFIER_ROW)]),
        "confusion_matrix": CONFUSION,
    }
    report.update(overrides.pop("report_extras", {}))
    kwargs: dict[str, Any] = {
        "model_name": "legspin-variation",
        "dataset_version": "deliveries-v1",
        "trainer_version": "fake-variation-1",
        "config": {"big_turn_cm": 8.0},
        "result": RunResult(
            metrics=overrides.pop("headline", dict(CLASSIFIER_HEADLINE)),
            weights_path=Path("unused.pt"),
            report=report,
        ),
        "headline_keys": ("variation_agreement_3way", "unclear_rate"),
        "class_fraction_keys": ("precision", "recall"),
        "extra_report_keys": ("confusion_matrix",),
    }
    kwargs.update(overrides)
    return kwargs


def test_pinned_detector_key_sets_are_unchanged() -> None:
    assert HEADLINE_METRIC_KEYS == (
        "map50_ball",
        "map50_bat",
        "map50_stumps",
        "recall_ball_high_blur",
    )
    assert CLASS_ROW_FRACTION_KEYS == ("map50", "precision", "recall")
    assert sorted(CLASS_ROW_KEYS) == ["label", "map50", "precision", "recall", "support"]


def test_detector_defaults_produce_the_exact_prior_artifact() -> None:
    """Calls passing no key-set kwargs stay byte-identical (US-I6 constraint)."""
    headline = {
        "map50_ball": 0.91,
        "map50_bat": 0.86,
        "map50_stumps": 0.97,
        "recall_ball_high_blur": 0.8,
    }
    row = {"label": "ball", "map50": 0.91, "precision": 0.9, "recall": 0.88, "support": 120}
    report = build_eval_report(
        model_name="ball-detector",
        dataset_version="v1",
        trainer_version="fake-trainer-1",
        config={"imgsz": 640},
        result=RunResult(
            metrics=headline,
            weights_path=Path("w.pt"),
            report={"per_class": [dict(row)], "dataset_digest": "abc"},
        ),
    )
    expected = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "model_name": "ball-detector",
        "dataset_version": "v1",
        "dataset_digest": "abc",
        "trainer_version": "fake-trainer-1",
        "config": {"imgsz": 640},
        "headline": headline,
        "per_class": [row],
    }
    assert json.dumps(report, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert list(report) == list(expected)  # key order pinned too
    assert list(report["per_class"][0]) == ["label", "map50", "precision", "recall", "support"]


def test_classifier_artifact_carries_custom_keys_and_extras() -> None:
    report = build_eval_report(**_classifier_kwargs())
    assert report["headline"] == CLASSIFIER_HEADLINE
    assert report["per_class"] == [CLASSIFIER_ROW]
    assert list(report["per_class"][0]) == ["label", "precision", "recall", "support"]
    assert report["confusion_matrix"] == CONFUSION
    assert report["dataset_digest"] is None


def test_custom_headline_keys_are_enforced() -> None:
    with pytest.raises(TrainingError, match=r"missing pinned keys.*variation_agreement_3way"):
        build_eval_report(**_classifier_kwargs(headline={"unclear_rate": 0.1}))


def test_custom_class_rows_reject_detector_shape() -> None:
    row = {"label": "leg_break", "map50": 0.9, "precision": 0.9, "recall": 0.9, "support": 3}
    with pytest.raises(TrainingError, match=r"unknown keys \['map50'\]"):
        build_eval_report(**_classifier_kwargs(per_class=[row]))


def test_custom_class_rows_reject_missing_fraction_key() -> None:
    row = {"label": "leg_break", "precision": 0.9, "support": 3}
    with pytest.raises(TrainingError, match=r"missing keys \['recall'\]"):
        build_eval_report(**_classifier_kwargs(per_class=[row]))


def test_missing_extra_report_key_fails_loudly() -> None:
    kwargs = _classifier_kwargs(extra_report_keys=("confusion_matrix", "calibration_curve"))
    with pytest.raises(TrainingError, match="missing extra key 'calibration_curve'"):
        build_eval_report(**kwargs)


def test_extra_key_colliding_with_pinned_artifact_key_fails() -> None:
    kwargs = _classifier_kwargs(
        extra_report_keys=("headline",), report_extras={"headline": {"x": 1}}
    )
    with pytest.raises(TrainingError, match="collides with a pinned key"):
        build_eval_report(**kwargs)
