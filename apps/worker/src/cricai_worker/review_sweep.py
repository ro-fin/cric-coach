"""US-J5: the review-gate timeout sweep — default-publish with notice, never silent.

In ``coach_gate`` mode a generated report stays DRAFT carrying a
``review_due_at`` deadline (set from ``cricai_coaching.review_gate``). When
the deadline passes with no coach action, this sweep pushes the report
THROUGH the publish validation path — the same hash/claims/coverage/evidence/
live-safety-state gate a manual publish runs (``cricai_api.routers.reports``).
A report failing the gate at timeout lands in BLOCKED with its reasons
audited, exactly as a manual publish would; it is NEVER silently published.
Reports a coach already decided (published, blocked, or edited-and-republished)
are no longer DRAFT and are never touched.

The gate arrives as an injected callable because the publish validation path
lives in ``cricai_api`` (claim recompute + live H1/H4 state re-check), which
this package does not hard-depend on. There is deliberately no default: a
sweep with no gate cannot run. The production composition is
``cricai_worker.scheduled_jobs.run_review_sweep`` (``publish_reasons`` +
``default_recompute``, deferred import) — the cron-callable entrypoint —
proven end-to-end by ``apps/api/tests/test_review_sweep_wiring.py``.

Every decision writes an AuditLog row under the same action names the manual
publish endpoint uses (``report_published`` / ``report_publish_blocked``) with
``timeout: true`` detail, so US-J5's "default-publish + notice" is one audit
query away and the trail stays complete.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from cricai_coaching.review_gate import timeout_expired
from cricai_data.db import session_scope
from cricai_data.enums import ReportStatus
from cricai_data.models import AuditLog, Report, utcnow
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext

#: audit_log actor for sweep decisions (distinguishes them from manual ones).
AUDIT_ACTOR = "worker:review_sweep"

#: The publish validation path, injected: ``(db, report) -> reasons`` where an
#: empty list means publishable (the ``publish_reasons`` contract).
PublishGate = Callable[[OrmSession, Report], list[str]]


@dataclass(frozen=True)
class SweepSummary:
    """What one sweep did (job summary, test surface)."""

    due: int
    published: int
    blocked: int
    report_ids: tuple[uuid.UUID, ...]


def _as_utc(value: datetime) -> datetime:
    """Stored timestamps are UTC; some dialects (SQLite) round-trip them naive."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _decide(db: OrmSession, report: Report, reasons: list[str], due_at: datetime) -> bool:
    """Apply one gate outcome; True when published. Audited either way."""
    if reasons:
        report.status = ReportStatus.BLOCKED
        action = "report_publish_blocked"
        detail: dict[str, object] = {"reasons": reasons}
    else:
        report.status = ReportStatus.PUBLISHED
        action = "report_published"
        detail = {"safety_sha256": report.safety_sha256}
    detail["timeout"] = True
    detail["review_due_at"] = due_at.isoformat()
    db.add(
        AuditLog(
            actor=AUDIT_ACTOR,
            action=action,
            entity="report",
            entity_id=str(report.id),
            detail=detail,
        )
    )
    return not reasons


def sweep_due_reports(
    ctx: WorkerContext, publish_reasons: PublishGate, *, now: datetime | None = None
) -> SweepSummary:
    """Publish-or-block every gate-held draft whose deadline has passed (US-J5).

    Idempotent: a decided report leaves DRAFT, so a re-run (or a crash-resume)
    only ever sees the still-undecided remainder.
    """
    at = now if now is not None else utcnow()
    if at.tzinfo is None:
        raise ValueError(f"now must be timezone-aware UTC, got naive {at!r}")
    published = blocked = 0
    decided_ids: list[uuid.UUID] = []
    with session_scope(ctx.session_factory) as db:
        held = db.scalars(
            select(Report)
            .where(Report.status == ReportStatus.DRAFT)
            .order_by(Report.review_due_at, Report.id)
        ).all()
        for report in held:
            due_raw = report.review_due_at
            if due_raw is None:  # never gate-held (drafted under auto_publish)
                continue
            due_at = _as_utc(due_raw)
            if not timeout_expired(due_at, at):
                continue
            if _decide(db, report, publish_reasons(db, report), due_at):
                published += 1
            else:
                blocked += 1
            decided_ids.append(report.id)
    return SweepSummary(
        due=len(decided_ids),
        published=published,
        blocked=blocked,
        report_ids=tuple(decided_ids),
    )
