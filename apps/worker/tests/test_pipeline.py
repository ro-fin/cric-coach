"""US-J1/US-L1 DAG runner unit tests: topology, retries, resume, degraded paths.

In-memory SQLite exercises every line/branch of the runner; fake stage
callables are injected through the ``registry`` seam as ``module:function``
dotted paths (resolved via importlib), so no real stage module is imported.
Real PostgreSQL contention, the golden stage sequence and the production
dotted-path wiring live in ``test_pipeline_pg_integration.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

import pytest
from cricai_data.db import create_all
from cricai_data.enums import BowlerSource, SessionType, StageStatus, VideoStatus
from cricai_data.models import PipelineRun, PipelineStage, Player, Video
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker import pipeline as pl
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import (
    LOCKED,
    STAGES,
    AdvisoryLock,
    PipelineOutcome,
    PipelineTopologyError,
    RunPolicy,
    advisory_lock_key,
    context_engine,
    payload_digest,
    resolve_stage,
    run_pipeline,
    run_session_pipeline_job,
    stage_input_digest,
    validate_topology,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# --------------------------------------------------------------- fake stages
# Referenced by dotted path as ``{__name__}:name``; the module is already in
# sys.modules, so resolve_stage re-imports it and grabs these callables.

_CALLS: dict[str, int] = {}


def _bump(key: str) -> int:
    _CALLS[key] = _CALLS.get(key, 0) + 1
    return _CALLS[key]


def ok_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    return {"ok": True}


def none_stage(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    """A stage that produces no payload (runner normalizes ``None`` to ``{}``)."""


def boom_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    raise RuntimeError("stage exploded")


def counting_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Payload changes every invocation — a stale replay is thus detectable."""
    return {"count": _bump("counting")}


def unserializable_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Returns a payload the JSON trace column cannot store (raises at commit)."""
    return {"when": datetime(2026, 7, 7, 12, 0, 0)}


def flaky_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Fails on its very first invocation, succeeds on every later one."""
    n = _bump("flaky")
    if n == 1:
        raise ValueError("transient failure")
    return {"attempt": n}


NOT_A_CALLABLE = "definitely not callable"


@pytest.fixture(autouse=True)
def _reset_calls() -> Iterator[None]:
    _CALLS.clear()
    yield
    _CALLS.clear()


# --------------------------------------------------------------- db fixtures


@pytest.fixture
def ctx(tmp_path: Any) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        store=FsObjectStore(tmp_path / "store"),
    )


@pytest.fixture
def session_id(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.commit()
        return session.id


def path(name: str) -> str:
    return f"{__name__}:{name}"


def all_ok_registry(**overrides: str) -> dict[str, str]:
    registry = {stage: path("ok_stage") for stage in STAGES}
    registry.update(overrides)
    return registry


def stage_rows(ctx: WorkerContext, run_id: uuid.UUID) -> list[PipelineStage]:
    with ctx.session_factory() as db:
        return list(db.scalars(select(PipelineStage).where(PipelineStage.run_id == run_id)).all())


# ---------------------------------------------------------------- topology


@pytest.mark.safety
def test_default_topology_is_valid() -> None:
    validate_topology()  # the shipped DAG must always pass


@pytest.mark.safety
@pytest.mark.parametrize(
    ("stages", "deps", "match"),
    [
        (("a", "a"), {"a": ()}, "duplicate stage names"),
        (("a", "b"), {"a": ()}, "different stage vocabularies"),
        (("a", "b"), {"a": ("b",), "b": ()}, "does not run before it"),
        (("report", "safety"), {"report": (), "safety": ()}, "safety must be the last"),
        (("safety", "report"), {"safety": (), "report": ()}, "must hard-depend on safety"),
    ],
)
def test_corrupted_topologies_are_rejected(
    stages: tuple[str, ...], deps: dict[str, tuple[str, ...]], match: str
) -> None:
    with pytest.raises(PipelineTopologyError, match=match):
        validate_topology(stages, deps)


@pytest.mark.safety
def test_safety_is_last_stage_before_report() -> None:
    """US-H5 supremacy: the sequence pins safety immediately before report."""
    assert STAGES[-2:] == ("safety", "report")
    assert "safety" in pl.STAGE_DEPS["report"]


@pytest.mark.safety
def test_db_state_reading_stages_are_pinned_non_resumable() -> None:
    """Every DB-state-reading stage must never digest-resume: probe (video
    rows carry the byte identity the resume currency hangs on), the bowling
    metric stages (bounce marks / targets / delivery labels are coach-mutable
    between runs, US-I2-I6) and the agent stages (a re-run after a pain
    check-in / ledger / rule change must re-validate and regenerate, never
    replay a stale verdict or report, US-H5)."""
    expected = {
        "probe",
        "bowling_action",
        "bowling_flight",
        "classify_variations",
        "analysis",
        "progress",
        "planner",
        "safety",
        "report",
    }
    assert frozenset(expected) == pl.NON_RESUMABLE


def test_bowling_stages_sit_between_metrics_and_analysis() -> None:
    """US-I2-I6 wiring (Phase-6 findings 0/45): the bowling stages are real
    DAG members at the metrics→analysis boundary, consuming the media work
    they need and never gating the analysis tail."""
    assert STAGES.index("metrics") < STAGES.index("bowling_action")
    assert STAGES.index("classify_variations") < STAGES.index("analysis")
    assert pl.STAGE_DEPS["bowling_action"] == ("pose",)
    assert pl.STAGE_DEPS["bowling_flight"] == ("detect_track",)
    assert pl.STAGE_DEPS["classify_variations"] == ("bowling_flight",)
    assert pl.STAGE_DEPS["analysis"] == ("metrics",)  # bowling failure degrades, never blocks


@pytest.mark.safety
@pytest.mark.parametrize(
    ("non_resumable", "match"),
    [
        (frozenset({"safety", "report", "bogus"}), "not in STAGES"),
        (frozenset({"report"}), "safety and report must be non-resumable"),
        (frozenset(), "safety and report must be non-resumable"),
    ],
)
def test_corrupted_non_resumable_sets_are_rejected(
    non_resumable: frozenset[str], match: str
) -> None:
    """A future edit must not be able to make safety or report resumable again."""
    with pytest.raises(PipelineTopologyError, match=match):
        validate_topology(STAGES, pl.STAGE_DEPS, non_resumable)


# ------------------------------------------------------------ small helpers


def test_advisory_lock_key_is_deterministic_signed_64bit() -> None:
    sid = uuid.uuid4()
    key = advisory_lock_key(sid)
    assert key == advisory_lock_key(sid)
    assert -(2**63) <= key < 2**63
    assert advisory_lock_key(uuid.uuid4()) != key


def test_backoff_is_capped() -> None:
    policy = RunPolicy(backoff_base_s=1.0, backoff_cap_s=8.0)
    assert policy.backoff_s(1) == 1.0
    assert policy.backoff_s(2) == 2.0
    assert policy.backoff_s(20) == 8.0  # capped


def test_payload_and_input_digests_are_stable() -> None:
    assert payload_digest({"b": 1, "a": 2}) == payload_digest({"a": 2, "b": 1})
    d1 = stage_input_digest("events", {"probe": "x"}, {"det": "v1"})
    assert d1 != stage_input_digest("events", {"probe": "x"}, {"det": "v2"})
    assert d1 != stage_input_digest("events", {"probe": "y"}, {"det": "v1"})


def test_resolve_stage_resolves_a_callable() -> None:
    assert resolve_stage(path("ok_stage")) is ok_stage


@pytest.mark.parametrize("bad", ["nocolon", ":func", "mod:"])
def test_resolve_stage_rejects_malformed_paths(bad: str) -> None:
    with pytest.raises(ValueError, match=r"package.module:function"):
        resolve_stage(bad)


def test_resolve_stage_rejects_missing_module_and_non_callable() -> None:
    with pytest.raises(ModuleNotFoundError):
        resolve_stage("cricai_worker.does_not_exist:fn")
    with pytest.raises(TypeError, match="non-callable"):
        resolve_stage(path("NOT_A_CALLABLE"))


def test_context_engine_returns_the_bound_engine(ctx: WorkerContext) -> None:
    assert context_engine(ctx) is ctx.session_factory.kw["bind"]


# --------------------------------------------------- AdvisoryLock (fake PG)


class _FakeResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar(self) -> int:
        return self._value


class _FakeConn:
    def __init__(self, granted: int) -> None:
        self.granted = granted
        self.closed = False
        self.statements: list[str] = []

    def execute(self, statement: Any, _params: dict[str, Any]) -> _FakeResult:
        self.statements.append(str(statement))
        return _FakeResult(self.granted)

    def close(self) -> None:
        self.closed = True


class _FakeDialect:
    name = "postgresql"


class _FakeEngine:
    def __init__(self, granted: int) -> None:
        self.dialect = _FakeDialect()
        self.conn = _FakeConn(granted)

    def connect(self) -> _FakeConn:
        return self.conn


def test_advisory_lock_is_noop_on_sqlite(ctx: WorkerContext) -> None:
    lock = AdvisoryLock(engine=context_engine(ctx), key=1)
    assert lock.acquire() is True
    lock.release()  # no connection was opened; must be a safe no-op


def test_advisory_lock_acquires_and_releases_on_postgres() -> None:
    engine = _FakeEngine(granted=1)
    lock = AdvisoryLock(engine=engine, key=99)  # type: ignore[arg-type]
    assert lock.acquire() is True
    lock.release()
    assert engine.conn.closed is True
    assert any("pg_advisory_unlock" in s for s in engine.conn.statements)


def test_advisory_lock_denied_when_held_on_postgres() -> None:
    engine = _FakeEngine(granted=0)
    lock = AdvisoryLock(engine=engine, key=99)  # type: ignore[arg-type]
    assert lock.acquire() is False
    assert engine.conn.closed is True  # the probe connection is returned


# ---------------------------------------------------------- happy-path run


def test_full_run_succeeds_and_records_every_stage(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    outcome = run_pipeline(ctx, session_id, registry=all_ok_registry())
    assert outcome.status == "succeeded"
    assert [s.stage for s in outcome.stages] == list(STAGES)
    assert all(s.status is StageStatus.SUCCEEDED for s in outcome.stages)
    assert all(not s.resumed for s in outcome.stages)
    rows = stage_rows(ctx, outcome.run_id)  # type: ignore[arg-type]
    assert len(rows) == len(STAGES)
    for row in rows:
        assert row.started_at is not None and row.finished_at is not None
        assert row.output is not None and row.output["digest"]


def test_none_payload_is_normalized(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    outcome = run_pipeline(ctx, session_id, registry=all_ok_registry(probe=path("none_stage")))
    probe = next(s for s in outcome.stages if s.stage == "probe")
    assert probe.status is StageStatus.SUCCEEDED
    assert probe.digest == payload_digest({})


def test_missing_session_raises_lookup_error(ctx: WorkerContext) -> None:
    with pytest.raises(LookupError, match="not found"):
        run_pipeline(ctx, uuid.uuid4(), registry=all_ok_registry())


# ----------------------------------------------- retries / degraded paths


def test_stage_crash_then_retry_then_succeed(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    calls: list[float] = []
    policy = RunPolicy(max_attempts=3, sleep=calls.append, backoff_base_s=0.01)
    outcome = run_pipeline(
        ctx, session_id, registry=all_ok_registry(probe=path("flaky_stage")), policy=policy
    )
    assert outcome.status == "succeeded"
    probe = next(s for s in outcome.stages if s.stage == "probe")
    assert probe.status is StageStatus.SUCCEEDED
    assert probe.attempts == 2  # failed once, succeeded on the retry
    assert len(calls) == 1  # one backoff sleep between the two attempts
    rows = [r for r in stage_rows(ctx, outcome.run_id) if r.stage == "probe"]  # type: ignore[arg-type]
    assert sorted(r.attempt for r in rows) == [1, 2]


def test_default_sleep_is_used_when_policy_sleep_is_none(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    # sleep left as None -> time.sleep(0.0); backoff_base_s 0 keeps it instant.
    policy = RunPolicy(max_attempts=2, backoff_base_s=0.0)
    outcome = run_pipeline(
        ctx, session_id, registry=all_ok_registry(probe=path("flaky_stage")), policy=policy
    )
    assert outcome.status == "succeeded"


def test_permanent_failure_yields_degraded_but_honest_run(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    """US-J1 AC: a failed stage degrades the run; independent safety+report still run."""
    policy = RunPolicy(max_attempts=2, sleep=lambda _s: None, backoff_base_s=0.0)
    outcome = run_pipeline(
        ctx, session_id, registry=all_ok_registry(pose=path("boom_stage")), policy=policy
    )
    fate = {s.stage: s for s in outcome.stages}
    assert outcome.status == "failed"
    assert fate["pose"].status is StageStatus.FAILED
    assert fate["pose"].attempts == 2
    assert "RuntimeError" in (fate["pose"].error or "")
    # pose's dependents cannot run; each SKIPPED row names its own missing input.
    for downstream in ("metrics", "bowling_action", "analysis", "progress", "planner"):
        assert fate[downstream].status is StageStatus.SKIPPED
    assert fate["metrics"].error == "missing input: pose failed"  # direct dependent
    assert fate["bowling_action"].error == "missing input: pose failed"
    assert fate["analysis"].error == "missing input: metrics skipped"  # cascade
    assert fate["planner"].error == "missing input: analysis skipped, progress skipped"
    # detect_track is independent of pose; the flight/variation chain hangs
    # off it and still runs; safety+report never depend on any of them.
    assert fate["detect_track"].status is StageStatus.SUCCEEDED
    assert fate["bowling_flight"].status is StageStatus.SUCCEEDED
    assert fate["classify_variations"].status is StageStatus.SUCCEEDED
    assert fate["safety"].status is StageStatus.SUCCEEDED
    assert fate["report"].status is StageStatus.SUCCEEDED


def test_unresolvable_stage_fails_honestly_without_crashing(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    registry = all_ok_registry(probe="cricai_worker.no_such_stage:go")
    outcome = run_pipeline(ctx, session_id, registry=registry)
    probe = next(s for s in outcome.stages if s.stage == "probe")
    assert probe.status is StageStatus.FAILED
    assert probe.attempts == 1
    assert "unresolvable stage callable" in (probe.error or "")
    assert outcome.status == "failed"


def test_run_with_default_paths_degrades_without_a_registry(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    # No registry -> DEFAULT_STAGE_PATHS. Every stage is now a real
    # (ctx, session_id) callable — the CV media stages go through the Phase-6
    # call-adapters — so a BARE session (no uploaded footage) degrades on its
    # honest INPUT gap: 'events' fails because there is no evidence footage to
    # detect on, its dependents skip with the reason, and the independent
    # safety + report tail still runs (US-J1 degraded-but-honest; US-H5 safety
    # is not blocked by an upstream media failure). A session WITH media runs
    # the media stages for real — see test_stage_adapters(_pg_integration).
    policy = RunPolicy(max_attempts=1)
    outcome = run_pipeline(ctx, session_id, policy=policy)
    fate = {s.stage: s for s in outcome.stages}
    assert outcome.status == "failed"
    assert fate["probe"].status is StageStatus.SUCCEEDED
    assert fate["calibrate_check"].status is StageStatus.SUCCEEDED
    assert fate["events"].status is StageStatus.FAILED
    # Pin the TRUE failure mode: an honest input-gap error naming the missing
    # footage — a dotted-path typo or signature mismatch would report a
    # different error and fail this assertion loudly.
    assert "StageInputError" in (fate["events"].error or "")
    assert "no evidence footage" in (fate["events"].error or "")
    # events' dependents skip with the reason; nothing runs on invented inputs.
    for downstream in (
        "clips",
        "pose",
        "detect_track",
        "metrics",
        "bowling_action",
        "bowling_flight",
        "classify_variations",
        "analysis",
    ):
        assert fate[downstream].status is StageStatus.SKIPPED, downstream
    assert fate["clips"].error == "missing input: events failed"
    assert fate["safety"].status is StageStatus.SUCCEEDED
    assert fate["report"].status is StageStatus.SUCCEEDED


# ------------------------------------------------------------------ resume


def test_rerun_short_circuits_media_stages_and_reexecutes_agent_stages(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    """Duplicate-job idempotency: an identical re-run resumes the media stages
    (their outputs are pure functions of the digested inputs) but ALWAYS
    re-executes the agent stages, whose real inputs are mutable DB state the
    digest cannot capture — resuming them replays stale verdicts/reports."""
    first = run_pipeline(ctx, session_id, registry=all_ok_registry())
    assert first.status == "succeeded"
    second = run_pipeline(ctx, session_id, registry=all_ok_registry())
    assert second.status == "succeeded"
    assert second.run_id != first.run_id
    fate = {s.stage: s for s in second.stages}
    for stage in (s for s in STAGES if s not in pl.NON_RESUMABLE):
        assert fate[stage].resumed is True, stage
        assert fate[stage].attempts == 0, stage
    for stage in pl.NON_RESUMABLE:
        assert fate[stage].resumed is False, stage
        assert fate[stage].attempts == 1, stage
        assert fate[stage].status is StageStatus.SUCCEEDED, stage


@pytest.mark.safety
def test_rerun_persists_the_fresh_safety_output_not_a_replay(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    """US-H5: the safety stage re-executes on every run and the trace persists
    the NEW verdict — a re-trigger after a pain check-in / ledger change can
    never replay run 1's stale 'no violations' verdict."""
    registry = all_ok_registry(safety=path("counting_stage"))
    first = run_pipeline(ctx, session_id, registry=registry)
    second = run_pipeline(ctx, session_id, registry=registry)
    assert _CALLS["counting"] == 2  # executed once per run, never short-circuited
    assert first.status == "succeeded" and second.status == "succeeded"
    safety_rows = [
        r
        for r in stage_rows(ctx, second.run_id)
        if r.stage == "safety"  # type: ignore[arg-type]
    ]
    assert len(safety_rows) == 1
    output = safety_rows[0].output
    assert output is not None
    assert output["resumed"] is False
    assert output["payload"] == {"count": 2}  # run 2's own output, not run 1's replay


def test_resume_reexecutes_lost_work_and_agent_stages(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    """US-L1 resume-from-failure: succeeded upstream media work resumes, the
    failed stage reruns — and the agent tail re-derives against live state."""
    registry = all_ok_registry(pose=path("flaky_stage"))
    policy = RunPolicy(max_attempts=1)  # first run: pose fails outright
    first = run_pipeline(ctx, session_id, registry=registry, policy=policy)
    assert first.status == "failed"
    assert next(s for s in first.stages if s.stage == "pose").status is StageStatus.FAILED

    second = run_pipeline(ctx, session_id, registry=registry, policy=policy)
    fate = {s.stage: s for s in second.stages}
    assert second.status == "succeeded"
    assert fate["probe"].resumed is False  # probe re-executes (fresh footage inventory)
    assert fate["events"].resumed is True  # upstream media work reused
    assert fate["pose"].resumed is False  # the previously-lost stage re-executed
    assert fate["pose"].status is StageStatus.SUCCEEDED
    assert fate["metrics"].status is StageStatus.SUCCEEDED  # newly runnable
    # In run 1 safety/report ran (degraded tail); in run 2 they re-execute
    # rather than resume: analysis newly succeeded, so the report must be
    # assembled from the fresh findings, not replayed from the degraded run.
    assert fate["safety"].resumed is False
    assert fate["report"].resumed is False


def test_in_place_video_replacement_invalidates_media_resume(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    """Honest resume vs in-place video replacement (Phase-6 finding 5).

    ``probe`` re-executes on every run (it is NON_RESUMABLE — its input digest
    is a constant) and its payload carries every evidence video's checksum, so
    the byte identity of the stored footage IS part of the resume currency:
    replacing a video's bytes under the same row/status changes probe's output
    digest and the events stage re-executes instead of resuming stale
    segmentation. Unchanged bytes still resume (duplicate-job idempotency).
    """
    registry = all_ok_registry(probe="cricai_worker.probe_media:probe_session_media")
    with ctx.session_factory() as db:
        db.add(
            Video(
                session_id=session_id,
                camera_id="C1",
                object_key=f"videos/{session_id}/C1.mp4",
                filename="c1.mp4",
                checksum_sha256="a" * 64,
                size_bytes=1,
                status=VideoStatus.PROBED,
            )
        )
        db.commit()
    first = run_pipeline(ctx, session_id, registry=registry)
    assert first.status == "succeeded"

    second = run_pipeline(ctx, session_id, registry=registry)
    fate2 = {s.stage: s for s in second.stages}
    assert fate2["probe"].resumed is False  # probe always re-executes (fresh inventory)
    assert fate2["events"].resumed is True  # bytes unchanged: honest resume

    with ctx.session_factory() as db:  # in-place replacement: same row+status, new bytes
        video = db.scalar(select(Video).where(Video.session_id == session_id))
        assert video is not None
        video.checksum_sha256 = "b" * 64
        db.commit()
    third = run_pipeline(ctx, session_id, registry=registry)
    fate3 = {s.stage: s for s in third.stages}
    assert fate3["events"].resumed is False  # stale segmentation must not resume


# -------------------------------------------- crashed-worker / trace honesty


def test_new_run_closes_abandoned_running_runs(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    """A prior run stuck RUNNING (crashed worker) is closed FAILED at the next
    run's start — the advisory lock proves it cannot still be alive — so the
    trace stays honest and status-based run readers only ever see the live run."""
    with ctx.session_factory() as db:
        dead_run = PipelineRun(session_id=session_id, status=StageStatus.RUNNING)
        db.add(dead_run)
        db.flush()
        db.add(
            PipelineStage(
                run_id=dead_run.id,
                stage="metrics",
                status=StageStatus.RUNNING,
                attempt=1,
            )
        )
        db.commit()
        dead_run_id = dead_run.id

    outcome = run_pipeline(ctx, session_id, registry=all_ok_registry())
    assert outcome.status == "succeeded"
    assert outcome.run_id != dead_run_id
    with ctx.session_factory() as db:
        closed = db.get(PipelineRun, dead_run_id)
        assert closed is not None
        assert closed.status is StageStatus.FAILED
        assert closed.finished_at is not None
        stale_stage = db.scalar(select(PipelineStage).where(PipelineStage.run_id == dead_run_id))
        assert stale_stage is not None
        assert stale_stage.status is StageStatus.FAILED
        assert stale_stage.error == pl.ABANDONED_RUN_ERROR
        assert stale_stage.finished_at is not None


def test_unserializable_stage_payload_fails_the_attempt_not_the_runner(
    ctx: WorkerContext, session_id: uuid.UUID
) -> None:
    """A payload the JSON trace column cannot store must burn the attempt like
    any stage error (FAILED row + retries + honest terminal run status), never
    escape the runner and leave the run stuck RUNNING."""
    policy = RunPolicy(max_attempts=2, sleep=lambda _s: None, backoff_base_s=0.0)
    outcome = run_pipeline(
        ctx, session_id, registry=all_ok_registry(probe=path("unserializable_stage")), policy=policy
    )
    assert outcome.status == "failed"
    probe = next(s for s in outcome.stages if s.stage == "probe")
    assert probe.status is StageStatus.FAILED
    assert probe.attempts == 2  # the serialization error is retried like any other
    assert "not JSON serializable" in (probe.error or "")
    rows = [r for r in stage_rows(ctx, outcome.run_id) if r.stage == "probe"]  # type: ignore[arg-type]
    assert sorted(r.attempt for r in rows) == [1, 2]
    assert all(r.status is StageStatus.FAILED for r in rows)
    assert all(r.finished_at is not None for r in rows)
    with ctx.session_factory() as db:
        run = db.get(PipelineRun, outcome.run_id)
        assert run is not None
        assert run.status is StageStatus.FAILED  # honest terminal row, not zombie RUNNING
        assert run.finished_at is not None


# -------------------------------------------------------- locking / outcome


class _GrantLock:
    def __init__(self) -> None:
        self.released = False

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        self.released = True


class _DenyLock:
    def __init__(self) -> None:
        self.released = False

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        self.released = True


def test_injected_lock_is_acquired_and_released(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    lock = _GrantLock()
    outcome = run_pipeline(ctx, session_id, registry=all_ok_registry(), policy=RunPolicy(lock=lock))
    assert outcome.status == "succeeded"
    assert lock.released is True


def test_held_lock_yields_a_locked_outcome(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    lock = _DenyLock()
    outcome = run_pipeline(ctx, session_id, registry=all_ok_registry(), policy=RunPolicy(lock=lock))
    assert outcome.status == LOCKED
    assert outcome.run_id is None
    assert outcome.stages == ()
    assert lock.released is False  # released only after a successful acquire


def test_locked_outcome_payload_has_null_run_id() -> None:
    sid = uuid.uuid4()
    payload = PipelineOutcome(session_id=sid, run_id=None, status=LOCKED, stages=()).as_payload()
    assert payload == {
        "session_id": str(sid),
        "run_id": None,
        "status": LOCKED,
        "stages": [],
    }


def test_succeeded_outcome_payload_is_json_ready(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    payload = run_pipeline(ctx, session_id, registry=all_ok_registry()).as_payload()
    assert payload["status"] == "succeeded"
    assert payload["run_id"] is not None
    assert {s["stage"] for s in payload["stages"]} == set(STAGES)
    assert payload["stages"][0]["status"] == "succeeded"


# ---------------------------------------------------- job + enqueue seams


def test_run_session_pipeline_job_uses_env_context(
    ctx: WorkerContext, session_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(WorkerContext, "from_env", staticmethod(lambda: ctx))
    result = run_session_pipeline_job(str(session_id), registry=all_ok_registry())
    assert result["status"] == "succeeded"
    assert result["run_id"] is not None


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def enqueue(self, f: Any, *args: Any, **_kwargs: Any) -> str:
        self.calls.append((f, args))
        return "job-1"


def test_enqueue_session_pipeline_is_thin() -> None:
    queue = _FakeQueue()
    sid = uuid.uuid4()
    assert pl.enqueue_session_pipeline(queue, sid) == "job-1"
    (fn, args) = queue.calls[0]
    assert fn is pl.run_session_pipeline_job
    assert args == (str(sid),)


def test_run_row_status_transitions_to_succeeded(ctx: WorkerContext, session_id: uuid.UUID) -> None:
    outcome = run_pipeline(ctx, session_id, registry=all_ok_registry())
    with ctx.session_factory() as db:
        run = db.get(PipelineRun, outcome.run_id)
        assert run is not None
        assert run.status is StageStatus.SUCCEEDED
        assert run.finished_at is not None
