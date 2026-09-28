"""Per-ball technique & decision metrics API (US-E2/E3/E4).

- ``PUT /sessions/{id}/balls/{ball_no}/metrics/{phase}``: parent-only upsert of one
  phase's metric set (the worker writes with the parent token). The ball must be
  known — a non-rejected BallEvent or a manual BallTag, the reference-library rule
  — else 404 (no phantom rows pinning US-D4 renumbering). Every value is validated
  against the pinned metric contract ``{value, unit, confidence, reason-if-null,
  proxy?, source?}``; a null value without a reason is a silent-zero violation and
  a 422, and numeric values must be finite (US-E2/E3 AC). Replacing a phase
  preserves that phase's machine-written keys
  (:data:`MACHINE_WRITTEN_METRIC_KEYS`: the US-F5 fusion contact keys, the
  US-I2 bowling-action release/checkpoint keys, the US-I3 bowling-flight
  turn/apex/dip/target keys) unless the payload rewrites them — those jobs
  merge-write by key, and a re-run of this PUT must never silently delete
  their outputs (survival is mutual; findings 6/32/57).
- ``GET /sessions/{id}/balls/{ball_no}/metrics``: all stored phases. When no flight
  row is stored but the ball has manual context (US-B4 tag / US-C5 bounce mark), a
  flight set is synthesized on the fly via
  :func:`cricai_coaching.decision.assemble_flight_metrics` and returned with
  ``stored=false`` -- US-E4 V1: identical schema either way, with source provenance.
- ``GET /sessions/{id}/metrics/summary``: per-metric means over numeric values
  (booleans average as the fraction true, e.g. control %); the non-null count is
  the reported denominator next to the phase's total ball count (US-E3 AC).
- ``GET /sessions/{id}/decision-quality``: leaves vs chases per length zone for
  balls outside off (US-E4 AC), over tags joined with bounce marks (the mark with
  the lowest camera_id represents a multi-camera ball, as in the heatmap).

All reads are open to every role with player access scoped away from guest
sessions (US-L3); writes are parent-only.
"""

import math
import uuid
from collections import defaultdict
from typing import Annotated, Any

from cricai_coaching.checkpoints import CHECKPOINT_KEYS
from cricai_coaching.contact_fusion import FUSION_METRIC_KEYS
from cricai_coaching.decision import BallContext, assemble_flight_metrics, decision_quality
from cricai_data.enums import Length, MetricPhase, Role
from cricai_data.models import BallEvent, BallMetrics, BallTag, BounceMark, Session, SessionBlock
from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(tags=["ball-metrics"])

#: All roles may read metrics; player access is scoped away from guest data.
READ_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH, Role.PLAYER)

#: The pinned per-value contract keys (US-E2/E3/E4).
METRIC_VALUE_KEYS: frozenset[str] = frozenset(
    {"value", "unit", "confidence", "reason", "proxy", "source"}
)

#: Machine-written (job-owned, merge-written) metric keys per phase, preserved
#: across a whole-phase-set PUT unless the payload explicitly rewrites them —
#: survival is mutual (US-F5/I2/I3): the fusion job owns the contact keys, the
#: US-I2 bowling-action job owns the pre_release release/checkpoint keys and
#: the US-I3 bowling-flight job owns the flight turn/apex/dip/target keys. The
#: API cannot import the worker app, so the flight/pre_release tuples mirror
#: ``cricai_worker.bowling_flight.BOWLING_FLIGHT_KEYS`` and
#: ``cricai_worker.bowling_action.BOWLING_ACTION_KEYS``; a parity test in
#: ``apps/api/tests`` pins the mirrors (findings 6/32/57).
MACHINE_WRITTEN_METRIC_KEYS: dict[MetricPhase, tuple[str, ...]] = {
    MetricPhase.CONTACT: tuple(FUSION_METRIC_KEYS),
    MetricPhase.FLIGHT: ("turn_cm", "apex_m", "dip_flag", "target_hit"),
    MetricPhase.PRE_RELEASE: (
        "release_frame",
        "release_ms",
        "hand_xy",
        "release_height_cm",
        "release_frame_offset",
        *CHECKPOINT_KEYS,
    ),
}

BallNo = Annotated[int, Path(ge=1)]


class MetricsIn(BaseModel):
    metrics: dict[str, Any]
    schema_version: int = Field(default=1, ge=1)


class PhaseMetricsOut(BaseModel):
    phase: MetricPhase
    metrics: dict[str, Any]
    schema_version: int
    stored: bool  # false = synthesized on the fly from manual sources (US-E4 V1)


class MetricSummaryOut(BaseModel):
    """count is the denominator actually used (US-E3: the denominator is reported)."""

    name: str
    mean: float | None  # null when no numeric/boolean values exist for this metric
    count: int  # non-null values


class PhaseSummaryOut(BaseModel):
    phase: MetricPhase
    balls: int  # total balls with a stored metric row in this phase
    metrics: list[MetricSummaryOut]


class SummaryOut(BaseModel):
    session_id: uuid.UUID
    phases: list[PhaseSummaryOut]


class ZoneOut(BaseModel):
    length: Length
    leaves: int
    chases: int
    balls: int


class DecisionQualityOut(BaseModel):
    session_id: uuid.UUID
    outside_off_balls: int
    leaves: int
    chases: int
    unclassified: int
    zones: list[ZoneOut]


def _session_or_404(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only; hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _require(condition: bool, name: str, problem: str) -> None:
    if not condition:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"metric {name!r}: {problem}")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    """Finite numbers only: ``json.loads`` accepts NaN/Infinity tokens, which would
    serialize back as bare nulls (a silent-null violation) and break jsonb on
    PostgreSQL, so they are contract violations (422), never stored."""
    return _is_number(value) and math.isfinite(value)


def _valid_value(value: Any) -> bool:
    if value is None or isinstance(value, str | bool):
        return True
    if _is_finite_number(value):
        return True
    return isinstance(value, list) and all(_is_finite_number(item) for item in value)


def _validate_annotations(name: str, entry: dict[str, Any]) -> None:
    reason = entry.get("reason")
    _require(
        reason is None or (isinstance(reason, str) and bool(reason.strip())),
        name,
        "'reason' must be a non-empty string",
    )
    _require(
        entry["value"] is not None or reason is not None,
        name,
        "null value without a reason (silent-zero violation, US-E2/E3)",
    )
    _require(
        not ("proxy" in entry and not isinstance(entry["proxy"], bool)),
        name,
        "'proxy' must be a boolean",
    )
    _require(
        not ("source" in entry and not isinstance(entry["source"], str)),
        name,
        "'source' must be a string",
    )


def _validate_metrics(metrics: dict[str, Any]) -> None:
    """Enforce the {value, unit, confidence, reason-if-null, proxy?} contract (422s)."""
    if not metrics:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "metrics must contain at least one entry"
        )
    for name, entry in metrics.items():
        _require(isinstance(entry, dict), name, "must be an object with value/unit/confidence")
        unknown = sorted(set(entry) - METRIC_VALUE_KEYS)
        _require(not unknown, name, f"unknown keys {unknown}")
        _require("value" in entry, name, "missing required key 'value'")
        _require(
            _valid_value(entry["value"]),
            name,
            "'value' must be null, a finite number, a string, a boolean,"
            " or a list of finite numbers",
        )
        _require(isinstance(entry.get("unit"), str), name, "'unit' must be a string")
        confidence = entry.get("confidence")
        _require(_is_finite_number(confidence), name, "'confidence' must be a finite number")
        _require(0.0 <= confidence <= 1.0, name, "'confidence' must be within [0, 1]")
        _validate_annotations(name, entry)


def _ball_or_404(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    """404 unless the ball is known: a non-rejected event or a manual tag (US-D4).

    The same known-ball rule as the reference library. Without it a typo'd PUT
    creates a phantom ball_metrics row for a ball that never existed — garbage in
    summary denominators that ALSO pins US-D4 renumbering, because ball_metrics is
    one of the dependent tables the corrections API refuses to renumber across.
    """
    event_id = db.scalar(
        select(BallEvent.id).where(
            BallEvent.session_id == session_id,
            BallEvent.ball_no == ball_no,
            BallEvent.valid.is_(True),  # rejected events are not balls (US-D4)
        )
    )
    if event_id is not None:
        return
    tag_id = db.scalar(
        select(BallTag.id).where(BallTag.session_id == session_id, BallTag.ball_no == ball_no)
    )
    if tag_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"ball {ball_no} has no ball event or tag in this session",
        )


@router.put("/sessions/{session_id}/balls/{ball_no}/metrics/{phase}")
def put_ball_metrics(
    session_id: uuid.UUID,
    ball_no: BallNo,
    phase: MetricPhase,
    payload: MetricsIn,
    response: Response,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> PhaseMetricsOut:
    _session_or_404(db, session_id, role)
    _ball_or_404(db, session_id, ball_no)
    _validate_metrics(payload.metrics)
    # Row-locked read-modify-write (mirrors the worker's flight/action merges):
    # FOR UPDATE + populate_existing serializes this whole-set replacement with
    # a concurrent job's locked merge of the same row, so the machine-written
    # key survival below copies the job's COMMITTED values, never a stale
    # pre-job read (PostgreSQL; a no-op on the single-writer SQLite unit DBs).
    row = db.execute(
        select(BallMetrics)
        .where(
            BallMetrics.session_id == session_id,
            BallMetrics.ball_no == ball_no,
            BallMetrics.phase == phase,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = BallMetrics(session_id=session_id, ball_no=ball_no, phase=phase)
        db.add(row)
    # Re-running a job replaces the whole phase set (the uq triple is the identity)
    # EXCEPT the machine-written keys of that phase: the US-F5 fusion, US-I2
    # bowling-action and US-I3 bowling-flight jobs merge-write those by key, so
    # an E2/E3/E4 re-run must not silently delete them (survival is mutual). An
    # explicit write to a job-owned key still wins — preserve, never lock.
    metrics = dict(payload.metrics)
    if not created:
        for key in MACHINE_WRITTEN_METRIC_KEYS[phase]:
            if key in row.metrics and key not in metrics:
                metrics[key] = row.metrics[key]
    row.metrics = metrics
    row.schema_version = payload.schema_version
    db.flush()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return PhaseMetricsOut(
        phase=phase, metrics=row.metrics, schema_version=row.schema_version, stored=True
    )


def _machine_speed(db: OrmSession, session: Session, tag: BallTag | None) -> float | None:
    """The ball's machine speed: its tag's block settings first, else the session's."""
    settings: dict[str, Any] | None = session.machine_settings
    if tag is not None and tag.block_id is not None:
        block = db.get(SessionBlock, tag.block_id)
        if block is not None and block.machine_settings is not None:
            settings = block.machine_settings
    speed = settings.get("speed_kph") if settings is not None else None
    return float(speed) if speed is not None else None


def _representative_mark(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> BounceMark | None:
    """The ball's lowest-camera_id bounce mark (same dedupe rule as the heatmap)."""
    return db.scalars(
        select(BounceMark)
        .where(BounceMark.session_id == session_id, BounceMark.ball_no == ball_no)
        .order_by(BounceMark.camera_id)
    ).first()


def _synthesize_flight(db: OrmSession, session: Session, ball_no: int) -> PhaseMetricsOut | None:
    """US-E4 V1: assemble flight metrics from manual sources when nothing is stored."""
    tag = db.scalar(
        select(BallTag).where(BallTag.session_id == session.id, BallTag.ball_no == ball_no)
    )
    mark = _representative_mark(db, session.id, ball_no)
    if tag is None and mark is None:
        return None
    metrics = assemble_flight_metrics(tag, mark, _machine_speed(db, session, tag))
    return PhaseMetricsOut(
        phase=MetricPhase.FLIGHT,
        metrics={name: value.to_payload() for name, value in metrics.items()},
        schema_version=1,
        stored=False,
    )


@router.get("/sessions/{session_id}/balls/{ball_no}/metrics")
def get_ball_metrics(
    session_id: uuid.UUID,
    ball_no: BallNo,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> list[PhaseMetricsOut]:
    session = _session_or_404(db, session_id, role)
    rows = db.scalars(
        select(BallMetrics).where(
            BallMetrics.session_id == session_id, BallMetrics.ball_no == ball_no
        )
    ).all()
    by_phase = {row.phase: row for row in rows}
    out = [
        PhaseMetricsOut(
            phase=phase,
            metrics=by_phase[phase].metrics,
            schema_version=by_phase[phase].schema_version,
            stored=True,
        )
        for phase in MetricPhase
        if phase in by_phase
    ]
    if MetricPhase.FLIGHT not in by_phase:
        synthesized = _synthesize_flight(db, session, ball_no)
        if synthesized is not None:
            out.append(synthesized)
    return out


@router.get("/sessions/{session_id}/metrics/summary")
def metrics_summary(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    phase: MetricPhase | None = None,
) -> SummaryOut:
    """Per-session aggregation: means over numeric values, denominators reported.

    ``mean`` averages the numeric non-null values of a metric (booleans count as
    1.0/0.0, so e.g. ``control`` averages to the control fraction); ``count`` is
    the non-null denominator and ``balls`` the phase population (US-E3 AC).
    """
    _session_or_404(db, session_id, role)
    query = select(BallMetrics).where(BallMetrics.session_id == session_id)
    if phase is not None:
        query = query.where(BallMetrics.phase == phase)
    rows = db.scalars(query).all()
    grouped: dict[MetricPhase, list[BallMetrics]] = defaultdict(list)
    for row in rows:
        grouped[row.phase].append(row)
    phases: list[PhaseSummaryOut] = []
    for group_phase in MetricPhase:
        if group_phase not in grouped:
            continue
        phase_rows = grouped[group_phase]
        names = sorted({name for row in phase_rows for name in row.metrics})
        metrics: list[MetricSummaryOut] = []
        for name in names:
            values = [row.metrics[name].get("value") for row in phase_rows if name in row.metrics]
            non_null = [value for value in values if value is not None]
            numeric = [float(value) for value in non_null if isinstance(value, int | float)]
            mean = round(sum(numeric) / len(numeric), 4) if numeric else None
            metrics.append(MetricSummaryOut(name=name, mean=mean, count=len(non_null)))
        phases.append(PhaseSummaryOut(phase=group_phase, balls=len(phase_rows), metrics=metrics))
    return SummaryOut(session_id=session_id, phases=phases)


@router.get("/sessions/{session_id}/decision-quality")
def get_decision_quality(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
) -> DecisionQualityOut:
    _session_or_404(db, session_id, role)
    tags = {
        tag.ball_no: tag
        for tag in db.scalars(select(BallTag).where(BallTag.session_id == session_id))
    }
    marks: dict[int, BounceMark] = {}
    for mark in db.scalars(
        select(BounceMark)
        .where(BounceMark.session_id == session_id)
        .order_by(BounceMark.ball_no, BounceMark.camera_id)
    ):
        marks.setdefault(mark.ball_no, mark)
    contexts = [
        BallContext(ball_no=tag.ball_no, line=tag.line, length=tag.length, shot=tag.shot)
        for tag in tags.values()
    ]
    contexts.extend(
        BallContext(ball_no=mark.ball_no, line=mark.line, length=mark.length, shot=None)
        for mark in marks.values()
        if mark.ball_no not in tags
    )
    quality = decision_quality(contexts)
    return DecisionQualityOut(
        session_id=session_id,
        outside_off_balls=quality.outside_off_balls,
        leaves=quality.leaves,
        chases=quality.chases,
        unclassified=quality.unclassified,
        zones=[
            ZoneOut(length=zone.length, leaves=zone.leaves, chases=zone.chases, balls=zone.balls)
            for zone in quality.zones
        ],
    )
