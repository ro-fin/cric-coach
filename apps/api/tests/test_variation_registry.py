"""US-I6 registry proof: legspin-variation flows through the EXISTING models API.

No registry code changed for the classifier model type — the generalized
``build_eval_report`` key sets plus the models router's model-agnostic run
lifecycle are enough: classifier metrics (``variation_agreement_3way``,
``unclear_rate``) land on the ModelRun row and a candidate version registers,
exactly as detector runs do.
"""

import json
from pathlib import Path

import pytest
from cricai_data.db import make_session_factory
from cricai_data.enums import BowlingVariation
from cricai_data.models import Dataset
from cricai_vision.train import RunResult, build_eval_report
from cricai_vision.variation import (
    CLASSIFIER_CLASS_FRACTION_KEYS,
    CLASSIFIER_HEADLINE_KEYS,
    CLASSIFIER_REPORT_EXTRA_KEYS,
    DeliveryFeatures,
    FakeVariationClassifier,
    evaluate_variations,
    meets_agreement_gate,
)
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from cricai_testing.apptest import PARENT_TOKEN, auth, make_sqlite_engine, make_test_app

LB = BowlingVariation.LEG_BREAK
TS = BowlingVariation.TOP_SPINNER
GO = BowlingVariation.GOOGLY

#: A labeled benchmark set (intent, features): the fake classifier agrees on
#: 5 of 6 and answers "unknown" once — agreement 5/6, unclear rate 1/7.
BENCH: tuple[tuple[BowlingVariation, DeliveryFeatures], ...] = (
    (LB, DeliveryFeatures(turn_cm=14.0)),
    (LB, DeliveryFeatures(turn_cm=11.0, apex_m=2.3)),
    (GO, DeliveryFeatures(turn_cm=-12.0)),
    (GO, DeliveryFeatures(turn_cm=-16.0, release_height_cm=201.0)),
    (TS, DeliveryFeatures(turn_cm=0.5, dip_flag=True, apex_m=2.6)),
    (TS, DeliveryFeatures(turn_cm=-9.5)),  # drifts like a googly: honest disagreement
    (LB, DeliveryFeatures(turn_cm=5.0)),  # ambiguous: unclear, never forced
)


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(tmp_path: Path, engine: Engine) -> TestClient:
    client = TestClient(make_test_app(tmp_path, engine))
    with make_session_factory(engine)() as db:
        db.add(Dataset(version="deliveries-v1", frozen=True, manifest_digest="b" * 64))
        db.commit()
    return client


def test_legspin_variation_metrics_flow_through_model_runs(client: TestClient) -> None:
    classifier = FakeVariationClassifier()
    pairs = [(intent, classifier.classify(features).variation) for intent, features in BENCH]
    evaluation = evaluate_variations(pairs)
    report = build_eval_report(
        model_name="legspin-variation",
        dataset_version="deliveries-v1",
        trainer_version=classifier.version,
        config={"big_turn_cm": classifier.big_turn_cm, "floor": classifier.confidence_floor},
        result=RunResult(
            metrics=evaluation.headline_metrics(),
            weights_path=Path("unused.pt"),
            report=evaluation.to_report(),
        ),
        headline_keys=CLASSIFIER_HEADLINE_KEYS,
        class_fraction_keys=CLASSIFIER_CLASS_FRACTION_KEYS,
        extra_report_keys=CLASSIFIER_REPORT_EXTRA_KEYS,
    )
    assert json.loads(json.dumps(report)) == report  # artifact is JSON-clean

    run = client.post(
        "/model-runs",
        json={
            "model_name": "legspin-variation",
            "dataset_version": "deliveries-v1",
            "trainer_version": classifier.version,
            "config": report["config"],
        },
        headers=auth(PARENT_TOKEN),
    )
    assert run.status_code == 201
    run_id = run.json()["id"]
    assert (
        client.post(
            f"/model-runs/{run_id}/transition",
            json={"status": "running"},
            headers=auth(PARENT_TOKEN),
        ).status_code
        == 200
    )
    succeeded = client.post(
        f"/model-runs/{run_id}/transition",
        json={
            "status": "succeeded",
            "metrics": report["headline"],
            "report_key": "models/legspin-variation/runs/r1/eval-report.json",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert succeeded.status_code == 200

    stored = client.get(f"/model-runs/{run_id}", headers=auth(PARENT_TOKEN)).json()
    assert stored["metrics"] == {
        "variation_agreement_3way": round(5 / 6, 4),
        "unclear_rate": round(1 / 7, 4),
    }
    assert stored["status"] == "succeeded"

    version = client.post(
        "/model-versions",
        json={"model_name": "legspin-variation", "version": "0.1.0", "run_id": run_id},
        headers=auth(PARENT_TOKEN),
    )
    assert version.status_code == 201
    assert version.json()["stage"] == "candidate"

    # T3 honesty: 5/6 agreement passes the >= 80% gate; the confusion matrix
    # in the artifact names the one honest disagreement (top spinner -> googly).
    assert meets_agreement_gate(stored["metrics"]["variation_agreement_3way"]) is True
    assert report["confusion_matrix"]["top_spinner"]["googly"] == 1


def test_classifier_metrics_reject_non_fraction_values(client: TestClient) -> None:
    """The models API's 0-1 fraction rule applies to classifier metrics too."""
    run = client.post(
        "/model-runs",
        json={
            "model_name": "legspin-variation",
            "dataset_version": "deliveries-v1",
            "trainer_version": "fake-variation-1",
            "config": {},
        },
        headers=auth(PARENT_TOKEN),
    )
    run_id = run.json()["id"]
    client.post(
        f"/model-runs/{run_id}/transition",
        json={"status": "running"},
        headers=auth(PARENT_TOKEN),
    )
    rejected = client.post(
        f"/model-runs/{run_id}/transition",
        json={"status": "succeeded", "metrics": {"variation_agreement_3way": 83.0}},
        headers=auth(PARENT_TOKEN),
    )
    assert rejected.status_code == 422
    assert "variation_agreement_3way" in rejected.json()["detail"]
