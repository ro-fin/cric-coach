"""Real-PostgreSQL proof of the run_analysis verdict guard (US-J1/G6).

SQLite can't enforce the ``evidence_verdicts.finding_id`` foreign key, so only
real PostgreSQL shows the failure this guard replaces: without it, a plain
pipeline re-run's delete-then-insert would hit an FK IntegrityError (or, on a
non-enforcing engine, silently orphan a coach's review). Here the re-run blocks
cleanly with the row untouched, and the audited re-derive path is what clears
it — after which analysis runs again.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    Contact,
    EvidenceVerdict,
    Footwork,
    Length,
    Line,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import (
    BallTag,
    CoachingRule,
    EvidenceVerdictRecord,
    Finding,
    Player,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.agent_stages import AnalysisBlockedError, run_analysis
from cricai_worker.context import WorkerContext
from cricai_worker.rederive import rederive_session
from sqlalchemy import func, select

SESSION_DATE = date(2026, 7, 7)

RULE_DEFINITION = {
    "metric": "control",
    "op": "eq",
    "value": False,
    "condition": {"line": ["leg"]},
    "min_n": 10,
    "severity": "minor",
    "text_data": {"correction": "Keep your head still on short balls at leg stump."},
}


def _ctx(pg_url: str, tmp_path: Path) -> WorkerContext:
    engine = make_engine(pg_url)
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )


def _seed(ctx: WorkerContext) -> uuid.UUID:
    """A session with 10 uncontrolled leg-stump balls + the leg-control rule."""
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = SessionRow(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        for ball_no in range(1, 11):
            db.add(
                BallTag(
                    session_id=session.id,
                    ball_no=ball_no,
                    line=Line.LEG,
                    length=Length.SHORT,
                    shot=Shot.PULL,
                    footwork=Footwork.BACK,
                    contact=Contact.EDGE,
                    outcome=Outcome.EDGED,
                    control=False,
                    created_by="coach",
                )
            )
        db.add(
            CoachingRule(
                rule_key="leg_control",
                version=1,
                author="coach",
                approved_by="coach",
                rationale="Head still against short balls at leg stump.",
                definition=RULE_DEFINITION,
                enabled=True,
            )
        )
        db.commit()
        return session.id


@pytest.mark.integration
def test_verdict_guard_blocks_cleanly_then_rederive_clears(pg_url: str, tmp_path: Path) -> None:
    ctx = _ctx(pg_url, tmp_path)
    session_id = _seed(ctx)

    assert run_analysis(ctx, session_id)["finding_count"] >= 1
    with ctx.session_factory() as db:
        finding = db.scalar(select(Finding).where(Finding.session_id == session_id))
        assert finding is not None
        finding_id = finding.id
        db.add(
            EvidenceVerdictRecord(
                finding_id=finding_id, verdict=EvidenceVerdict.NOT_SUPPORTED, actor="coach"
            )
        )
        db.commit()

    # A plain pipeline re-run blocks — no FK IntegrityError, no orphaned verdict.
    with pytest.raises(AnalysisBlockedError):
        run_analysis(ctx, session_id)
    with ctx.session_factory() as db:
        assert db.get(Finding, finding_id) is not None  # finding survives intact
        assert db.scalar(select(func.count()).select_from(EvidenceVerdictRecord)) == 1

    # The audited re-derive path (confirm required — the seed has manual tags)
    # cascades the verdict and findings; analysis then runs again without blocking.
    summary = rederive_session(ctx, session_id, actor="coach", confirm_manual_invalidation=True)
    assert summary.evidence_verdicts_deleted == 1
    assert summary.findings_deleted >= 1

    result = run_analysis(ctx, session_id)
    assert result["finding_count"] == 0  # tags were cleared, so nothing to derive
    with ctx.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(EvidenceVerdictRecord)) == 0


@pytest.mark.integration
def test_reanalysis_over_unchanged_data_keeps_stable_finding_ids(
    pg_url: str, tmp_path: Path
) -> None:
    """Finding 38: findings are delete-then-insert, but the row id is a
    deterministic function of (session, content), so re-running over unchanged
    data re-mints IDENTICAL ids — plan/report references never dangle."""
    ctx = _ctx(pg_url, tmp_path)
    session_id = _seed(ctx)

    first = run_analysis(ctx, session_id)
    assert first["finding_count"] >= 1
    with ctx.session_factory() as db:
        first_ids = {
            str(row.id)
            for row in db.scalars(select(Finding).where(Finding.session_id == session_id))
        }

    second = run_analysis(ctx, session_id)
    assert second["finding_count"] == first["finding_count"]
    with ctx.session_factory() as db:
        second_ids = {
            str(row.id)
            for row in db.scalars(select(Finding).where(Finding.session_id == session_id))
        }
    assert second_ids == first_ids  # stable ids over a re-run of identical data
