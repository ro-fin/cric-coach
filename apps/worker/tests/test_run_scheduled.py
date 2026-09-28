"""Cron dispatcher unit tests (Phase-7 fx1, finding 36; US-J4/G5/L4/J5).

``scripts/run_scheduled.py`` is the SOLE production cron entrypoint the deploy
runbook wires (docs/runbooks/deploy_lan.md §5) — before these tests no gate
executed a single line of it. The dispatch runs each mode against a REAL
:class:`WorkerContext` over SQLite (the scheduled jobs are engine-agnostic, as
apps/worker/tests/test_scheduled_jobs.py already proves), plus argparse-edge
tests: ``--as-of`` pass-through, the review-sweep ``--as-of`` refusal, and the
bad-mode exit code.

``scripts`` is not an importable package, so the module is loaded by path
(the repo's established pattern, see apps/api/tests/test_verify_deploy.py).
"""

import importlib.util
import sys
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.models import Player
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

_REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "run_scheduled", _REPO_ROOT / "scripts" / "run_scheduled.py"
)
assert _spec is not None and _spec.loader is not None
run_scheduled = importlib.util.module_from_spec(_spec)
sys.modules["run_scheduled"] = run_scheduled
_spec.loader.exec_module(run_scheduled)

AS_OF = date(2026, 6, 17)  # a Wednesday


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


def _add_player(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20), is_guest=False)
        db.add(player)
        db.commit()
        return player.id


# --------------------------------------------------------------- dispatch


def test_nightly_runs_the_real_baselines_job(
    ctx: WorkerContext, capsys: pytest.CaptureFixture[str]
) -> None:
    """US-J4: `nightly` dispatches to run_nightly with the provided ctx."""
    _add_player(ctx)
    assert run_scheduled.main(["nightly"], ctx=ctx) == 0
    assert "nightly: baselines recomputed for 1 player(s)" in capsys.readouterr().out


def test_weekly_runs_drift_then_rollups(
    ctx: WorkerContext, capsys: pytest.CaptureFixture[str]
) -> None:
    """US-L4 + US-G5: `weekly` dispatches to run_weekly (drift week named)."""
    assert run_scheduled.main(["weekly", "--as-of", AS_OF.isoformat()], ctx=ctx) == 0
    out = capsys.readouterr().out
    assert "weekly: drift week" in out
    assert "0 player rollup(s)" in out and "0 skipped" in out


def test_review_sweep_runs_the_timeout_sweep(
    ctx: WorkerContext, capsys: pytest.CaptureFixture[str]
) -> None:
    """US-J5: `review-sweep` dispatches to run_review_sweep."""
    assert run_scheduled.main(["review-sweep"], ctx=ctx) == 0
    assert "review-sweep: 0 due, 0 published, 0 blocked" in capsys.readouterr().out


# ----------------------------------------------------------- --as-of edges


def test_as_of_passes_through_to_nightly(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded: dict[str, object] = {}

    def fake_nightly(context: WorkerContext, *, as_of: date | None = None) -> list[object]:
        recorded["ctx"] = context
        recorded["as_of"] = as_of
        return []

    monkeypatch.setattr(run_scheduled, "run_nightly", fake_nightly)
    assert run_scheduled.main(["nightly", "--as-of", "2026-06-17"], ctx=ctx) == 0
    assert recorded == {"ctx": ctx, "as_of": AS_OF}
    capsys.readouterr()


def test_as_of_passes_through_to_weekly(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The weekly branch must FORWARD --as-of to run_weekly. A dropped or
    hardcoded as_of would silently roll up the wrong week on the runbook's
    downtime catch-up (nightly --as-of DATE, then weekly) — the silent-cron
    regression class of finding 36. Mirrors the nightly pass-through test."""
    recorded: dict[str, object] = {}

    def fake_weekly(context: WorkerContext, *, as_of: date | None = None) -> object:
        recorded["ctx"] = context
        recorded["as_of"] = as_of
        return SimpleNamespace(
            drift=SimpleNamespace(week_start=date(2026, 6, 15)),
            rollups=SimpleNamespace(summaries=[], skipped=[]),
        )

    monkeypatch.setattr(run_scheduled, "run_weekly", fake_weekly)
    assert run_scheduled.main(["weekly", "--as-of", "2026-06-17"], ctx=ctx) == 0
    assert recorded == {"ctx": ctx, "as_of": AS_OF}
    capsys.readouterr()


def test_as_of_defaults_to_none(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded: dict[str, object] = {"as_of": "unset"}

    def fake_nightly(context: WorkerContext, *, as_of: date | None = None) -> list[object]:
        recorded["as_of"] = as_of
        return []

    monkeypatch.setattr(run_scheduled, "run_nightly", fake_nightly)
    assert run_scheduled.main(["nightly"], ctx=ctx) == 0
    assert recorded["as_of"] is None
    capsys.readouterr()


def test_review_sweep_refuses_as_of(ctx: WorkerContext, capsys: pytest.CaptureFixture[str]) -> None:
    """The sweep sweeps what is due NOW — a re-anchored sweep would publish
    drafts whose review window has not really expired."""
    with pytest.raises(SystemExit) as excinfo:
        run_scheduled.main(["review-sweep", "--as-of", "2026-06-17"], ctx=ctx)
    assert excinfo.value.code == 2
    assert "--as-of does not apply to review-sweep" in capsys.readouterr().err


def test_malformed_as_of_exits_two(ctx: WorkerContext) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_scheduled.main(["nightly", "--as-of", "not-a-date"], ctx=ctx)
    assert excinfo.value.code == 2


# -------------------------------------------------------------- bad inputs


def test_unknown_job_exits_two(ctx: WorkerContext) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_scheduled.main(["hourly"], ctx=ctx)
    assert excinfo.value.code == 2


def test_missing_job_exits_two(ctx: WorkerContext) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_scheduled.main([], ctx=ctx)
    assert excinfo.value.code == 2


# ------------------------------------------------------------- env context


def test_default_context_comes_from_env(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without an injected ctx the dispatcher builds WorkerContext.from_env()
    — the cron lines rely on deploy/.env having been sourced (runbook §5)."""
    monkeypatch.setattr(run_scheduled.WorkerContext, "from_env", classmethod(lambda _cls: ctx))
    assert run_scheduled.main(["nightly"]) == 0
    assert "nightly: baselines recomputed for 0 player(s)" in capsys.readouterr().out
