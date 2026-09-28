"""Session pipeline DAG runner (US-J1, US-L1) — a thin layer over RQ.

Design (Phase-5 plan, pinned contract #8):

- **Fixed stage order** :data:`STAGES` — ``probe → calibrate_check → events →
  clips → pose → detect_track → metrics → bowling_action → bowling_flight →
  classify_variations → analysis → progress → planner → safety → report``.
  Stage names are the ``pipeline_stages.stage`` vocabulary. The three Phase-6
  bowling metric stages (US-I2/I3/I4/I5/I6) sit at the metrics→analysis
  boundary so a BOWLING session's ``ball_metrics``/``delivery_labels`` rows
  exist before the analysis agent assembles BallRecords; for every other
  session type they are honest session-type-gated no-ops.
  **Safety is topologically last before report** (US-H5 supremacy): ``report``
  hard-depends on ``safety``, so no report exists without a safety verdict —
  enforced structurally by :func:`validate_topology`, which runs at import.
- **Stage callables are data**: dotted-path strings (``pkg.module:function``)
  resolved via :mod:`importlib` at run time. :data:`DEFAULT_STAGE_PATHS` pins
  the production wiring (the five CV media stages go through the Phase-6
  call-adapters in :mod:`cricai_worker.stage_adapters`); tests inject fakes
  through the ``registry`` parameter. A stage callable takes
  ``(ctx, session_id)`` and returns a JSON payload.
- **Per-stage trace rows**: every attempt is one ``PipelineStage`` row
  (``uq (run_id, stage, attempt)``) with timings in ``started_at`` /
  ``finished_at`` and ``output = {"digest", "payload", "resumed"}`` — the full
  run trace US-J1 requires. Retries use capped exponential backoff.
- **Resume via input digests — media stages only**: a stage's ``input_digest``
  hashes its upstream output digests plus the run's ``detector_context``.
  Re-running a session short-circuits *media* stages whose digest matches a
  previously *succeeded* attempt (duplicate-job idempotency, resume-from-failure
  re-executes only lost CV work). Everything in :data:`NON_RESUMABLE` — probe,
  the bowling metric stages and the agent stages — reads mutable DB state no
  digest captures, so it re-executes on **every** run: a resumed run must never
  replay a stale footage inventory, bounce/target/label state, safety verdict
  or report (US-H5).
- **Degraded but honest** (US-J1 AC): a stage that fails after retries marks
  the run FAILED, downstream stages missing that input record SKIPPED with the
  reason, and independent stages (notably ``safety`` → ``report``) still run —
  "bat-path analysis unavailable today", never silence or invention.
- **Crashed-worker hygiene**: a new run starts by closing any prior RUNNING
  run of the session as FAILED — the advisory lock it holds proves nothing
  else can still be running — so the persisted trace never serves a zombie
  "running" status and status-based run readers only ever see the live run.
- **Concurrent-session safety**: one PostgreSQL advisory lock per session
  (key = first 8 bytes of SHA-256 of the session UUID). A held lock means
  another worker is on this session: the runner returns a ``locked`` outcome
  (callers skip or requeue) instead of interleaving writes. On non-PostgreSQL
  engines (SQLite unit tests) the lock is a no-op; the PG integration suite
  exercises real contention.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from cricai_data.db import session_scope
from cricai_data.enums import StageStatus
from cricai_data.models import PipelineRun, PipelineStage, utcnow
from cricai_data.models import Session as SessionRow
from sqlalchemy import Engine, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext

#: Pinned stage order (plan contract #8). Safety is last before report.
STAGES: tuple[str, ...] = (
    "probe",
    "calibrate_check",
    "events",
    "clips",
    "pose",
    "detect_track",
    "metrics",
    "bowling_action",
    "bowling_flight",
    "classify_variations",
    "analysis",
    "progress",
    "planner",
    "safety",
    "report",
)

#: DAG edges: stage -> upstream stages whose SUCCEEDED output it requires.
#: ``safety`` has no upstream (it reads ledger/wellness state, US-H1/H4) and
#: ``report`` requires ``safety`` — the structural half of US-H5 supremacy.
#:
#: ``report`` deliberately depends ONLY on ``safety`` even though run_report
#: also reads findings + quality: a dep on ``analysis`` would SKIP the report
#: exactly when the degraded-but-honest design requires it to ship ("bat-path
#: analysis unavailable today", US-J1/G3 honesty path). Report freshness is
#: guaranteed not by DAG edges but by ``report`` being :data:`NON_RESUMABLE`:
#: it re-executes on every run and reads findings/quality live from the DB,
#: so a resumed run can never serve a stale report body.
#: The bowling stages (US-I2-I6) hang off the media work they consume:
#: ``bowling_action`` reads the C5 cut clips through the pose-stage seam
#: (``pose`` proves the clips decoded); ``bowling_flight`` reads the tracked
#: trajectories (``detect_track``); ``classify_variations`` reads the flight
#: metrics ``bowling_flight`` just wrote. None of them gates ``analysis``:
#: a failed bowling stage degrades that section to honest nulls rather than
#: blocking the whole report (US-J1 degraded-but-honest).
STAGE_DEPS: dict[str, tuple[str, ...]] = {
    "probe": (),
    "calibrate_check": ("probe",),
    "events": ("probe",),
    "clips": ("events",),
    "pose": ("clips",),
    "detect_track": ("events",),
    "metrics": ("pose", "detect_track"),
    "bowling_action": ("pose",),
    "bowling_flight": ("detect_track",),
    "classify_variations": ("bowling_flight",),
    "analysis": ("metrics",),
    "progress": ("metrics",),
    "planner": ("analysis", "progress"),
    "safety": (),
    "report": ("safety",),
}

#: Stages that must NEVER short-circuit via digest resume. The media stages'
#: outputs are pure functions of their upstream digests + detector context, so
#: replaying a succeeded attempt is safe (and is what resume exists for — they
#: are the expensive CV work). The agent stages instead read mutable DB state
#: the input digest does not capture — analysis: coaching_rules/rule_overrides
#: + ball tags; progress: metric_baselines; planner: drills + the ledger
#: allowance; safety: ledger/wellness/pain-clearance/safety_config + the
#: persisted plan; report: findings + quality + the plan — so a digest match
#: proves nothing about their real inputs. They re-execute on every run: a
#: re-trigger after a pain check-in must re-validate the persisted plan
#: (US-H5) and regenerate the report, never replay a stale verdict.
#:
#: ``probe`` joins them for the same reason at the other end of the DAG: it
#: has no upstream digests, so its input digest is a constant — resuming it
#: would replay a stale footage inventory forever. Its real input is mutable
#: DB state (the session's video rows: uploads, re-uploads, status flips) and
#: it is one cheap SELECT, so it re-executes on every run. Its payload carries
#: the per-video byte identity (``video_checksums``), which makes the OUTPUT
#: digest the honest resume currency for everything downstream: replace a
#: video's bytes in place and the whole media chain re-executes instead of
#: resuming stale CV work.
#:
#: The bowling stages (US-I2-I6) are agent-like despite living in the media
#: half of the DAG: beyond the tracked media they read mutable DB rows the
#: digest cannot capture — bowling_action: ball_metrics merge targets;
#: bowling_flight: manual ``bounce_marks`` (manual beats machine, US-F4),
#: declared ``bowling_targets`` and ``bounce_estimates``; classify_variations:
#: coach-entered ``delivery_labels`` intents. A coach marking a bounce or
#: declaring a target between runs must change the metrics, so a digest match
#: proves nothing — they re-execute on every run.
#: :func:`validate_topology` enforces that safety and report stay in this set.
NON_RESUMABLE: frozenset[str] = frozenset(
    {
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
)

#: Error recorded on the RUNNING stage rows of a crashed run closed by a newer
#: run's start-of-run hygiene sweep (``pipeline_runs`` has no error column, so
#: the run row carries only the FAILED status + ``finished_at``).
ABANDONED_RUN_ERROR = "abandoned: superseded by a newer run"

#: Production stage wiring: ``module:function`` dotted paths resolved lazily.
#: A wiring integration test resolves every path so a rename fails loudly, and
#: an unresolvable stage records an honest FAILED row at run time instead of
#: crashing the runner.
#:
#: Every stage is a real ``fn(ctx, session_id)`` callable (:data:`StageCallable`).
#: The five CV media stages go through :mod:`cricai_worker.stage_adapters`
#: (Phase-6 call-adapters), which derive each job's richer real inputs from
#: stored data — probe payloads, uploaded videos, cut clips, calibrations and
#: the production model registry — and degrade honestly, never silently: a
#: missing INPUT fails the stage with the gap named on its trace row, a
#: missing MODEL falls back with the reason recorded in the stage output.
DEFAULT_STAGE_PATHS: dict[str, str] = {
    "probe": "cricai_worker.probe_media:probe_session_media",
    "calibrate_check": "cricai_worker.calibrate_check:check_session_calibration",
    "events": "cricai_worker.stage_adapters:run_events_stage",
    "clips": "cricai_worker.stage_adapters:run_clips_stage",
    "pose": "cricai_worker.stage_adapters:run_pose_stage",
    "detect_track": "cricai_worker.stage_adapters:run_detect_track_stage",
    "metrics": "cricai_worker.stage_adapters:run_metrics_stage",
    "bowling_action": "cricai_worker.stage_adapters:run_bowling_action_stage",
    "bowling_flight": "cricai_worker.stage_adapters:run_bowling_flight_stage",
    "classify_variations": "cricai_worker.stage_adapters:run_classify_variations_stage",
    "analysis": "cricai_worker.agent_stages:run_analysis",
    "progress": "cricai_worker.agent_stages:run_progress",
    "planner": "cricai_worker.agent_stages:run_planner",
    "safety": "cricai_worker.agent_stages:run_safety",
    "report": "cricai_worker.agent_stages:run_report",  # wrapper over pinned seam #9
}

#: Outcome status for a run skipped because another worker holds the session.
LOCKED = "locked"

StageCallable = Callable[[WorkerContext, uuid.UUID], Mapping[str, Any] | None]


class PipelineTopologyError(RuntimeError):
    """The stage vocabulary/DAG is inconsistent — refuse to run anything."""


def validate_topology(
    stages: Sequence[str] = STAGES,
    deps: Mapping[str, tuple[str, ...]] = STAGE_DEPS,
    non_resumable: frozenset[str] = NON_RESUMABLE,
) -> None:
    """Assert the DAG is runnable in :data:`STAGES` order with safety last.

    Runs at import so a bad edit can never ship; the SAF topology test also
    calls it against corrupted variants (US-J1/H5 AC "enforced in code + test").
    Also pins that safety and report can never be made digest-resumable: their
    inputs are live DB state, so a resumed run replaying a stale verdict or
    report would bypass US-H5 re-validation.
    """
    if len(set(stages)) != len(stages):
        raise PipelineTopologyError("duplicate stage names in STAGES")
    if set(stages) != set(deps):
        raise PipelineTopologyError("STAGES and STAGE_DEPS name different stage vocabularies")
    for index, stage in enumerate(stages):
        earlier = set(stages[:index])
        for dep in deps[stage]:
            if dep not in earlier:
                raise PipelineTopologyError(
                    f"stage {stage!r} depends on {dep!r} which does not run before it"
                )
    if stages[-1] != "report" or stages[-2] != "safety":
        raise PipelineTopologyError("safety must be the last stage before report (US-H5)")
    if "safety" not in deps["report"]:
        raise PipelineTopologyError("report must hard-depend on safety (US-H5)")
    unknown = non_resumable - set(stages)
    if unknown:
        raise PipelineTopologyError(f"non-resumable stages not in STAGES: {sorted(unknown)}")
    if not {"safety", "report"} <= non_resumable:
        raise PipelineTopologyError(
            "safety and report must be non-resumable (US-H5: never replay a stale verdict)"
        )


validate_topology()


def advisory_lock_key(session_id: uuid.UUID) -> int:
    """Stable signed 64-bit PostgreSQL advisory-lock key for one session."""
    digest = hashlib.sha256(session_id.bytes).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class SessionLock(Protocol):
    """Per-session mutual exclusion; ``acquire`` is non-blocking (skip/requeue)."""

    def acquire(self) -> bool: ...

    def release(self) -> None: ...


@dataclass
class AdvisoryLock:
    """PostgreSQL session-level advisory lock held on a dedicated connection.

    The lock lives on its own connection for the whole run (the runner's ORM
    session returns pooled connections on every commit, which would leak a
    connection-scoped lock). On non-PostgreSQL engines this is a no-op that
    always acquires — unit DBs are single-tester SQLite; real contention is
    integration-tested on PostgreSQL.
    """

    engine: Engine
    key: int
    _conn: Connection | None = field(default=None, init=False, repr=False)

    def acquire(self) -> bool:
        if self.engine.dialect.name != "postgresql":
            return True
        conn = self.engine.connect()
        got = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key}).scalar()
        )
        if not got:
            conn.close()
            return False
        self._conn = conn
        return True

    def release(self) -> None:
        if self._conn is None:
            return
        self._conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
        self._conn.close()
        self._conn = None


@dataclass(frozen=True)
class RunPolicy:
    """Retry/backoff/locking knobs; tests inject fast values and fake locks."""

    max_attempts: int = 3
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    sleep: Callable[[float], None] | None = None  # None -> time.sleep
    lock: SessionLock | None = None  # None -> AdvisoryLock on the ctx engine

    def backoff_s(self, attempt: int) -> float:
        return float(min(self.backoff_base_s * (2 ** (attempt - 1)), self.backoff_cap_s))


@dataclass(frozen=True)
class StageOutcome:
    """One stage's fate within a run (summary view of its trace rows)."""

    stage: str
    status: StageStatus
    attempts: int
    resumed: bool
    error: str | None
    digest: str | None  # output payload digest when succeeded


@dataclass(frozen=True)
class PipelineOutcome:
    """What one :func:`run_pipeline` call did; ``status`` is
    ``locked | succeeded | failed``."""

    session_id: uuid.UUID
    run_id: uuid.UUID | None
    status: str
    stages: tuple[StageOutcome, ...]

    def as_payload(self) -> dict[str, Any]:
        """JSON-serializable form (RQ job results, API responses)."""
        return {
            "session_id": str(self.session_id),
            "run_id": None if self.run_id is None else str(self.run_id),
            "status": self.status,
            "stages": [
                {
                    "stage": s.stage,
                    "status": s.status.value,
                    "attempts": s.attempts,
                    "resumed": s.resumed,
                    "error": s.error,
                    "digest": s.digest,
                }
                for s in self.stages
            ],
        }


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def payload_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 of a stage's canonical output payload — the resume currency."""
    return hashlib.sha256(_canonical(dict(payload)).encode()).hexdigest()


def stage_input_digest(
    stage: str, dep_digests: Mapping[str, str], detector_context: Mapping[str, Any]
) -> str:
    """Digest of everything that determines a stage's work.

    Upstream output digests plus the run's detector context: change either and
    the stage re-executes instead of short-circuiting (honest resume).
    """
    body = {"stage": stage, "inputs": dict(dep_digests), "context": dict(detector_context)}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def resolve_stage(path: str) -> StageCallable:
    """Resolve ``module:function`` to a callable; raises on any miswiring."""
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"stage path {path!r} must look like 'package.module:function'")
    module = importlib.import_module(module_name)
    fn = getattr(module, attr)
    if not callable(fn):
        raise TypeError(f"stage path {path!r} resolves to a non-callable")
    return cast("StageCallable", fn)


def context_engine(ctx: WorkerContext) -> Engine:
    """The engine behind a worker context (advisory locks need one)."""
    return cast("Engine", ctx.session_factory.kw["bind"])


@dataclass
class _Runner:
    """One pipeline run over one session; holds the run-scoped state."""

    db: OrmSession
    ctx: WorkerContext
    session_id: uuid.UUID
    paths: Mapping[str, str]
    context: dict[str, Any]
    policy: RunPolicy
    outputs: dict[str, str] = field(default_factory=dict)  # stage -> output digest
    fates: dict[str, StageStatus] = field(default_factory=dict)
    run_id: uuid.UUID = field(init=False)  # set once the run row exists

    def run(self) -> PipelineOutcome:
        if self.db.get(SessionRow, self.session_id) is None:
            raise LookupError(f"session {self.session_id} not found")
        self._close_abandoned_runs()
        run = PipelineRun(
            session_id=self.session_id,
            status=StageStatus.RUNNING,
            detector_context=self.context,
        )
        self.db.add(run)
        self.db.commit()
        self.run_id = run.id
        outcomes = tuple(self._run_stage(stage) for stage in STAGES)
        failed = any(fate is StageStatus.FAILED for fate in self.fates.values())
        run.status = StageStatus.FAILED if failed else StageStatus.SUCCEEDED
        run.finished_at = utcnow()
        self.db.commit()
        return PipelineOutcome(
            session_id=self.session_id,
            run_id=run.id,
            status=run.status.value,
            stages=outcomes,
        )

    def _close_abandoned_runs(self) -> None:
        """Crashed-worker hygiene: close zombie RUNNING rows before starting.

        This runner holds the per-session advisory lock, so a prior run of
        this session still marked RUNNING cannot actually be running — its
        worker died mid-run (OOM/SIGKILL/redeploy). Close the run and its
        RUNNING stage rows as FAILED so the persisted trace stays honest
        ("degraded but honest", never a zombie "running" status) and so
        status-based readers (the agent stages locating the current run)
        only ever see the one live run regardless of cross-worker clock skew.
        ``pipeline_runs`` has no error column; the abandonment reason lands
        on the closed stage rows.
        """
        now = utcnow()
        stale_runs = self.db.scalars(
            select(PipelineRun).where(
                PipelineRun.session_id == self.session_id,
                PipelineRun.status == StageStatus.RUNNING,
            )
        ).all()
        for dead_run in stale_runs:
            dead_run.status = StageStatus.FAILED
            dead_run.finished_at = now
            for dead_stage in self.db.scalars(
                select(PipelineStage).where(
                    PipelineStage.run_id == dead_run.id,
                    PipelineStage.status == StageStatus.RUNNING,
                )
            ):
                dead_stage.status = StageStatus.FAILED
                dead_stage.error = ABANDONED_RUN_ERROR
                dead_stage.finished_at = now
        self.db.commit()

    def _run_stage(self, stage: str) -> StageOutcome:
        deps = STAGE_DEPS[stage]
        missing = [dep for dep in deps if dep not in self.outputs]
        if missing:
            reason = "missing input: " + ", ".join(
                f"{dep} {self.fates[dep].value}" for dep in missing
            )
            self._record_terminal(stage, StageStatus.SKIPPED, error=reason)
            self.fates[stage] = StageStatus.SKIPPED
            return StageOutcome(stage, StageStatus.SKIPPED, 0, False, reason, None)
        digest = stage_input_digest(stage, {dep: self.outputs[dep] for dep in deps}, self.context)
        prior = None
        if stage not in NON_RESUMABLE:  # agent stages read live DB state: always re-execute
            prior = self._resumable_output(stage, digest)
        if prior is not None:
            prior_digest = str(prior["digest"])
            self._record_terminal(
                stage,
                StageStatus.SUCCEEDED,
                digest=digest,
                output={"digest": prior_digest, "payload": prior["payload"], "resumed": True},
            )
            self.outputs[stage] = prior_digest
            self.fates[stage] = StageStatus.SUCCEEDED
            return StageOutcome(stage, StageStatus.SUCCEEDED, 0, True, None, prior_digest)
        try:
            fn = resolve_stage(self.paths[stage])
        except Exception as exc:
            error = f"unresolvable stage callable {self.paths[stage]!r}: {exc}"
            self._record_terminal(stage, StageStatus.FAILED, digest=digest, error=error)
            self.fates[stage] = StageStatus.FAILED
            return StageOutcome(stage, StageStatus.FAILED, 1, False, error, None)
        return self._execute(stage, digest, fn)

    def _execute(self, stage: str, digest: str, fn: StageCallable) -> StageOutcome:
        """Run one stage with retries; every attempt is its own trace row."""
        sleep = self.policy.sleep if self.policy.sleep is not None else time.sleep
        error = ""
        for attempt in range(1, self.policy.max_attempts + 1):
            row = PipelineStage(
                run_id=self.run_id,
                stage=stage,
                status=StageStatus.RUNNING,
                attempt=attempt,
                input_digest=digest,
                started_at=utcnow(),
            )
            self.db.add(row)
            self.db.commit()
            try:
                # The success-path digest + trace commit live INSIDE the try:
                # a payload the JSON column cannot serialize must burn the
                # attempt like any stage error (FAILED row, retries, honest
                # terminal run status) — never escape the runner and strand
                # the run row in RUNNING with no error anywhere.
                payload = dict(fn(self.ctx, self.session_id) or {})
                out_digest = payload_digest(payload)
                row.status = StageStatus.SUCCEEDED
                row.output = {"digest": out_digest, "payload": payload, "resumed": False}
                row.finished_at = utcnow()
                self.db.commit()
            except Exception as exc:
                self.db.rollback()  # a failed flush poisons the session until rolled back
                error = f"{type(exc).__name__}: {exc}"
                row.status = StageStatus.FAILED
                row.error = error
                row.output = None
                row.finished_at = utcnow()
                self.db.commit()
                if attempt < self.policy.max_attempts:
                    sleep(self.policy.backoff_s(attempt))
                continue
            self.outputs[stage] = out_digest
            self.fates[stage] = StageStatus.SUCCEEDED
            return StageOutcome(stage, StageStatus.SUCCEEDED, attempt, False, None, out_digest)
        self.fates[stage] = StageStatus.FAILED
        return StageOutcome(stage, StageStatus.FAILED, self.policy.max_attempts, False, error, None)

    def _resumable_output(self, stage: str, digest: str) -> dict[str, Any] | None:
        """Latest succeeded output of this stage for the same inputs, if any.

        ``finished_at`` is a client-side clock, so two workers' timestamps can
        collide (or skew); the ``id`` tiebreak keeps the pick deterministic
        rather than fetch-order-dependent. Any row matching the input digest
        is equally valid to resume from, so determinism is all that matters.
        """
        row = self.db.scalar(
            select(PipelineStage)
            .join(PipelineRun, PipelineStage.run_id == PipelineRun.id)
            .where(
                PipelineRun.session_id == self.session_id,
                PipelineRun.id != self.run_id,
                PipelineStage.stage == stage,
                PipelineStage.status == StageStatus.SUCCEEDED,
                PipelineStage.input_digest == digest,
            )
            .order_by(PipelineStage.finished_at.desc(), PipelineStage.id.desc())
            .limit(1)
        )
        return None if row is None else row.output

    def _record_terminal(
        self,
        stage: str,
        status: StageStatus,
        *,
        digest: str | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Write a single-shot trace row (skipped, resumed, or unresolvable)."""
        now = utcnow()
        self.db.add(
            PipelineStage(
                run_id=self.run_id,
                stage=stage,
                status=status,
                attempt=1,
                input_digest=digest,
                output=output,
                error=error,
                started_at=now,
                finished_at=now,
            )
        )
        self.db.commit()


def run_pipeline(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    registry: Mapping[str, str] | None = None,
    detector_context: Mapping[str, Any] | None = None,
    policy: RunPolicy | None = None,
) -> PipelineOutcome:
    """Run the session DAG; see the module docstring for the semantics.

    Raises :class:`LookupError` when the session does not exist. Returns a
    ``locked`` outcome (no run row) when another worker holds the session.
    """
    paths = dict(DEFAULT_STAGE_PATHS)
    if registry is not None:
        paths.update(registry)
    run_policy = policy if policy is not None else RunPolicy()
    lock: SessionLock = (
        run_policy.lock
        if run_policy.lock is not None
        else AdvisoryLock(engine=context_engine(ctx), key=advisory_lock_key(session_id))
    )
    if not lock.acquire():
        return PipelineOutcome(session_id=session_id, run_id=None, status=LOCKED, stages=())
    try:
        with session_scope(ctx.session_factory) as db:
            runner = _Runner(
                db=db,
                ctx=ctx,
                session_id=session_id,
                paths=paths,
                context=dict(detector_context or {}),
                policy=run_policy,
            )
            return runner.run()
    finally:
        lock.release()


def run_session_pipeline_job(
    session_id: str,
    *,
    registry: Mapping[str, str] | None = None,
    detector_context: Mapping[str, Any] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """RQ job entry point: env-configured context, JSON-serializable result."""
    ctx = WorkerContext.from_env()
    outcome = run_pipeline(
        ctx,
        uuid.UUID(session_id),
        registry=registry,
        detector_context=detector_context,
        policy=RunPolicy(max_attempts=max_attempts),
    )
    return outcome.as_payload()


class _Enqueuer(Protocol):
    """The one RQ ``Queue`` method the enqueue seam uses (duck-typed for tests)."""

    def enqueue(self, f: Callable[..., Any], *args: Any, **kwargs: Any) -> Any: ...


def enqueue_session_pipeline(queue: _Enqueuer, session_id: uuid.UUID) -> Any:
    """Enqueue one session's DAG run on an RQ queue (thin, by design)."""
    return queue.enqueue(run_session_pipeline_job, str(session_id))
