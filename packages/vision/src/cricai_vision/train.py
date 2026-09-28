"""Detector training protocol, deterministic fake trainer, eval-report builder (US-F2).

The training pipeline (``scripts/train_detector.py`` -> ``cricai_worker.train_detector``)
depends on :class:`TrainerProtocol`, never on a concrete trainer, so tests and the
CI smoke path run on :class:`FakeTrainer` — seeded and fully deterministic: its
metrics are a pure function of (seed, dataset digest, canonical config), so the
same dataset + config always reproduces the same run while any change to either
input changes the numbers. A real ultralytics trainer slots in behind the same
protocol at the real-footage milestone.

The eval-report artifact (:func:`build_eval_report`) is the pinned US-F2 contract:
headline metrics use exactly the keys in :data:`HEADLINE_METRIC_KEYS` (the
promotion gate in the models API compares these keys, 0-1 scaled), per-class rows
carry map50/precision/recall/support, and the dataset version + full config are
echoed so every production model is reproducible from its report alone (US-F2 AC).

The pinned key sets are the DETECTOR defaults. Other model types (US-I6: the
``legspin-variation`` classifier) reuse the same artifact machinery by passing
their own ``headline_keys`` / ``class_fraction_keys`` / ``extra_report_keys``
to :func:`build_eval_report`; calls that pass nothing produce byte-identical
detector artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

#: Pinned headline metric keys (cross-group contract #4): eval reports and the
#: promotion regression gate use these exact names. Values are 0-1 fractions.
HEADLINE_METRIC_KEYS: tuple[str, ...] = (
    "map50_ball",
    "map50_bat",
    "map50_stumps",
    "recall_ball_high_blur",
)

#: Detector classes the fake trainer reports per-class rows for.
REPORT_CLASSES: tuple[str, ...] = ("ball", "bat", "stumps")

#: Fraction-valued keys of one DETECTOR per-class row (``label``/``support``
#: are always required); classifier model types pass their own set (US-I6).
CLASS_ROW_FRACTION_KEYS: tuple[str, ...] = ("map50", "precision", "recall")

#: Allowed keys of one per-class eval row (tight: the artifact is a pinned contract).
CLASS_ROW_KEYS: frozenset[str] = frozenset({"label", "support", *CLASS_ROW_FRACTION_KEYS})

REPORT_SCHEMA_VERSION = 1


class TrainingError(ValueError):
    """Raised for invalid training inputs (bad dataset dir, config, report shape)."""


@dataclass(frozen=True)
class RunResult:
    """What one training run produced.

    ``metrics`` are the headline metrics (0-1, pinned keys); ``report`` is the
    trainer's eval detail and must contain ``per_class`` rows (and may carry
    ``dataset_digest``) — :func:`build_eval_report` assembles the final artifact.
    """

    metrics: dict[str, float]
    weights_path: Path
    report: dict[str, Any]


class TrainerProtocol(Protocol):
    """Trains a detector on one dataset directory and evaluates it."""

    #: Identifier recorded on the model_runs row (trainer_version provenance).
    version: str

    def train(self, dataset_dir: Path, config: Mapping[str, Any]) -> RunResult:
        """Train + eval; deterministic for a fixed (dataset_dir, config)."""
        ...


def canonical_config(config: Mapping[str, Any]) -> str:
    """Order-independent JSON form of a config (digest + echo material)."""
    try:
        return json.dumps(dict(config), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TrainingError(f"config is not JSON-serializable: {exc}") from exc


def dataset_digest(dataset_dir: Path) -> str:
    """SHA-256 over every file's relative path + bytes: pins dataset membership."""
    if not dataset_dir.is_dir():
        raise TrainingError(f"dataset directory not found: {dataset_dir}")
    files = sorted(path for path in dataset_dir.rglob("*") if path.is_file())
    if not files:
        raise TrainingError(f"dataset directory is empty: {dataset_dir}")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(dataset_dir).as_posix().encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def _require(condition: bool, problem: str) -> None:
    if not condition:
        raise TrainingError(problem)


def _require_fraction(name: str, value: Any) -> float:
    """A metric value must be a finite number within [0, 1] (0-1 scale contract)."""
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    _require(
        is_number and math.isfinite(value) and 0.0 <= value <= 1.0,
        f"{name} must be a finite number within [0, 1], got {value!r}",
    )
    return float(value)


def _validated_class_row(row: Mapping[str, Any], fraction_keys: Sequence[str]) -> dict[str, Any]:
    allowed = {"label", "support", *fraction_keys}
    unknown = sorted(set(row) - allowed)
    _require(not unknown, f"per-class row has unknown keys {unknown}")
    missing = sorted(allowed - set(row))
    _require(not missing, f"per-class row missing keys {missing}")
    label = row["label"]
    _require(
        isinstance(label, str) and bool(label.strip()),
        f"per-class label must be a non-empty string, got {label!r}",
    )
    support = row["support"]
    _require(
        isinstance(support, int) and not isinstance(support, bool) and support >= 0,
        f"per-class support must be a non-negative int, got {support!r}",
    )
    return {
        "label": label,
        **{
            key: _require_fraction(f"per_class[{label!r}].{key}", row[key]) for key in fraction_keys
        },
        "support": support,
    }


def build_eval_report(  # noqa: PLR0913  (public seam: model types pass optional key sets, US-I6)
    *,
    model_name: str,
    dataset_version: str,
    trainer_version: str,
    config: Mapping[str, Any],
    result: RunResult,
    headline_keys: Sequence[str] = HEADLINE_METRIC_KEYS,
    class_fraction_keys: Sequence[str] = CLASS_ROW_FRACTION_KEYS,
    extra_report_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble + validate the eval-report artifact from one run's result (US-F2
    AC: every production model has dataset version, config, metrics and this
    report). ``result.metrics`` must carry every key in ``headline_keys`` and
    ``result.report`` the ``per_class`` rows (fraction columns per
    ``class_fraction_keys``).

    The defaults are the pinned detector contract; a classifier model type
    (US-I6) passes its own key sets, plus ``extra_report_keys`` naming report
    detail (e.g. ``confusion_matrix``) copied verbatim into the artifact —
    a named extra missing from the trainer report fails loudly.
    """
    for name, value in (
        ("model_name", model_name),
        ("dataset_version", dataset_version),
        ("trainer_version", trainer_version),
    ):
        _require(bool(value.strip()), f"{name} must be a non-empty string")
    headline = result.metrics
    missing = [key for key in headline_keys if key not in headline]
    _require(not missing, f"headline metrics missing pinned keys {missing} (US-F2 contract)")
    validated_headline = {
        key: _require_fraction(f"headline[{key!r}]", value) for key, value in headline.items()
    }
    per_class = result.report.get("per_class")
    if per_class is None:
        raise TrainingError("trainer report is missing 'per_class' rows (US-F2 report contract)")
    _require(bool(per_class), "per_class must contain at least one row")
    artifact: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "model_name": model_name,
        "dataset_version": dataset_version,
        "dataset_digest": result.report.get("dataset_digest"),
        "trainer_version": trainer_version,
        "config": dict(config),  # config echo: the run is reproducible from the report
        "headline": validated_headline,
        "per_class": [_validated_class_row(row, class_fraction_keys) for row in per_class],
    }
    for key in extra_report_keys:
        _require(key not in artifact, f"extra report key {key!r} collides with a pinned key")
        _require(key in result.report, f"trainer report is missing extra key {key!r}")
        artifact[key] = result.report[key]
    return artifact


@dataclass
class FakeTrainer:
    """Seeded deterministic trainer — the only trainer tests (and CI smoke) use.

    Metrics are derived from ``sha256(version | seed | dataset_digest | config)``
    and mapped into [0.75, 0.95], so identical inputs reproduce identical runs
    and any dataset/config change moves the numbers. Weights are a small
    deterministic artifact written under ``output_dir``.
    """

    output_dir: Path
    seed: int = 7
    version: str = "fake-trainer-1"

    def train(self, dataset_dir: Path, config: Mapping[str, Any]) -> RunResult:
        digest = dataset_digest(dataset_dir)
        material = f"{self.version}|{self.seed}|{digest}|{canonical_config(config)}"
        run_digest = hashlib.sha256(material.encode()).hexdigest()
        raw = bytes.fromhex(run_digest)

        def metric(index: int) -> float:
            return round(0.75 + raw[index] / 255 * 0.2, 4)

        headline = {key: metric(index) for index, key in enumerate(HEADLINE_METRIC_KEYS)}
        per_class: list[dict[str, Any]] = [
            {
                "label": label,
                "map50": headline[f"map50_{label}"],
                "precision": metric(4 + 2 * index),
                "recall": metric(5 + 2 * index),
                "support": 40 + raw[10 + index],
            }
            for index, label in enumerate(REPORT_CLASSES)
        ]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        weights_path = self.output_dir / f"weights-{run_digest[:12]}.pt"
        weights_path.write_bytes(f"fake-weights {run_digest}\n".encode())
        return RunResult(
            metrics=headline,
            weights_path=weights_path,
            report={"per_class": per_class, "dataset_digest": digest},
        )
