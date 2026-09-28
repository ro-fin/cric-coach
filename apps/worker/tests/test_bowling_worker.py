"""US-I7 worker wiring: the analysis stage's bowling branch (leg-spin probes
alongside the US-G2 rules) and the report job's bowling dispatch (leg-spin
body: accuracy scorecard, release scatter, honest variation agreement,
Warne/Saqlain modules, always-present US-H1 workload block), plus the writer
guard treating the bowling section as structural.

SQLite-backed unit tests (the ``test_agent_stages.py`` pattern)."""

from __future__ import annotations

import copy
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from cricai_coaching.bowling_analysis import BOWLING_FINDING_KINDS
from cricai_coaching.bowling_report import BOWLING_BODY_KEY
from cricai_coaching.contracts import validate_report_body
from cricai_coaching.report import HONESTY_CLEAN
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    BowlingVariation,
    ClipStatus,
    DeliveryIntensity,
    MetricPhase,
    SessionType,
)
from cricai_data.models import (
    BallEvent,
    BallMetrics,
    BowlingLedgerEntry,
    Clip,
    CoachingRule,
    DeliveryLabel,
    Finding,
    Player,
    Report,
    SafetyConfig,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.agent_stages import run_analysis
from cricai_worker.context import WorkerContext
from cricai_worker.generate_report import generate_report
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

SESSION_DATE = date(2026, 7, 7)

#: 24 balls: enough for every probe under the DEFAULT config (checkpoint halves
#: of 12 >= min_sample 10) and for the seed rule's min_n 12.
BALL_COUNT = 24

#: The US-I7 seed rule exercised through the ordinary DSL (mirrors the YAML).
BOWLING_RULE_DEFINITION: dict[str, Any] = {
    "metric": "length",
    "op": "eq",
    "value": "short",
    "condition": {"mode": ["bowling"]},
    "min_n": 12,
    "severity": "major",
    "text_data": {
        "finding": "A lot of your deliveries are landing short of the target length.",
        "correction": "Give the ball more flight and land it fuller.",
    },
}


@pytest.fixture
def ctx(tmp_path: Any) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )


def _payload(value: Any) -> dict[str, Any]:
    return {"value": value, "confidence": 0.9, "source": "auto"}


def _seed_bowling_session(
    ctx: WorkerContext,
    *,
    with_rule: bool = True,
    with_metrics: bool = True,
    with_clips: bool = True,
    session_type: SessionType = SessionType.BOWLING,
) -> uuid.UUID:
    """A leg-spin session whose records fire every probe and the seed rule.

    Per ball: release heights alternate 150/200 cm (sigma 25 >= the 8 cm gate);
    the front-leg brace holds for the first half then collapses (checkpoint
    trend -1); leg-breaks (balls 1-12) hit the target far more than googlies
    (balls 13-24); detections agree on leg-breaks only (50%% agreement, below
    the 80%% gate); every ball lands short (the seed rule fires).
    """
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = SessionRow(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=session_type,
            bowler_source=BowlerSource.HUMAN,
        )
        db.add(session)
        db.flush()
        for index in range(BALL_COUNT):
            ball_no = index + 1
            leg_break = ball_no <= BALL_COUNT // 2
            db.add(
                BallEvent(
                    session_id=session.id,
                    ball_no=ball_no,
                    start_ms=ball_no * 4000,
                    release_ms=ball_no * 4000 + 500,
                    end_ms=ball_no * 4000 + 3000,
                    confidence=0.9,
                )
            )
            if with_metrics:
                db.add(
                    BallMetrics(
                        session_id=session.id,
                        ball_no=ball_no,
                        phase=MetricPhase.PRE_RELEASE,
                        metrics={
                            "release_height_cm": _payload(150.0 if ball_no % 2 else 200.0),
                            "brace_state": _payload(
                                "braced" if ball_no <= BALL_COUNT // 2 else "collapsed"
                            ),
                        },
                    )
                )
                db.add(
                    BallMetrics(
                        session_id=session.id,
                        ball_no=ball_no,
                        phase=MetricPhase.FLIGHT,
                        metrics={
                            "length": _payload("short"),
                            "target_hit": _payload(
                                (ball_no % 6 != 0) if leg_break else (ball_no % 6 == 0)
                            ),
                        },
                    )
                )
                db.add(
                    DeliveryLabel(
                        session_id=session.id,
                        ball_no=ball_no,
                        variation_intent=(
                            BowlingVariation.LEG_BREAK if leg_break else BowlingVariation.GOOGLY
                        ),
                        variation_detected=BowlingVariation.LEG_BREAK,
                        labeler="coach",
                    )
                )
            if with_clips and ball_no <= 3:
                for camera_id in ("C5", "C6"):
                    db.add(
                        Clip(
                            session_id=session.id,
                            ball_no=ball_no,
                            camera_id=camera_id,
                            object_key=f"clip/{ball_no}/{camera_id}",
                            start_ms=ball_no * 4000,
                            end_ms=ball_no * 4000 + 3000,
                            status=ClipStatus.CUT,
                        )
                    )
        if with_rule:
            db.add(
                CoachingRule(
                    rule_key="bowling_short_of_target_length",
                    version=1,
                    author="coach",
                    approved_by="coach",
                    rationale="short balls sit up",
                    enabled=True,
                    definition=BOWLING_RULE_DEFINITION,
                )
            )
        db.commit()
        return session.id


def _finding_rows(ctx: WorkerContext, session_id: uuid.UUID) -> list[Finding]:
    with ctx.session_factory() as db:
        return list(db.scalars(select(Finding).where(Finding.session_id == session_id)))


def _report_body(ctx: WorkerContext, report_id: uuid.UUID) -> dict[str, Any]:
    with ctx.session_factory() as db:
        report = db.scalars(select(Report).where(Report.id == report_id)).one()
        return dict(report.body)


class TestAnalysisStageBowlingBranch:
    def test_bowling_session_persists_bowling_kind_findings(self, ctx: WorkerContext) -> None:
        """US-I7: session_type bowling -> leg-spin probes alongside the rules."""
        session_id = _seed_bowling_session(ctx)
        result = run_analysis(ctx, session_id)
        assert result["finding_count"] > 0
        rows = _finding_rows(ctx, session_id)
        kinds = {row.kind for row in rows}
        assert kinds <= {"rule", *BOWLING_FINDING_KINDS}
        assert kinds & set(BOWLING_FINDING_KINDS)  # probes actually fired
        assert any(row.rule_key == "bowling_short_of_target_length" for row in rows)
        probe_rows = [row for row in rows if row.kind in BOWLING_FINDING_KINDS]
        for row in probe_rows:
            assert row.agent == "bowling_analysis"
            assert row.payload["probe"] == row.kind  # probe origin survives persist

    def test_bowling_analysis_reruns_idempotently(self, ctx: WorkerContext) -> None:
        session_id = _seed_bowling_session(ctx)
        first = run_analysis(ctx, session_id)
        second = run_analysis(ctx, session_id)
        assert first == second  # deterministic ids, delete-then-insert

    def test_bowling_session_without_metrics_yields_no_probe_findings(
        self, ctx: WorkerContext
    ) -> None:
        session_id = _seed_bowling_session(ctx, with_rule=False, with_metrics=False)
        result = run_analysis(ctx, session_id)
        assert result == {"finding_count": 0, "finding_ids": []}


class TestGenerateReportBowlingDispatch:
    def test_bowling_report_carries_the_legspin_section(self, ctx: WorkerContext) -> None:
        """US-I7: bowling sessions assemble via bowling_report (all blocks)."""
        session_id = _seed_bowling_session(ctx)
        run_analysis(ctx, session_id)
        result = generate_report(ctx, session_id)
        body = _report_body(ctx, result.report_id)
        validate_report_body(body)  # contract #4 holds with the additive key
        section = body[BOWLING_BODY_KEY]
        card = section["accuracy_scorecard"]
        assert card["overall"]["n"] == BALL_COUNT  # denominator shown (US-I4)
        assert {row["variation"] for row in card["by_variation"]} == {"leg_break", "googly"}
        assert section["release_scatter"]["n"] == BALL_COUNT
        assert section["release_scatter"]["sigma_cm"] == 25.0
        assert section["variation_agreement"]["agreement_pct"] == 50.0
        assert section["variation_agreement"]["compared"] == BALL_COUNT
        module_kinds = {module["kind"] for module in section["learning_modules"]}
        assert module_kinds and module_kinds <= set(BOWLING_FINDING_KINDS)
        for module in section["learning_modules"]:
            # US-I7 honesty: no coach sign-off is recorded yet, so the payload
            # must say so — never a fabricated "coach" endorsement.
            assert module["approved_by"] == "pending_coach_review"
        assert body["main_correction"] is not None  # findings met the bar
        assert not result.honest

    def test_workload_block_always_present_with_week_to_date_overs(
        self, ctx: WorkerContext
    ) -> None:
        """US-I7 AC: the report always shows week-to-date overs vs ceiling."""
        session_id = _seed_bowling_session(ctx, with_rule=False, with_metrics=False)
        result = generate_report(ctx, session_id)
        body = _report_body(ctx, result.report_id)
        assert body["honesty_banner"] == HONESTY_CLEAN  # no findings at all
        block = body[BOWLING_BODY_KEY]["workload"]
        # The session's own 24 balls were ensured into the ledger: 4.0 overs.
        assert block["weighted_overs"] == 4.0
        assert block["ceiling_overs"] == 16.0  # age-11 band default
        assert block["remaining_balls"] == 72
        assert block["violations"] == []
        assert block["window"]["end"] == SESSION_DATE.isoformat()

    @pytest.mark.safety
    def test_workload_block_shows_ceiling_breach_saf(self, ctx: WorkerContext) -> None:
        """SAF: a ceiling breach is visible even on the honesty path (US-H1/I7)."""
        session_id = _seed_bowling_session(ctx, with_rule=False, with_metrics=False)
        with ctx.session_factory() as db:
            session = db.get(SessionRow, session_id)
            assert session is not None
            db.add(
                BowlingLedgerEntry(
                    player_id=session.player_id,
                    entry_date=SESSION_DATE - timedelta(days=2),
                    balls=90,
                    intensity=DeliveryIntensity.SPIN,
                    created_by="coach",
                )
            )
            db.commit()
        result = generate_report(ctx, session_id)
        body = _report_body(ctx, result.report_id)
        block = body[BOWLING_BODY_KEY]["workload"]
        assert block["weighted_overs"] == 19.0  # (90 + 24) / 6
        assert "workload_ceiling" in block["violations"]
        assert block["remaining_balls"] == 0
        assert body["honesty_banner"] == HONESTY_CLEAN  # block ships regardless

    def test_workload_block_reads_the_active_safety_config(self, ctx: WorkerContext) -> None:
        session_id = _seed_bowling_session(ctx, with_rule=False, with_metrics=False)
        with ctx.session_factory() as db:
            config = copy.deepcopy(
                {
                    "workload": {
                        "age_bands": [
                            {
                                "max_age": 13,
                                "weekly_overs_target": [16, 20],
                                "weekly_overs_ceiling": 20,
                            }
                        ],
                        "balls_per_over": 6,
                        "max_bowling_days_per_rolling_7": 4,
                        "max_consecutive_day_pairs_per_rolling_7": 1,
                        "intensity_weights": {"spin": 1.0},
                    },
                    "wellness": {
                        "pain_escalation_count": 2,
                        "pain_escalation_window_days": 14,
                    },
                }
            )
            db.add(SafetyConfig(version=2, config=config, approved_by="coach", reason="test"))
            db.commit()
        result = generate_report(ctx, session_id)
        body = _report_body(ctx, result.report_id)
        assert body[BOWLING_BODY_KEY]["workload"]["ceiling_overs"] == 20.0

    def test_batting_session_body_is_unchanged(self, ctx: WorkerContext) -> None:
        """The batting path never grows a bowling section (byte-identical)."""
        session_id = _seed_bowling_session(
            ctx, with_rule=False, with_metrics=False, session_type=SessionType.BATTING
        )
        result = generate_report(ctx, session_id)
        body = _report_body(ctx, result.report_id)
        assert BOWLING_BODY_KEY not in body


class _TamperBowlingWriter:
    """Test double: rewrites a bowling measurement (must be rejected)."""

    def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        worded = copy.deepcopy(body)
        worded[BOWLING_BODY_KEY]["workload"] = {"weighted_overs": 0.0}  # lie
        return worded


class _RewordingWriter:
    """Test double: rewords only the positive line (must be accepted)."""

    def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        worded = copy.deepcopy(body)
        worded["positive"] = "Lovely energy at the crease today - well bowled."
        return worded


class TestWriterGuardBowlingSection:
    def test_writer_tampering_with_bowling_data_falls_back(self, ctx: WorkerContext) -> None:
        """The leg-spin blocks are structural: measurements are not wording."""
        session_id = _seed_bowling_session(ctx, with_rule=False, with_metrics=False)
        result = generate_report(ctx, session_id, writer=_TamperBowlingWriter())
        assert result.writer == "rule_based"
        assert result.fallback_reason is not None
        assert "bowling" in result.fallback_reason
        body = _report_body(ctx, result.report_id)
        assert body[BOWLING_BODY_KEY]["workload"]["weighted_overs"] == 4.0  # truth kept

    def test_rewording_writer_is_accepted_on_a_bowling_body(self, ctx: WorkerContext) -> None:
        session_id = _seed_bowling_session(ctx, with_rule=False, with_metrics=False)
        result = generate_report(ctx, session_id, writer=_RewordingWriter())
        assert result.writer == "injected"
        assert result.fallback_reason is None
        body = _report_body(ctx, result.report_id)
        assert body["positive"] == "Lovely energy at the crease today - well bowled."
