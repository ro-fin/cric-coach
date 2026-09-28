"""Nightly baseline job (US-J4/US-G5): ball_metrics -> frozen ``metric_baselines``.

For every session date of one player, computes the per-metric mean over that
date's machine-source metric values and upserts one ``metric_baselines`` row
per (metric, zone_key, window="session", snapshot_date). Every value lands in
the ``zone_key="all"`` slice (the Phase-5 behaviour, unchanged); a ball whose
manual tag names its line x length ALSO lands in a per-zone slice keyed
``"<line>/<length>"`` (e.g. ``"off/good"``) — the context-normalized series
US-G5 regression alerts compare against, so "worse technique" (same zone
regressing) is distinguishable from "harder ball mix" (only the mix-sensitive
``all`` slice moved). Slices whose source data is no longer eligible — a
session flagged ``calibration_suspect`` after it was baselined, a metric whose
machine values vanished, or a zone whose tags were re-labelled — are DELETED,
so every run converges to the state a fresh run would produce. Idempotent via
the ``uq_baseline_slice_snapshot`` unique constraint: a re-run updates the
same rows in place, so a crash-resume is a plain re-run.

Each row's ``payload`` carries the contributing ``session_ids`` list plus the
scalar ``session_id`` (the lone contributor, or null when several sessions
merged into the date) — the key ``cricai_coaching.progress_agent`` reads to
give every ProgressSnapshot point its session linkage (pinned contract #7).

Machine-source only (US-J4/US-G5): a metric value entry counts when its
``source`` provenance is NOT ``"manual"`` — machine writers omit ``source``
or stamp ``auto_*`` (the ``cricai_coaching.contact_metrics`` convention),
while human-entered values are exactly ``"manual"``. Manual values power the
tags/decision views, not longitudinal baselines. Numeric values average as
floats and booleans as the fraction true (the metrics-summary idiom); null
and class/list values are skipped — a class label has no mean.

Sessions flagged ``calibration_suspect`` are excluded entirely (US-G5:
suspect data never feeds trend claims). Report generation never calls this
job — it reads the stored rows via ``cricai_coaching.progress_agent``, so the
snapshot it sees is frozen (US-J4 AC).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from cricai_data.db import session_scope
from cricai_data.models import BallMetrics, BallTag, MetricBaseline, Player
from cricai_data.models import Session as SessionRow
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext

#: Provenance value that marks a human-entered metric (never baselined here).
MANUAL_SOURCE = "manual"

#: Baseline slice constants: session-window snapshots; ``all`` aggregates every
#: ball, per-zone slices key ``"<line>/<length>"`` from the ball's manual tag.
ZONE_ALL = "all"
WINDOW_SESSION = "session"


@dataclass(frozen=True)
class BaselineSummary:
    """What one run did: rows written (created/updated/deleted), values seen.

    ``values`` counts underlying machine metric values once each (the ``all``
    slice tally); a value that also feeds a per-zone slice is not re-counted.
    """

    player_id: str
    created: int
    updated: int
    deleted: int
    values: int


def _machine_value(entry: object) -> float | None:
    """The averageable float of one metric entry, or None to skip it.

    Skips non-dict entries, manual provenance, nulls and class/list values;
    booleans average as 1.0/0.0 (fraction true, the summary idiom).
    """
    if not isinstance(entry, dict) or entry.get("source") == MANUAL_SOURCE:
        return None
    value = entry.get("value")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    return None


def _zone_lookup(db: Session, session_id: uuid.UUID) -> dict[int, str]:
    """ball_no -> ``"<line>/<length>"`` zone key from the session's manual tags.

    Untagged balls have no zone: their values feed only the ``all`` slice
    (US-G5 — a zone slice never guesses its context).
    """
    return {
        tag.ball_no: f"{tag.line.value}/{tag.length.value}"
        for tag in db.scalars(select(BallTag).where(BallTag.session_id == session_id))
    }


def _date_values(
    db: Session, sessions: list[SessionRow]
) -> dict[date, dict[str, dict[str, list[float]]]]:
    """zone -> metric -> machine values grouped by session date, in stable order.

    Every value lands in :data:`ZONE_ALL`; a tagged ball's value additionally
    lands in its line x length zone (the US-G5 context-normalized slice).
    """
    grouped: dict[date, dict[str, dict[str, list[float]]]] = {}
    for session in sessions:
        rows = db.scalars(
            select(BallMetrics)
            .where(BallMetrics.session_id == session.id)
            .order_by(BallMetrics.ball_no, BallMetrics.phase)
        ).all()
        zones = _zone_lookup(db, session.id)
        by_zone = grouped.setdefault(session.session_date, {})
        for row in rows:
            zone = zones.get(row.ball_no)
            for name in sorted(row.metrics):
                value = _machine_value(row.metrics[name])
                if value is None:
                    continue
                by_zone.setdefault(ZONE_ALL, {}).setdefault(name, []).append(value)
                if zone is not None:
                    by_zone.setdefault(zone, {}).setdefault(name, []).append(value)
    return grouped


@dataclass(frozen=True)
class _SlicePoint:
    """One baseline slice about to be written: identity plus its values."""

    metric: str
    zone_key: str
    snapshot_date: date
    values: tuple[float, ...]
    session_ids: tuple[str, ...]


def _upsert(db: Session, player_id: uuid.UUID, point: _SlicePoint) -> bool:
    """Upsert one baseline row on its unique slice; True = created."""
    row = db.scalar(
        select(MetricBaseline).where(
            MetricBaseline.player_id == player_id,
            MetricBaseline.metric == point.metric,
            MetricBaseline.zone_key == point.zone_key,
            MetricBaseline.window == WINDOW_SESSION,
            MetricBaseline.snapshot_date == point.snapshot_date,
        )
    )
    created = row is None
    if row is None:
        row = MetricBaseline(
            player_id=player_id,
            metric=point.metric,
            zone_key=point.zone_key,
            window=WINDOW_SESSION,
            snapshot_date=point.snapshot_date,
        )
        db.add(row)
    row.value = round(sum(point.values) / len(point.values), 4)
    row.n = len(point.values)
    row.payload = {
        # Contract #7 session linkage: the lone contributor, else null.
        "session_id": point.session_ids[0] if len(point.session_ids) == 1 else None,
        "session_ids": list(point.session_ids),
    }
    return created


def _delete_stale(
    db: Session, player_id: uuid.UUID, fresh: set[tuple[str, str, date]], as_of: date | None
) -> int:
    """Delete this job's slices whose (metric, zone, snapshot_date) was not recomputed.

    A slice disappears from the fresh set when its source data stopped being
    eligible (session flagged ``calibration_suspect`` after baselining, machine
    values re-attributed to a human, a re-derive that dropped the metric, a
    zone whose tags were re-labelled) — the stale row must not keep feeding
    trend claims (US-G5). This job owns every ``window="session"`` slice, all
    zones included. An ``as_of``-bounded run only reconciles slices inside its
    own horizon: later snapshots are out of scope, not stale.
    """
    query = select(MetricBaseline).where(
        MetricBaseline.player_id == player_id,
        MetricBaseline.window == WINDOW_SESSION,
    )
    if as_of is not None:
        query = query.where(MetricBaseline.snapshot_date <= as_of)
    deleted = 0
    for row in db.scalars(query):
        if (row.metric, row.zone_key, row.snapshot_date) not in fresh:
            db.delete(row)
            deleted += 1
    return deleted


def compute_nightly_baselines(
    ctx: WorkerContext, player_id: uuid.UUID, *, as_of: date | None = None
) -> BaselineSummary:
    """Recompute one player's frozen baseline snapshots (US-J4 nightly job).

    ``as_of`` bounds the sessions considered (nightly runs pass today); the
    job is idempotent — identical inputs upsert identical rows via the
    baseline unique constraint, never duplicates — and convergent: slices
    whose (metric, zone_key, snapshot_date) is absent from the freshly
    computed set (source data no longer eligible) are deleted within the
    run's horizon.
    """
    created = updated = values_seen = 0
    with session_scope(ctx.session_factory) as db:
        player = db.get(Player, player_id)
        if player is None:
            raise ValueError(f"player not found: {player_id}")
        query = (
            select(SessionRow)
            .where(
                SessionRow.player_id == player_id,
                SessionRow.calibration_suspect.is_(False),  # US-G5 exclusion
            )
            .order_by(SessionRow.session_date, SessionRow.created_at)
        )
        if as_of is not None:
            query = query.where(SessionRow.session_date <= as_of)
        sessions = list(db.scalars(query))
        sessions_by_date: dict[date, list[str]] = {}
        for session in sessions:
            sessions_by_date.setdefault(session.session_date, []).append(str(session.id))
        fresh: set[tuple[str, str, date]] = set()
        for snapshot_date, by_zone in sorted(_date_values(db, sessions).items()):
            session_ids = tuple(sorted(sessions_by_date[snapshot_date]))
            for zone_key in sorted(by_zone):
                by_metric = by_zone[zone_key]
                for metric in sorted(by_metric):
                    values = by_metric[metric]
                    if zone_key == ZONE_ALL:  # count each underlying value once
                        values_seen += len(values)
                    point = _SlicePoint(
                        metric=metric,
                        zone_key=zone_key,
                        snapshot_date=snapshot_date,
                        values=tuple(values),
                        session_ids=session_ids,
                    )
                    fresh.add((metric, zone_key, snapshot_date))
                    if _upsert(db, player_id, point):
                        created += 1
                    else:
                        updated += 1
        deleted = _delete_stale(db, player_id, fresh, as_of)
        db.flush()
    return BaselineSummary(
        player_id=str(player_id),
        created=created,
        updated=updated,
        deleted=deleted,
        values=values_seen,
    )
