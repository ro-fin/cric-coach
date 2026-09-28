"""US-F2 training job + CLI tests: run recording, artifacts, candidate registration.

Unit tests run the job on in-memory SQLite with the deterministic FakeTrainer
(full line+branch coverage of the job; no real training ever). The CLI is
exercised the same way ``scripts/detect_events.py`` is: validation failures via
subprocess without a database, the happy path end-to-end against temp-Postgres
(integration-marked), including the loud already-registered re-run failure.
"""

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import create_all, make_engine, make_session_factory, session_scope
from cricai_data.enums import ModelStage, TrainingStatus
from cricai_data.models import Dataset, ModelRun, ModelVersion
from cricai_data.storage import FsObjectStore
from cricai_vision.train import (
    HEADLINE_METRIC_KEYS,
    FakeTrainer,
    RunResult,
    TrainingError,
    dataset_digest,
)
from cricai_worker.context import ENV_DATABASE_URL, ENV_STORAGE_ROOT, WorkerContext
from cricai_worker.train_detector import TrainingSpec, run_key, run_training
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "train_detector.py"

CONFIG = {"imgsz": 640, "epochs": 50}

GOOD_METRICS = dict.fromkeys(HEADLINE_METRIC_KEYS, 0.9)


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _data_yaml(version: str = "v1", digest: str = "a" * 64) -> str:
    """The provenance header scripts/export_dataset.py writes, plus a body line."""
    return f"# cricAI dataset {version} (manifest digest {digest})\npath: .\n"


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    root = tmp_path / "dataset-v1"
    (root / "images").mkdir(parents=True)
    (root / "images" / "f0001.jpg").write_bytes(b"jpeg-bytes")
    (root / "labels.txt").write_text("0 0.5 0.5 0.02 0.02\n")
    (root / "data.yaml").write_text(_data_yaml())
    return root


def _seed_dataset(ctx: WorkerContext, version: str = "v1", *, frozen: bool = True) -> None:
    with session_scope(ctx.session_factory) as db:
        db.add(Dataset(version=version, frozen=frozen, manifest_digest="a" * 64))


def _trainer(tmp_path: Path) -> FakeTrainer:
    return FakeTrainer(output_dir=tmp_path / "work")


def _train(ctx: WorkerContext, tmp_path: Path, dataset_dir: Path, **kwargs: Any) -> Any:
    trainer = kwargs.pop("trainer", _trainer(tmp_path))
    spec = TrainingSpec(
        model_name=kwargs.pop("model_name", "ball-detector"),
        dataset_version=kwargs.pop("dataset_version", "v1"),
        dataset_dir=dataset_dir,
        config=kwargs.pop("config", CONFIG),
        **kwargs,
    )
    return run_training(ctx, spec, trainer)


def _runs(ctx: WorkerContext) -> list[ModelRun]:
    with ctx.session_factory() as db:
        return list(db.scalars(select(ModelRun).order_by(ModelRun.created_at)))


def _versions(ctx: WorkerContext) -> list[ModelVersion]:
    with ctx.session_factory() as db:
        return list(db.scalars(select(ModelVersion).order_by(ModelVersion.created_at)))


@dataclass
class _ExplodingTrainer:
    """A trainer that dies mid-train (GPU on fire, OOM, ...)."""

    version: str = "boom-1"

    def train(self, dataset_dir: Path, config: Any) -> RunResult:
        raise RuntimeError(f"gpu on fire training {dataset_dir.name} with {len(config)} options")


@dataclass
class _NoPerClassTrainer:
    """Violates the RunResult report contract: no per_class rows."""

    weights: Path
    version: str = "bad-report-1"

    def train(self, dataset_dir: Path, config: Any) -> RunResult:
        self.weights.write_bytes(dataset_dir.name.encode() + str(len(config)).encode())
        return RunResult(metrics=dict(GOOD_METRICS), weights_path=self.weights, report={})


@dataclass
class _StatusProbeTrainer:
    """Records the run's committed status as seen mid-train (crash-safety proof)."""

    ctx: WorkerContext
    inner: FakeTrainer
    observed: list[TrainingStatus] = field(default_factory=list)
    version: str = "fake-trainer-1"

    def train(self, dataset_dir: Path, config: Any) -> RunResult:
        with self.ctx.session_factory() as db:
            run = db.scalars(select(ModelRun)).one()
            self.observed.append(run.status)
            assert run.started_at is not None
        return self.inner.train(dataset_dir, config)


@dataclass
class _RacingRegistrationTrainer:
    """Registers the run's own (model_name, version) identity mid-train: the
    TOCTOU window between the pre-check and the final registration commit."""

    ctx: WorkerContext
    inner: FakeTrainer
    identity: tuple[str, str] = ("ball-detector", "1.2.0")
    version: str = "fake-trainer-1"

    def train(self, dataset_dir: Path, config: Any) -> RunResult:
        model_name, model_version = self.identity
        with session_scope(self.ctx.session_factory) as db:
            run = db.scalars(select(ModelRun)).one()
            db.add(
                ModelVersion(
                    model_name=model_name,
                    version=model_version,
                    run_id=run.id,
                    stage=ModelStage.CANDIDATE,
                )
            )
        return self.inner.train(dataset_dir, config)


def test_happy_path_records_run_artifacts_and_candidate(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    summary = _train(ctx, tmp_path, dataset_dir)

    assert summary.status == "succeeded"
    assert summary.model_name == "ball-detector"
    assert summary.dataset_version == "v1"
    assert set(summary.metrics) == set(HEADLINE_METRIC_KEYS)

    (run,) = _runs(ctx)
    assert str(run.id) == summary.run_id
    assert summary.model_version == run.id.hex[:12]  # derived default
    assert run.status is TrainingStatus.SUCCEEDED
    assert run.metrics == summary.metrics
    assert run.config == CONFIG
    assert run.trainer_version == "fake-trainer-1"
    assert run.started_at is not None and run.finished_at is not None
    assert run.report_key == summary.report_key

    (version,) = _versions(ctx)
    assert version.stage is ModelStage.CANDIDATE
    assert version.run_id == run.id
    assert (version.model_name, version.version) == ("ball-detector", summary.model_version)

    assert summary.report_key == run_key("ball-detector", run.id, "eval-report.json")
    report = json.loads(ctx.store.get(summary.report_key))
    assert report["schema_version"] == 1
    assert report["model_name"] == "ball-detector"
    assert report["dataset_version"] == "v1"
    assert report["dataset_digest"] == dataset_digest(dataset_dir)
    assert report["trainer_version"] == "fake-trainer-1"
    assert report["config"] == CONFIG  # config echo: reproducible from the report
    assert report["headline"] == summary.metrics
    assert [row["label"] for row in report["per_class"]] == ["ball", "bat", "stumps"]
    assert report["artifacts"] == {"weights_key": summary.weights_key}
    assert ctx.store.get(summary.weights_key).startswith(b"fake-weights ")


def test_explicit_model_version_is_registered(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    summary = _train(ctx, tmp_path, dataset_dir, model_version="1.2.0")
    assert summary.model_version == "1.2.0"
    assert _versions(ctx)[0].version == "1.2.0"


def test_reruns_append_new_runs_never_mutate(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    first = _train(ctx, tmp_path, dataset_dir)
    second = _train(ctx, tmp_path, dataset_dir)
    assert first.run_id != second.run_id
    assert first.model_version != second.model_version
    assert first.metrics == second.metrics  # same dataset + config: reproducible
    assert len(_runs(ctx)) == 2
    assert len(_versions(ctx)) == 2


def test_unknown_dataset_version_writes_nothing(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    with pytest.raises(LookupError, match="dataset version not found: v1"):
        _train(ctx, tmp_path, dataset_dir)
    assert _runs(ctx) == []


def test_unfrozen_dataset_is_refused(ctx: WorkerContext, tmp_path: Path, dataset_dir: Path) -> None:
    _seed_dataset(ctx, frozen=False)
    with pytest.raises(TrainingError, match="not frozen"):
        _train(ctx, tmp_path, dataset_dir)
    assert _runs(ctx) == []


def test_duplicate_version_fails_fast_before_training(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    _train(ctx, tmp_path, dataset_dir, model_version="1.0.0")
    probe = _StatusProbeTrainer(ctx=ctx, inner=_trainer(tmp_path))
    with pytest.raises(TrainingError, match="already registered"):
        _train(ctx, tmp_path, dataset_dir, trainer=probe, model_version="1.0.0")
    assert probe.observed == []  # no training was wasted
    assert len(_runs(ctx)) == 1  # no second run row was even created


def test_version_registered_mid_train_marks_run_failed(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    """The duplicate-identity pre-check races the whole training duration; when the
    final registration commit collides, the run must land FAILED, never RUNNING."""
    _seed_dataset(ctx)
    trainer = _RacingRegistrationTrainer(ctx=ctx, inner=_trainer(tmp_path))
    with pytest.raises(TrainingError, match="registered while this run trained"):
        _train(ctx, tmp_path, dataset_dir, trainer=trainer, model_version="1.2.0")
    (run,) = _runs(ctx)
    assert run.status is TrainingStatus.FAILED  # honest history: never stranded RUNNING
    assert run.finished_at is not None
    assert run.metrics == {} and run.report_key is None  # the success commit rolled back
    (squatter,) = _versions(ctx)  # only the mid-train registration survives
    assert (squatter.version, squatter.stage) == ("1.2.0", ModelStage.CANDIDATE)


# --- dataset_dir <-> dataset_version provenance (US-F2: train on what you record) ---


def test_dataset_dir_version_mismatch_is_refused(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    """dataset_dir exporting a different version than the one recorded is refused
    before any training or run row (the classic --dataset-dir tab-completion slip)."""
    _seed_dataset(ctx, version="v2")
    with pytest.raises(TrainingError, match="exports dataset 'v1', not 'v2'"):
        _train(ctx, tmp_path, dataset_dir, dataset_version="v2")
    assert _runs(ctx) == []


def test_dataset_dir_digest_mismatch_is_refused(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)  # stored manifest_digest is 'a' * 64
    (dataset_dir / "data.yaml").write_text(_data_yaml(digest="b" * 64))
    with pytest.raises(TrainingError, match="manifest digest"):
        _train(ctx, tmp_path, dataset_dir)
    assert _runs(ctx) == []


def test_dataset_dir_without_data_yaml_is_refused(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    (dataset_dir / "data.yaml").unlink()
    with pytest.raises(TrainingError, match="cannot read"):
        _train(ctx, tmp_path, dataset_dir)
    assert _runs(ctx) == []


@pytest.mark.parametrize("content", ["", "path: images\n"])
def test_dataset_dir_without_export_header_is_refused(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path, content: str
) -> None:
    _seed_dataset(ctx)
    (dataset_dir / "data.yaml").write_text(content)
    with pytest.raises(TrainingError, match="no cricAI export header"):
        _train(ctx, tmp_path, dataset_dir)
    assert _runs(ctx) == []


def test_dataset_without_digest_matches_empty_header_digest(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    """A frozen legacy dataset without a stored digest exports an empty digest;
    the provenance check compares like for like (mirrors export_dataset)."""
    with session_scope(ctx.session_factory) as db:
        db.add(Dataset(version="v9", frozen=True, manifest_digest=None))
    (dataset_dir / "data.yaml").write_text(_data_yaml(version="v9", digest=""))
    summary = _train(ctx, tmp_path, dataset_dir, dataset_version="v9")
    assert summary.status == "succeeded"


def test_whitespace_dataset_version_round_trips_through_header(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    """Dataset versions are free-form strings (the datasets API imposes no charset),
    so a version containing whitespace — which the export writes verbatim — must
    parse back out of the header instead of being refused as headerless."""
    _seed_dataset(ctx, version="release 2026 07")
    (dataset_dir / "data.yaml").write_text(_data_yaml(version="release 2026 07"))
    summary = _train(ctx, tmp_path, dataset_dir, dataset_version="release 2026 07")
    assert summary.status == "succeeded"
    assert summary.dataset_version == "release 2026 07"


def test_non_utf8_data_yaml_is_refused_loudly(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    """A corrupt (non-UTF-8) data.yaml must take the same loud TrainingError path
    as an unreadable one — never escape as a raw UnicodeDecodeError traceback."""
    _seed_dataset(ctx)
    (dataset_dir / "data.yaml").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(TrainingError, match="cannot read"):
        _train(ctx, tmp_path, dataset_dir)
    assert _runs(ctx) == []


def test_path_unsafe_model_name_is_refused(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    with pytest.raises(TrainingError, match="model_name must match"):
        _train(ctx, tmp_path, dataset_dir, model_name="../evil")
    assert _runs(ctx) == []


def test_trainer_crash_marks_run_failed_and_registers_nothing(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    with pytest.raises(RuntimeError, match="gpu on fire"):
        _train(ctx, tmp_path, dataset_dir, trainer=_ExplodingTrainer())
    (run,) = _runs(ctx)
    assert run.status is TrainingStatus.FAILED  # honest history, never phantom success
    assert run.started_at is not None and run.finished_at is not None
    assert run.metrics == {} and run.report_key is None
    assert _versions(ctx) == []
    assert ctx.store.list_keys("models") == []


def test_report_contract_violation_marks_run_failed(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    _seed_dataset(ctx)
    trainer = _NoPerClassTrainer(weights=tmp_path / "w.pt")
    with pytest.raises(TrainingError, match="per_class"):
        _train(ctx, tmp_path, dataset_dir, trainer=trainer)
    (run,) = _runs(ctx)
    assert run.status is TrainingStatus.FAILED
    assert _versions(ctx) == []
    assert ctx.store.list_keys("models") == []  # nothing uploaded for a failed run


def test_status_transitions_commit_incrementally(
    ctx: WorkerContext, tmp_path: Path, dataset_dir: Path
) -> None:
    """Mid-train, a separate DB session already sees the committed RUNNING row."""
    _seed_dataset(ctx)
    probe = _StatusProbeTrainer(ctx=ctx, inner=_trainer(tmp_path))
    _train(ctx, tmp_path, dataset_dir, trainer=probe)
    assert probe.observed == [TrainingStatus.RUNNING]


def test_run_key_layout() -> None:
    run_id = uuid.UUID("00000000-0000-0000-0000-000000000042")
    assert (
        run_key("ball-detector", run_id, "eval-report.json")
        == f"models/ball-detector/runs/{run_id}/eval-report.json"
    )


# --- CLI: validation failures need no database (mirrors detect_events.py tests) ---


def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


def _cli_args(dataset_dir: Path, **overrides: str) -> list[str]:
    options = {
        "--model-name": "ball-detector",
        "--dataset-version": "v1",
        "--dataset-dir": str(dataset_dir),
        **overrides,
    }
    return [part for key, value in options.items() for part in (key, value)]


def test_cli_rejects_missing_dataset_dir(tmp_path: Path) -> None:
    result = _run_cli(_cli_args(tmp_path / "nope"), {})
    assert result.returncode == 2
    assert "dataset directory not found" in result.stderr


def test_cli_rejects_invalid_config_json(tmp_path: Path, dataset_dir: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text("{not json")
    result = _run_cli(_cli_args(dataset_dir, **{"--config": str(config)}), {})
    assert result.returncode == 2
    assert "invalid JSON" in result.stderr


def test_cli_rejects_non_object_config(tmp_path: Path, dataset_dir: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text("[1, 2, 3]")
    result = _run_cli(_cli_args(dataset_dir, **{"--config": str(config)}), {})
    assert result.returncode == 2
    assert "must be a JSON object" in result.stderr


def test_cli_rejects_unreadable_config(tmp_path: Path, dataset_dir: Path) -> None:
    result = _run_cli(_cli_args(dataset_dir, **{"--config": str(tmp_path / "nope.json")}), {})
    assert result.returncode == 2
    assert "cannot read" in result.stderr


@pytest.mark.parametrize("key", ["seed", "trainer"])
def test_cli_rejects_reserved_provenance_keys_in_config(
    tmp_path: Path, dataset_dir: Path, key: str
) -> None:
    """A config-file 'seed'/'trainer' would be recorded as provenance while the
    trainer runs off the CLI flags — the conflict must be loud, not silent."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**CONFIG, key: 9}))
    result = _run_cli(_cli_args(dataset_dir, **{"--config": str(config)}), {})
    assert result.returncode == 2
    assert "reserved" in result.stderr
    assert key in result.stderr


# --- temp-Postgres integration: the CLI end to end, twice (loud re-run failure) ---


@pytest.mark.integration
def test_cli_end_to_end_records_candidate(pg_url: str, tmp_path: Path, dataset_dir: Path) -> None:
    engine: Engine = make_engine(pg_url)
    create_all(engine)
    ctx = WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )
    _seed_dataset(ctx)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG))
    env = {ENV_DATABASE_URL: pg_url, ENV_STORAGE_ROOT: str(tmp_path / "store")}
    args = _cli_args(
        dataset_dir,
        **{
            "--config": str(config),
            "--model-version": "1.0.0",
            "--work-dir": str(tmp_path / "work"),
        },
    )

    first = _run_cli(args, env)
    assert first.returncode == 0, first.stderr
    assert "ball-detector 1.0.0 (succeeded, candidate) on dataset v1" in first.stdout
    assert "map50_ball=" in first.stdout

    (run,) = _runs(ctx)
    assert run.status is TrainingStatus.SUCCEEDED
    assert run.config["trainer"] == "fake"  # the CLI records its own provenance keys
    assert run.config["imgsz"] == 640
    (version,) = _versions(ctx)
    assert (version.version, version.stage) == ("1.0.0", ModelStage.CANDIDATE)
    assert run.report_key is not None
    assert ctx.store.exists(run.report_key)

    second = _run_cli(args, env)  # same registry identity: loud failure, no mutation
    assert second.returncode == 2
    assert "already registered" in second.stderr
    assert len(_runs(ctx)) == 1
    engine.dispose()
