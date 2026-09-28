"""US-F2 trainer/eval-report tests: determinism, digest sensitivity, pinned contract."""

from pathlib import Path
from typing import Any

import pytest
from cricai_vision.train import (
    HEADLINE_METRIC_KEYS,
    REPORT_CLASSES,
    REPORT_SCHEMA_VERSION,
    FakeTrainer,
    RunResult,
    TrainerProtocol,
    TrainingError,
    build_eval_report,
    canonical_config,
    dataset_digest,
)

CONFIG = {"imgsz": 640, "epochs": 50, "seed": 7}


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    root = tmp_path / "dataset-v1"
    (root / "images").mkdir(parents=True)
    (root / "labels").mkdir(parents=True)
    (root / "images" / "f0001.jpg").write_bytes(b"jpeg-bytes-1")
    (root / "images" / "f0002.jpg").write_bytes(b"jpeg-bytes-2")
    (root / "labels" / "f0001.txt").write_text("0 0.5 0.5 0.02 0.02\n")
    return root


def _trainer(tmp_path: Path, **kwargs: Any) -> FakeTrainer:
    return FakeTrainer(output_dir=tmp_path / "runs", **kwargs)


def test_fake_trainer_satisfies_protocol_and_is_deterministic(
    tmp_path: Path, dataset_dir: Path
) -> None:
    trainer: TrainerProtocol = _trainer(tmp_path)
    first = trainer.train(dataset_dir, CONFIG)
    second = trainer.train(dataset_dir, CONFIG)
    assert first == second
    assert first.weights_path.read_bytes() == second.weights_path.read_bytes()
    assert trainer.version == "fake-trainer-1"


def test_fake_trainer_emits_pinned_headline_keys_in_range(
    tmp_path: Path, dataset_dir: Path
) -> None:
    result = _trainer(tmp_path).train(dataset_dir, CONFIG)
    assert tuple(result.metrics) == HEADLINE_METRIC_KEYS
    assert all(0.75 <= value <= 0.95 for value in result.metrics.values())


def test_fake_trainer_per_class_rows_match_headline(tmp_path: Path, dataset_dir: Path) -> None:
    result = _trainer(tmp_path).train(dataset_dir, CONFIG)
    rows = result.report["per_class"]
    assert [row["label"] for row in rows] == list(REPORT_CLASSES) == ["ball", "bat", "stumps"]
    for row in rows:
        assert row["map50"] == result.metrics[f"map50_{row['label']}"]
        assert 0.75 <= row["precision"] <= 0.95
        assert 0.75 <= row["recall"] <= 0.95
        assert 40 <= row["support"] <= 295
    assert result.report["dataset_digest"] == dataset_digest(dataset_dir)


def test_fake_trainer_writes_deterministic_weights_artifact(
    tmp_path: Path, dataset_dir: Path
) -> None:
    result = _trainer(tmp_path).train(dataset_dir, CONFIG)
    assert result.weights_path.is_file()
    assert result.weights_path.name.startswith("weights-")
    assert result.weights_path.suffix == ".pt"
    assert result.weights_path.read_bytes().startswith(b"fake-weights ")


@pytest.mark.parametrize(
    "variation",
    ["config", "seed", "dataset_content", "dataset_layout"],
)
def test_fake_trainer_metrics_shift_when_inputs_change(
    tmp_path: Path, dataset_dir: Path, variation: str
) -> None:
    baseline = _trainer(tmp_path).train(dataset_dir, CONFIG).metrics
    if variation == "config":
        changed = _trainer(tmp_path).train(dataset_dir, {**CONFIG, "epochs": 51}).metrics
    elif variation == "seed":
        changed = _trainer(tmp_path, seed=8).train(dataset_dir, CONFIG).metrics
    elif variation == "dataset_content":
        (dataset_dir / "images" / "f0001.jpg").write_bytes(b"jpeg-bytes-CHANGED")
        changed = _trainer(tmp_path).train(dataset_dir, CONFIG).metrics
    else:
        (dataset_dir / "images" / "f0001.jpg").rename(dataset_dir / "images" / "f9999.jpg")
        changed = _trainer(tmp_path).train(dataset_dir, CONFIG).metrics
    assert changed != baseline


def test_config_key_order_does_not_change_the_run(tmp_path: Path, dataset_dir: Path) -> None:
    reordered = dict(reversed(list(CONFIG.items())))
    trainer = _trainer(tmp_path)
    assert trainer.train(dataset_dir, CONFIG) == trainer.train(dataset_dir, reordered)


def test_dataset_digest_rejects_missing_and_empty_dirs(tmp_path: Path) -> None:
    with pytest.raises(TrainingError, match="not found"):
        dataset_digest(tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(TrainingError, match="empty"):
        dataset_digest(empty)


def test_canonical_config_rejects_unserializable_values(tmp_path: Path) -> None:
    with pytest.raises(TrainingError, match="not JSON-serializable"):
        canonical_config({"path": tmp_path})
    with pytest.raises(TrainingError, match="not JSON-serializable"):
        canonical_config({"bad": float("nan")})


GOOD_HEADLINE: dict[str, Any] = {
    "map50_ball": 0.91,
    "map50_bat": 0.86,
    "map50_stumps": 0.97,
    "recall_ball_high_blur": 0.8,
}

GOOD_ROW: dict[str, Any] = {
    "label": "ball",
    "map50": 0.91,
    "precision": 0.9,
    "recall": 0.88,
    "support": 120,
}


def _report_kwargs(**overrides: Any) -> dict[str, Any]:
    """build_eval_report kwargs; ``headline``/``per_class``/``dataset_digest``
    overrides land inside the RunResult (per_class=None omits the key entirely)."""
    headline: dict[str, Any] = overrides.pop("headline", dict(GOOD_HEADLINE))
    report: dict[str, Any] = {}
    per_class = overrides.pop("per_class", [dict(GOOD_ROW)])
    if per_class is not None:
        report["per_class"] = per_class
    if "dataset_digest" in overrides:
        report["dataset_digest"] = overrides.pop("dataset_digest")
    kwargs: dict[str, Any] = {
        "model_name": "ball-detector",
        "dataset_version": "v1",
        "trainer_version": "fake-trainer-1",
        "config": CONFIG,
        "result": RunResult(metrics=headline, weights_path=Path("weights.pt"), report=report),
    }
    kwargs.update(overrides)
    return kwargs


def test_build_eval_report_echoes_inputs(tmp_path: Path) -> None:
    report = build_eval_report(**_report_kwargs(dataset_digest="abc123"))
    assert report["schema_version"] == REPORT_SCHEMA_VERSION == 1
    assert report["model_name"] == "ball-detector"
    assert report["dataset_version"] == "v1"
    assert report["dataset_digest"] == "abc123"
    assert report["trainer_version"] == "fake-trainer-1"
    assert report["config"] == CONFIG
    assert report["headline"]["map50_ball"] == 0.91
    assert report["per_class"][0]["support"] == 120


def test_build_eval_report_defaults_dataset_digest_to_none() -> None:
    assert build_eval_report(**_report_kwargs())["dataset_digest"] is None


def test_build_eval_report_keeps_extra_headline_metrics() -> None:
    headline: dict[str, Any] = {**GOOD_HEADLINE, "map50_feet": 0.7}
    report = build_eval_report(**_report_kwargs(headline=headline))
    assert report["headline"]["map50_feet"] == 0.7


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"model_name": "  "}, "model_name"),
        ({"dataset_version": ""}, "dataset_version"),
        ({"trainer_version": " "}, "trainer_version"),
        ({"headline": {"map50_ball": 0.9}}, "missing pinned keys"),
        ({"per_class": None}, "missing 'per_class' rows"),
        ({"per_class": []}, "at least one row"),
    ],
)
def test_build_eval_report_rejects_incomplete_inputs(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(TrainingError, match=match):
        build_eval_report(**_report_kwargs(**overrides))


@pytest.mark.parametrize("bad_value", [1.5, -0.1, float("nan"), float("inf"), True, "0.9", None])
def test_build_eval_report_rejects_non_fraction_headline_values(bad_value: Any) -> None:
    headline: dict[str, Any] = {**GOOD_HEADLINE, "map50_ball": bad_value}
    with pytest.raises(TrainingError, match="finite number within"):
        build_eval_report(**_report_kwargs(headline=headline))


@pytest.mark.parametrize(
    ("row", "match"),
    [
        (
            {"label": "ball", "map50": 0.9, "precision": 0.9, "recall": 0.9},
            "missing keys",
        ),
        (
            {
                "label": "ball",
                "map50": 0.9,
                "precision": 0.9,
                "recall": 0.9,
                "support": 10,
                "f1": 0.9,
            },
            "unknown keys",
        ),
        (
            {"label": " ", "map50": 0.9, "precision": 0.9, "recall": 0.9, "support": 10},
            "label must be",
        ),
        (
            {"label": 3, "map50": 0.9, "precision": 0.9, "recall": 0.9, "support": 10},
            "label must be",
        ),
        (
            {"label": "ball", "map50": 0.9, "precision": 0.9, "recall": 0.9, "support": -1},
            "support must be",
        ),
        (
            {"label": "ball", "map50": 0.9, "precision": 0.9, "recall": 0.9, "support": True},
            "support must be",
        ),
        (
            {"label": "ball", "map50": 1.2, "precision": 0.9, "recall": 0.9, "support": 10},
            "map50",
        ),
    ],
)
def test_build_eval_report_rejects_bad_class_rows(row: dict[str, Any], match: str) -> None:
    with pytest.raises(TrainingError, match=match):
        build_eval_report(**_report_kwargs(per_class=[row]))


def test_run_result_is_frozen(tmp_path: Path, dataset_dir: Path) -> None:
    result = _trainer(tmp_path).train(dataset_dir, CONFIG)
    assert isinstance(result, RunResult)
    with pytest.raises(AttributeError):
        result.metrics = {}  # type: ignore[misc]
