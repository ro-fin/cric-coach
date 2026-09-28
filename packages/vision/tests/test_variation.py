"""US-I6 variation spine: features, deterministic fake classifier, honest eval.

Every acceptance-critical property is pinned: the 3-way vocabulary, the
unclear-never-forced rule, the T3 >= 80% agreement gate, and the confusion
matrix / agreement stats that flow into the eval-report artifact.
"""

from pathlib import Path
from typing import Any

import pytest
from cricai_data.enums import BowlingVariation
from cricai_vision.train import RunResult, build_eval_report
from cricai_vision.variation import (
    AGREEMENT_GATE,
    CLASSIFIER_CLASS_FRACTION_KEYS,
    CLASSIFIER_HEADLINE_KEYS,
    CLASSIFIER_REPORT_EXTRA_KEYS,
    UNCLEAR_LABEL,
    VARIATION_CLASSES,
    DeliveryFeatures,
    FakeVariationClassifier,
    VariationClassifierProtocol,
    VariationError,
    VariationPrediction,
    evaluate_variations,
    meets_agreement_gate,
)

LB = BowlingVariation.LEG_BREAK
TS = BowlingVariation.TOP_SPINNER
GO = BowlingVariation.GOOGLY
UN = BowlingVariation.UNKNOWN


# --- vocabulary pins --------------------------------------------------------------


def test_three_way_vocabulary_is_pinned() -> None:
    assert VARIATION_CLASSES == ("leg_break", "top_spinner", "googly")
    assert UNCLEAR_LABEL == "unknown"
    assert CLASSIFIER_HEADLINE_KEYS == ("variation_agreement_3way", "unclear_rate")
    assert CLASSIFIER_CLASS_FRACTION_KEYS == ("precision", "recall")
    assert CLASSIFIER_REPORT_EXTRA_KEYS == ("confusion_matrix",)
    assert AGREEMENT_GATE == 0.80


# --- features ---------------------------------------------------------------------


def test_features_accept_optional_fields_absent() -> None:
    features = DeliveryFeatures(turn_cm=-12.5)
    assert features.apex_m is None
    assert features.dip_flag is None
    assert features.release_height_cm is None


def test_features_accept_full_measurements() -> None:
    features = DeliveryFeatures(turn_cm=9.0, apex_m=2.3, dip_flag=True, release_height_cm=198.5)
    assert features.release_height_cm == 198.5


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"turn_cm": float("nan")}, "turn_cm must be a finite number"),
        ({"turn_cm": float("inf")}, "turn_cm must be a finite number"),
        ({"turn_cm": 5.0, "apex_m": float("nan")}, "apex_m must be a finite number"),
        ({"turn_cm": 5.0, "apex_m": 0.0}, "apex_m must be > 0"),
        (
            {"turn_cm": 5.0, "release_height_cm": float("inf")},
            "release_height_cm must be a finite number",
        ),
        ({"turn_cm": 5.0, "release_height_cm": -180.0}, "release_height_cm must be > 0"),
    ],
)
def test_features_reject_unusable_values(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(VariationError, match=match):
        DeliveryFeatures(**kwargs)


# --- prediction -------------------------------------------------------------------


def test_prediction_rejects_deferred_classes_and_bad_confidence() -> None:
    with pytest.raises(VariationError, match="slider"):
        VariationPrediction(BowlingVariation.SLIDER, 0.9)
    with pytest.raises(VariationError, match="confidence"):
        VariationPrediction(LB, 1.2)
    with pytest.raises(VariationError, match="confidence"):
        VariationPrediction(LB, float("nan"))


def test_prediction_accepts_unclear() -> None:
    assert VariationPrediction(UN, 0.0).variation is UN


# --- fake classifier ---------------------------------------------------------------


def test_fake_satisfies_protocol_and_is_deterministic() -> None:
    classifier: VariationClassifierProtocol = FakeVariationClassifier()
    features = DeliveryFeatures(turn_cm=14.0, apex_m=2.1, dip_flag=True)
    assert classifier.classify(features) == classifier.classify(features)
    assert classifier.version == "fake-variation-1"


def test_big_turn_away_is_leg_break() -> None:
    prediction = FakeVariationClassifier().classify(DeliveryFeatures(turn_cm=12.0))
    assert prediction.variation is LB
    assert prediction.confidence == 0.75  # 12 / 16


def test_big_turn_in_is_googly() -> None:
    prediction = FakeVariationClassifier().classify(DeliveryFeatures(turn_cm=-9.0))
    assert prediction.variation is GO
    assert prediction.confidence == 0.5625


def test_confidence_caps_at_one() -> None:
    assert FakeVariationClassifier().classify(DeliveryFeatures(turn_cm=40.0)).confidence == 1.0


def test_straight_with_dip_is_top_spinner_apex_sharpens_confidence() -> None:
    classifier = FakeVariationClassifier()
    with_apex = classifier.classify(DeliveryFeatures(turn_cm=1.0, apex_m=2.4, dip_flag=True))
    assert with_apex == VariationPrediction(TS, 0.75)
    without_apex = classifier.classify(DeliveryFeatures(turn_cm=1.0, dip_flag=True))
    assert without_apex == VariationPrediction(TS, 0.6)


@pytest.mark.parametrize(
    "features",
    [
        DeliveryFeatures(turn_cm=5.0),  # between straight and big: ambiguous
        DeliveryFeatures(turn_cm=-5.0),
        DeliveryFeatures(turn_cm=1.0),  # straight but no dip evidence
        DeliveryFeatures(turn_cm=1.0, dip_flag=False),
    ],
)
def test_unclear_is_never_force_classified(features: DeliveryFeatures) -> None:
    assert FakeVariationClassifier().classify(features) == VariationPrediction(UN, 0.0)


def test_confidence_floor_collapses_borderline_calls_to_unclear() -> None:
    strict = FakeVariationClassifier(confidence_floor=0.7)
    assert strict.classify(DeliveryFeatures(turn_cm=8.5)) == VariationPrediction(UN, 0.0)
    assert strict.classify(DeliveryFeatures(turn_cm=1.0, dip_flag=True)) == VariationPrediction(
        UN, 0.0
    )
    assert strict.classify(DeliveryFeatures(turn_cm=14.0)).variation is LB


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"big_turn_cm": 0.0}, "big_turn_cm must be > 0"),
        ({"straight_turn_cm": 0.0}, "straight_turn_cm"),
        ({"straight_turn_cm": 8.0}, "straight_turn_cm"),
        ({"confidence_floor": 1.5}, "confidence_floor"),
    ],
)
def test_fake_config_validation(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(VariationError, match=match):
        FakeVariationClassifier(**kwargs)


# --- honest eval ------------------------------------------------------------------


def test_matrix_counts_and_agreement() -> None:
    pairs = [
        (LB, LB),
        (LB, LB),
        (LB, GO),
        (TS, TS),
        (TS, UN),
        (GO, GO),
        (GO, LB),
        (BowlingVariation.SLIDER, LB),  # deferred intent: excluded, counted
        (UN, UN),  # unknown intent: excluded, counted
    ]
    evaluation = evaluate_variations(pairs)
    assert evaluation.matrix["leg_break"] == {
        "leg_break": 2,
        "top_spinner": 0,
        "googly": 1,
        "unknown": 0,
    }
    assert evaluation.matrix["top_spinner"]["unknown"] == 1
    assert evaluation.excluded_intents == 2
    assert evaluation.n_pairs == 7
    assert evaluation.n_classified == 6
    assert evaluation.agreement_3way == pytest.approx(4 / 6)
    assert evaluation.unclear_rate == pytest.approx(1 / 7)


def test_detected_outside_vocabulary_folds_into_unknown_column() -> None:
    evaluation = evaluate_variations([(LB, BowlingVariation.FLIPPER)])
    assert evaluation.matrix["leg_break"]["unknown"] == 1
    assert evaluation.n_classified == 0
    assert evaluation.agreement_3way is None
    assert evaluation.unclear_rate == 1.0


def test_empty_pairs_yield_no_fabricated_stats() -> None:
    evaluation = evaluate_variations([])
    assert evaluation.n_pairs == 0
    assert evaluation.agreement_3way is None
    assert evaluation.unclear_rate is None
    with pytest.raises(VariationError, match="refusing to fabricate"):
        evaluation.headline_metrics()


def test_headline_metrics_are_rounded_fractions() -> None:
    evaluation = evaluate_variations([(LB, LB), (LB, LB), (LB, GO), (GO, UN)])
    assert evaluation.headline_metrics() == {
        "variation_agreement_3way": round(2 / 3, 4),
        "unclear_rate": 0.25,
    }


def test_per_class_rows_zero_denominators_read_as_zero() -> None:
    rows = evaluate_variations([(LB, LB), (LB, TS)]).per_class_rows()
    by_label = {row["label"]: row for row in rows}
    assert by_label["leg_break"] == {
        "label": "leg_break",
        "precision": 1.0,
        "recall": 0.5,
        "support": 2,
    }
    # top_spinner was predicted once (wrongly) but never intended: recall has
    # no denominator and reads 0.0 — never inflated.
    assert by_label["top_spinner"] == {
        "label": "top_spinner",
        "precision": 0.0,
        "recall": 0.0,
        "support": 0,
    }
    assert by_label["googly"]["precision"] == 0.0


def test_gate_predicate_per_t3() -> None:
    assert meets_agreement_gate(0.80) is True
    assert meets_agreement_gate(0.799) is False
    assert meets_agreement_gate(None) is False
    assert meets_agreement_gate(0.5, gate=0.5) is True
    with pytest.raises(VariationError, match="gate must be within"):
        meets_agreement_gate(0.9, gate=0.0)


def test_eval_flows_into_generalized_eval_report() -> None:
    """The classifier eval plugs into the shared artifact machinery (US-I6)."""
    evaluation = evaluate_variations([(LB, LB), (LB, LB), (TS, TS), (GO, GO), (GO, LB), (TS, UN)])
    report = build_eval_report(
        model_name="legspin-variation",
        dataset_version="deliveries-v1",
        trainer_version="fake-variation-1",
        config={"big_turn_cm": 8.0},
        result=RunResult(
            metrics=evaluation.headline_metrics(),
            weights_path=Path("unused.pt"),
            report=evaluation.to_report(),
        ),
        headline_keys=CLASSIFIER_HEADLINE_KEYS,
        class_fraction_keys=CLASSIFIER_CLASS_FRACTION_KEYS,
        extra_report_keys=CLASSIFIER_REPORT_EXTRA_KEYS,
    )
    assert report["headline"]["variation_agreement_3way"] == 0.8
    assert report["confusion_matrix"]["googly"]["leg_break"] == 1
    assert [row["label"] for row in report["per_class"]] == list(VARIATION_CLASSES)
    assert meets_agreement_gate(report["headline"]["variation_agreement_3way"]) is True
