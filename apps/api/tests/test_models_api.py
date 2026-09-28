"""US-F2 model registry & promotion API tests.

Covers run lifecycle transitions, candidate registration, the staged promotion
path and both promotion gates: production completeness (dataset version, config,
pinned metrics, report artifact) and the AUTO-BLOCK regression rule (> 2.0
percentage points on any pinned headline metric vs the current production
version, missing metrics blocking loudly on either side).
"""

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from cricai_api.routers.models import _enforce_no_regression
from cricai_data.db import make_session_factory
from cricai_data.models import AuditLog, Dataset, ModelRun
from cricai_vision.train import HEADLINE_METRIC_KEYS
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from cricai_testing.apptest import (
    COACH_TOKEN,
    PARENT_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)

GOOD_METRICS = {
    "map50_ball": 0.9,
    "map50_bat": 0.85,
    "map50_stumps": 0.96,
    "recall_ball_high_blur": 0.78,
}

CONFIG = {"imgsz": 640, "epochs": 50}

UNKNOWN_ID = str(uuid.uuid4())


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(tmp_path: Path, engine: Engine) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine))


@pytest.fixture
def db_factory(engine: Engine) -> sessionmaker[OrmSession]:
    return make_session_factory(engine)


@pytest.fixture
def dataset(db_factory: sessionmaker[OrmSession]) -> str:
    with db_factory() as db:
        db.add(Dataset(version="v1", frozen=True, manifest_digest="a" * 64))
        db.commit()
    return "v1"


def _record_run(
    client: TestClient,
    *,
    model_name: str = "ball-detector",
    config: dict[str, Any] | None = None,
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    return client.post(
        "/model-runs",
        json={
            "model_name": model_name,
            "dataset_version": "v1",
            "trainer_version": "fake-trainer-1",
            "config": CONFIG if config is None else config,
        },
        headers=auth(token),
    )


def _transition(
    client: TestClient, run_id: str, body: dict[str, Any], token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(f"/model-runs/{run_id}/transition", json=body, headers=auth(token))


def _succeeded_run(
    client: TestClient,
    *,
    model_name: str = "ball-detector",
    metrics: dict[str, float] | None = None,
    report_key: str | None = "models/ball-detector/runs/r/eval-report.json",
    config: dict[str, Any] | None = None,
) -> str:
    run_id: str = _record_run(client, model_name=model_name, config=config).json()["id"]
    assert _transition(client, run_id, {"status": "running"}).status_code == 200
    body: dict[str, Any] = {"status": "succeeded"}
    if metrics is not None:
        body["metrics"] = metrics
    if report_key is not None:
        body["report_key"] = report_key
    assert _transition(client, run_id, body).status_code == 200
    return run_id


def _register(
    client: TestClient,
    run_id: str,
    *,
    model_name: str = "ball-detector",
    version: str = "1.0.0",
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    return client.post(
        "/model-versions",
        json={"model_name": model_name, "version": version, "run_id": run_id},
        headers=auth(token),
    )


def _promote(
    client: TestClient, version_id: str, target: str, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(
        f"/model-versions/{version_id}/promote",
        json={"target_stage": target},
        headers=auth(token),
    )


def _production_version(
    client: TestClient,
    *,
    model_name: str = "ball-detector",
    version: str = "1.0.0",
    metrics: dict[str, float] | None = None,
) -> str:
    run_id = _succeeded_run(
        client, model_name=model_name, metrics=GOOD_METRICS if metrics is None else metrics
    )
    version_id: str = _register(client, run_id, model_name=model_name, version=version).json()["id"]
    assert _promote(client, version_id, "staging").status_code == 200
    assert _promote(client, version_id, "production").status_code == 200
    return version_id


# ------------------------------------------------------------------ auth (US-L3)


def test_requires_bearer_token(client: TestClient) -> None:
    assert client.get("/model-runs").status_code == 401
    assert client.post("/model-runs", json={}).status_code == 401


def test_player_is_read_only(client: TestClient, dataset: str) -> None:
    assert _record_run(client, token=PLAYER_TOKEN).status_code == 403
    run_id = _record_run(client).json()["id"]
    assert _transition(client, run_id, {"status": "running"}, token=PLAYER_TOKEN).status_code == 403
    assert _register(client, run_id, token=PLAYER_TOKEN).status_code == 403
    assert _promote(client, UNKNOWN_ID, "staging", token=PLAYER_TOKEN).status_code == 403
    for path in ("/model-runs", f"/model-runs/{run_id}", "/model-versions"):
        assert client.get(path, headers=auth(PLAYER_TOKEN)).status_code == 200


# ------------------------------------------------------------------ run lifecycle


def test_record_run_starts_pending(client: TestClient, dataset: str) -> None:
    response = _record_run(client, token=COACH_TOKEN)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["dataset_version"] == "v1"
    assert body["config"] == CONFIG
    assert body["metrics"] == {}
    assert body["report_key"] is None
    assert body["trainer_version"] == "fake-trainer-1"
    assert body["started_at"] is None and body["finished_at"] is None
    fetched = client.get(f"/model-runs/{body['id']}", headers=auth(PARENT_TOKEN))
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_record_run_unknown_dataset_404(client: TestClient) -> None:
    response = client.post(
        "/model-runs",
        json={"model_name": "m", "dataset_version": "nope", "trainer_version": "t"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 404
    assert "dataset version not found" in response.json()["detail"]


def test_record_run_requires_frozen_dataset(
    client: TestClient, db_factory: sessionmaker[OrmSession]
) -> None:
    """US-F1/US-F2: a run must pin an immutable (frozen) dataset version — the
    API reporting path enforces the same freeze rule as the train_detector job."""
    with db_factory() as db:
        db.add(Dataset(version="v1", frozen=False))
        db.commit()
    response = _record_run(client)
    assert response.status_code == 409
    assert "not frozen" in response.json()["detail"]


def test_run_walks_pending_running_succeeded(client: TestClient, dataset: str) -> None:
    run_id = _record_run(client).json()["id"]
    running = _transition(client, run_id, {"status": "running"})
    assert running.status_code == 200
    assert running.json()["started_at"] is not None
    assert running.json()["finished_at"] is None
    succeeded = _transition(
        client,
        run_id,
        {"status": "succeeded", "metrics": GOOD_METRICS, "report_key": "models/r/report.json"},
    )
    assert succeeded.status_code == 200
    body = succeeded.json()
    assert body["status"] == "succeeded"
    assert body["metrics"] == GOOD_METRICS
    assert body["report_key"] == "models/r/report.json"
    assert body["finished_at"] is not None


def test_run_can_fail_and_terminal_states_are_final(client: TestClient, dataset: str) -> None:
    run_id = _record_run(client).json()["id"]
    _transition(client, run_id, {"status": "running"})
    failed = _transition(client, run_id, {"status": "failed"})
    assert failed.status_code == 200
    assert failed.json()["finished_at"] is not None
    stuck = _transition(client, run_id, {"status": "running"})
    assert stuck.status_code == 409
    assert "illegal transition failed -> running" in stuck.json()["detail"]


def test_succeeded_without_metrics_is_legal_but_unpromotable(
    client: TestClient, dataset: str
) -> None:
    run_id = _record_run(client).json()["id"]
    _transition(client, run_id, {"status": "running"})
    response = _transition(client, run_id, {"status": "succeeded"})
    assert response.status_code == 200
    assert response.json()["metrics"] == {}


def test_pending_cannot_jump_to_succeeded(client: TestClient, dataset: str) -> None:
    run_id = _record_run(client).json()["id"]
    response = _transition(client, run_id, {"status": "succeeded"})
    assert response.status_code == 409
    assert "pending -> running" in response.json()["detail"]


def test_transition_unknown_run_404(client: TestClient, dataset: str) -> None:
    assert _transition(client, UNKNOWN_ID, {"status": "running"}).status_code == 404
    assert client.get(f"/model-runs/{UNKNOWN_ID}", headers=auth(PARENT_TOKEN)).status_code == 404


def test_metrics_only_accepted_on_succeeded_transition(client: TestClient, dataset: str) -> None:
    run_id = _record_run(client).json()["id"]
    response = _transition(client, run_id, {"status": "running", "metrics": GOOD_METRICS})
    assert response.status_code == 422
    assert "succeeded transition only" in response.json()["detail"]
    _transition(client, run_id, {"status": "running"})
    response = _transition(client, run_id, {"status": "failed", "report_key": "k"})
    assert response.status_code == 422


@pytest.mark.parametrize("bad_value", [1.5, -0.1])
def test_metrics_must_be_fractions(client: TestClient, dataset: str, bad_value: float) -> None:
    run_id = _record_run(client).json()["id"]
    _transition(client, run_id, {"status": "running"})
    response = _transition(
        client,
        run_id,
        {"status": "succeeded", "metrics": {**GOOD_METRICS, "map50_ball": bad_value}},
    )
    assert response.status_code == 422
    assert "finite fraction within [0, 1]" in response.json()["detail"]


@pytest.mark.parametrize("token", ["NaN", "Infinity"])
def test_nan_metrics_rejected_not_stored(client: TestClient, dataset: str, token: str) -> None:
    """json.loads accepts bare NaN/Infinity tokens; they must 422, never persist."""
    run_id = _record_run(client).json()["id"]
    _transition(client, run_id, {"status": "running"})
    body = json.dumps({"status": "succeeded", "metrics": {"map50_ball": 0.9}})
    body = body.replace("0.9", token)  # raw token: httpx's json= refuses to encode NaN
    response = client.post(
        f"/model-runs/{run_id}/transition",
        content=body,
        headers={**auth(PARENT_TOKEN), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "finite fraction within [0, 1]" in str(detail)


def test_list_runs_filters_by_model_name_and_status(client: TestClient, dataset: str) -> None:
    first = _record_run(client, model_name="ball-detector").json()["id"]
    second = _record_run(client, model_name="bat-detector").json()["id"]
    _transition(client, second, {"status": "running"})
    everything = client.get("/model-runs", headers=auth(PARENT_TOKEN)).json()
    assert [run["id"] for run in everything] == [first, second]
    named = client.get(
        "/model-runs", params={"model_name": "ball-detector"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert [run["id"] for run in named] == [first]
    running = client.get(
        "/model-runs", params={"status": "running"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert [run["id"] for run in running] == [second]


# ------------------------------------------------------------------ registration


def test_register_succeeded_run_as_candidate(client: TestClient, dataset: str) -> None:
    run_id = _succeeded_run(client, metrics=GOOD_METRICS)
    response = _register(client, run_id, token=COACH_TOKEN)
    assert response.status_code == 201
    body = response.json()
    assert body["stage"] == "candidate"
    assert body["run_id"] == run_id
    assert body["promoted_at"] is None and body["promoted_by"] is None


def test_register_unknown_run_404(client: TestClient, dataset: str) -> None:
    assert _register(client, UNKNOWN_ID).status_code == 404


def test_register_model_name_mismatch_422(client: TestClient, dataset: str) -> None:
    run_id = _succeeded_run(client, metrics=GOOD_METRICS)
    response = _register(client, run_id, model_name="bat-detector")
    assert response.status_code == 422
    assert "trained 'ball-detector', not 'bat-detector'" in response.json()["detail"]


def test_register_requires_succeeded_run(client: TestClient, dataset: str) -> None:
    run_id = _record_run(client).json()["id"]
    response = _register(client, run_id)
    assert response.status_code == 409
    assert "only succeeded runs" in response.json()["detail"]


def test_register_duplicate_identity_409(client: TestClient, dataset: str) -> None:
    run_id = _succeeded_run(client, metrics=GOOD_METRICS)
    assert _register(client, run_id, version="1.0.0").status_code == 201
    response = _register(client, run_id, version="1.0.0")
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


def test_list_versions_filters(client: TestClient, dataset: str) -> None:
    ball_run = _succeeded_run(client, metrics=GOOD_METRICS)
    bat_run = _succeeded_run(client, model_name="bat-detector", metrics=GOOD_METRICS)
    ball_version = _register(client, ball_run).json()["id"]
    _register(client, bat_run, model_name="bat-detector")
    _promote(client, ball_version, "staging")
    named = client.get(
        "/model-versions", params={"model_name": "ball-detector"}, headers=auth(PLAYER_TOKEN)
    ).json()
    assert [version["id"] for version in named] == [ball_version]
    staged = client.get(
        "/model-versions", params={"stage": "staging"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert [version["id"] for version in staged] == [ball_version]
    assert len(client.get("/model-versions", headers=auth(PARENT_TOKEN)).json()) == 2


# ------------------------------------------------------------------ promotion path


def test_promotion_advances_one_stage_at_a_time(
    client: TestClient, db_factory: sessionmaker[OrmSession], dataset: str
) -> None:
    run_id = _succeeded_run(client, metrics=GOOD_METRICS)
    version_id = _register(client, run_id).json()["id"]

    skip = _promote(client, version_id, "production")
    assert skip.status_code == 409
    assert "illegal promotion candidate -> production" in skip.json()["detail"]

    staged = _promote(client, version_id, "staging", token=COACH_TOKEN)
    assert staged.status_code == 200
    assert staged.json()["stage"] == "staging"
    assert staged.json()["promoted_by"] == "coach"
    assert staged.json()["promoted_at"] is not None

    production = _promote(client, version_id, "production")
    assert production.status_code == 200
    assert production.json()["stage"] == "production"
    assert production.json()["promoted_by"] == "parent"

    beyond = _promote(client, version_id, "production")
    assert beyond.status_code == 409  # production is the last stage

    with db_factory() as db:
        audits = db.scalars(select(AuditLog).where(AuditLog.action == "model_promote")).all()
    assert [audit.actor for audit in audits] == ["coach", "parent"]
    assert audits[1].entity == "model_version"
    assert audits[1].entity_id == version_id
    assert audits[1].detail == {
        "model_name": "ball-detector",
        "version": "1.0.0",
        "to_stage": "production",
        "replaced_production": None,
    }


def test_promote_unknown_version_404(client: TestClient, dataset: str) -> None:
    assert _promote(client, UNKNOWN_ID, "staging").status_code == 404


def test_promote_wrong_target_stage_409(client: TestClient, dataset: str) -> None:
    run_id = _succeeded_run(client, metrics=GOOD_METRICS)
    version_id = _register(client, run_id).json()["id"]
    response = _promote(client, version_id, "candidate")
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("kwargs", "expected_missing"),
    [
        ({"metrics": GOOD_METRICS, "report_key": None}, "report_key"),
        (
            {"metrics": {key: 0.9 for key in HEADLINE_METRIC_KEYS if key != "map50_bat"}},
            "metrics['map50_bat']",
        ),
        ({"metrics": GOOD_METRICS, "config": {}}, "config"),
        ({"metrics": None}, "metrics['map50_ball']"),
    ],
)
def test_production_requires_complete_provenance(
    client: TestClient, dataset: str, kwargs: dict[str, Any], expected_missing: str
) -> None:
    """US-F2 AC: every production model has dataset version, config, metrics, report."""
    run_id = _succeeded_run(client, **kwargs)
    version_id = _register(client, run_id).json()["id"]
    assert _promote(client, version_id, "staging").status_code == 200
    response = _promote(client, version_id, "production")
    assert response.status_code == 409
    assert expected_missing in response.json()["detail"]
    assert "cannot promote to production" in response.json()["detail"]


def test_promotion_to_production_rechecks_frozen_dataset(
    client: TestClient, db_factory: sessionmaker[OrmSession], dataset: str
) -> None:
    """Promotion re-verifies the run's dataset is still frozen (defense in depth:
    a run recorded before the freeze rule, or a row mutated out of band, must not
    reach production with a mutable dataset reference)."""
    run_id = _succeeded_run(client, metrics=GOOD_METRICS)
    version_id = _register(client, run_id).json()["id"]
    assert _promote(client, version_id, "staging").status_code == 200
    with db_factory() as db:
        stored = db.scalar(select(Dataset).where(Dataset.version == "v1"))
        assert stored is not None
        stored.frozen = False
        db.commit()
    response = _promote(client, version_id, "production")
    assert response.status_code == 409
    assert "not frozen" in response.json()["detail"]


# ------------------------------------------------------------------ AUTO-BLOCK gate


def _challenger(client: TestClient, metrics: dict[str, float], version: str = "2.0.0") -> str:
    run_id = _succeeded_run(client, metrics=metrics)
    version_id: str = _register(client, run_id, version=version).json()["id"]
    assert _promote(client, version_id, "staging").status_code == 200
    return version_id


def test_regression_over_two_points_blocks_promotion(client: TestClient, dataset: str) -> None:
    _production_version(client)
    challenger = _challenger(client, {**GOOD_METRICS, "map50_ball": 0.879})
    response = _promote(client, challenger, "production")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "map50_ball regresses 2.1 points" in detail
    assert "0.8790 vs 0.9000" in detail
    assert "limit is 2.0 points" in detail
    # The candidate stays in staging; production is untouched.
    production = client.get(
        "/model-versions", params={"stage": "production"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert [version["version"] for version in production] == ["1.0.0"]


def test_regression_of_exactly_two_points_is_allowed(client: TestClient, dataset: str) -> None:
    """0.90 -> 0.88 is exactly 2.0 points; float noise must not tip it over the gate."""
    _production_version(client)
    challenger = _challenger(client, {**GOOD_METRICS, "map50_ball": 0.88})
    assert _promote(client, challenger, "production").status_code == 200


def test_improvement_replaces_production_and_demotes_old(client: TestClient, dataset: str) -> None:
    old_id = _production_version(client)
    challenger = _challenger(client, {**GOOD_METRICS, "map50_ball": 0.93})
    response = _promote(client, challenger, "production")
    assert response.status_code == 200
    versions = {
        version["id"]: version["stage"]
        for version in client.get("/model-versions", headers=auth(PARENT_TOKEN)).json()
    }
    assert versions[challenger] == "production"
    assert versions[old_id] == "staging"  # rollback path: exactly one production


def test_missing_metric_on_current_production_blocks_loudly(
    client: TestClient, db_factory: sessionmaker[OrmSession], dataset: str
) -> None:
    """A pre-contract production model without pinned metrics can never be silently
    'compared past' — promotion is refused until the baseline is re-evaluated."""
    _production_version(client)
    with db_factory() as db:
        run = db.scalars(select(ModelRun)).first()
        assert run is not None
        run.metrics = {key: value for key, value in run.metrics.items() if key != "map50_bat"}
        db.commit()
    challenger = _challenger(client, GOOD_METRICS)
    response = _promote(client, challenger, "production")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "current production run has no usable 'map50_bat' metric" in detail


def test_missing_candidate_metric_blocks_in_gate_unit() -> None:
    """The gate itself blocks a metric-less candidate even though the API's
    completeness check runs first (defense in depth for other callers)."""
    candidate = ModelRun(model_name="m", metrics={}, trainer_version="t")
    production = ModelRun(model_name="m", metrics=dict(GOOD_METRICS), trainer_version="t")
    with pytest.raises(HTTPException, match="candidate run has no usable 'map50_ball'"):
        _enforce_no_regression(candidate, production)


def test_production_gate_is_scoped_per_model_name(client: TestClient, dataset: str) -> None:
    """A different model name never gates against ball-detector's production."""
    _production_version(client)
    low = dict.fromkeys(GOOD_METRICS, 0.5)
    run_id = _succeeded_run(client, model_name="bat-detector", metrics=low)
    version_id = _register(client, run_id, model_name="bat-detector", version="0.1.0").json()["id"]
    assert _promote(client, version_id, "staging").status_code == 200
    assert _promote(client, version_id, "production").status_code == 200
