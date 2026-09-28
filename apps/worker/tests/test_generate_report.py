"""US-G3 acceptance: the daily-report job — assembly, writer seam, verbatim
safety insertion + hashing, idempotent upsert, honest defaults."""

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import cricai_worker.generate_report as gr_module
import pytest
from cricai_coaching import contracts
from cricai_coaching.fatigue import FatigueConfig
from cricai_coaching.llm import FakeLLMProvider
from cricai_coaching.llm_writer import LLMReportWriter
from cricai_coaching.report import DEFAULT_POSITIVE, HONESTY_CLEAN
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    Contact,
    Footwork,
    Length,
    Line,
    MetricPhase,
    Outcome,
    ReportKind,
    ReportStatus,
    SessionType,
    Shot,
)
from cricai_data.models import (
    AppSetting,
    AuditLog,
    BallMetrics,
    BallTag,
    Finding,
    Player,
    Report,
    ReportLLMAudit,
    SafetyConfig,
    Session,
    SessionBlock,
)
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.generate_report import (
    AUDIT_ACTOR,
    REPORT_FATIGUE_DIRECTIONS,
    finding_to_contract,
    generate_report,
)
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

SESSION_DATE = date(2026, 7, 9)

#: Small fatigue config for seeded tests: 10-ball window over 10-ball baseline,
#: monitoring the two numeric technique metrics the BallRecord carries.
FATIGUE_CFG = FatigueConfig(
    window=10, min_baseline=10, min_metric_n=5, directions=dict(REPORT_FATIGUE_DIRECTIONS)
)


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine), store=FsObjectStore(tmp_path / "store")
    )


def _seed_session(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.commit()
        return session.id


def _seed_finding(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    severity: str = "major",
    n: int = 20,
    metric: str = "control_pct",
    clips: int = 2,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    evidence = {str(i + 1): {"C1": f"clip-{session_id.hex[:4]}-{i}"} for i in range(clips)}
    with ctx.session_factory() as db:
        finding = Finding(
            session_id=session_id,
            agent="rules",
            rule_key="front_foot_stride",
            kind="technique",
            severity=severity,
            metric=metric,
            condition={"line": "outside_off"},
            n=n,
            confidence=0.9,
            ball_ids=list(range(1, n + 1)),
            evidence=evidence,
            payload=payload
            if payload is not None
            else {"text_data": {"correction": "Move your front foot to the ball."}},
        )
        db.add(finding)
        db.commit()
        return finding.id


def _get_report(ctx: WorkerContext, report_id: uuid.UUID) -> Report:
    with ctx.session_factory() as db:
        report = db.get(Report, report_id)
        assert report is not None
        db.expunge(report)
        return report


def _safety(text: str = "Stop bowling this week.") -> dict[str, Any]:
    return {
        "active": True,
        "codes": ["workload_ceiling"],
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


@dataclass
class RecordingWriter:
    """A well-behaved injected writer: rewords, keeps numbers/keys/safety."""

    contexts: list[dict[str, Any]] = field(default_factory=list)

    def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        self.contexts.append(context)
        if body["main_correction"] is not None:
            body["main_correction"]["text"] = "Coach says: " + body["main_correction"]["text"]
        body["positive"] = "Loved the energy out there."
        return body


@dataclass
class TamperingWriter:
    """Breaks the ReportWriter contract in one configurable way."""

    mutate: Any

    def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        return self.mutate(body)


class ExplodingWriter:
    def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del body, context
        raise RuntimeError("LLM provider down")


def test_unknown_session_raises() -> None:
    ctx_missing = WorkerContext(
        session_factory=make_session_factory(
            _make_engine := create_engine(
                "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        ),
        store=FsObjectStore(Path("/tmp/unused-store")),
    )
    create_all(_make_engine)
    with pytest.raises(ValueError, match="session not found"):
        generate_report(ctx_missing, uuid.uuid4())


def test_no_findings_ships_the_honesty_report(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    result = generate_report(ctx, session_id)
    assert result.created
    assert result.writer == "rule_based"
    assert result.fallback_reason is None
    assert result.honest
    report = _get_report(ctx, result.report_id)
    assert report.kind is ReportKind.DAILY
    assert report.status is ReportStatus.DRAFT
    assert report.period_start == SESSION_DATE
    assert report.period_end == SESSION_DATE
    assert report.safety_sha256 is None
    assert report.quality is None
    assert report.body["honesty_banner"] == HONESTY_CLEAN
    assert report.body["main_correction"] is None
    assert report.body["claims"] == []


def test_findings_produce_the_pinned_body(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    finding_id = _seed_finding(
        ctx,
        session_id,
        payload={
            "text_data": {"correction": "Move your front foot to the ball."},
            "value": 57.0,
            "target": 70,
        },
    )
    result = generate_report(ctx, session_id)
    body = _get_report(ctx, result.report_id).body
    assert not result.honest
    assert body["main_correction"]["finding_id"] == str(finding_id)
    assert "Seen on 20 balls." in body["main_correction"]["text"]
    assert body["goal"]["target"] == 70.0
    assert {c["recompute_key"] for c in body["claims"]} == {
        f"finding:{finding_id}:n",
        f"finding:{finding_id}:value",
        f"finding:{finding_id}:target",
    }


def test_finding_to_contract_projects_every_field(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    with ctx.session_factory() as db:
        row = db.scalars(select(Finding)).one()
        contract = finding_to_contract(row)
    assert contract["finding_id"] == str(row.id)
    assert contract["severity"] == "major"
    assert contract["text_data"] == {"correction": "Move your front foot to the ball."}
    assert contract["ball_ids"] == list(range(1, 21))
    assert contract["evidence"] == row.evidence


def test_finding_to_contract_surfaces_probe_origin(ctx: WorkerContext) -> None:
    """A probe finding (no rule_key) surfaces its origin as a top-level 'probe'
    key, so the read-back shape satisfies contract #2 — mirroring
    ``agent_stages._finding_to_contract`` (finding 47 parity)."""
    session_id = _seed_session(ctx)
    with ctx.session_factory() as db:
        db.add(
            Finding(
                session_id=session_id,
                agent="analysis",
                rule_key=None,
                kind="zone_contrast",
                severity="info",
                metric="control",
                condition={},
                n=10,
                effect_size=0.3,
                confidence=0.9,
                ball_ids=[1],
                evidence={},
                payload={"text_data": {}, "finding_id": "an-abc", "probe": "zone_contrast"},
            )
        )
        db.commit()
    with ctx.session_factory() as db:
        row = db.scalars(select(Finding)).one()
        contract = finding_to_contract(row)
    assert contract["probe"] == "zone_contrast"
    assert contract["rule_key"] is None
    contracts.validate_finding(contract)  # read-back satisfies contract #2


def test_regenerating_upserts_the_same_row_and_resets_to_draft(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    first = generate_report(ctx, session_id)
    with ctx.session_factory() as db:
        report = db.get(Report, first.report_id)
        assert report is not None
        report.status = ReportStatus.PUBLISHED
        db.commit()
    second = generate_report(ctx, session_id)
    assert second.report_id == first.report_id
    assert first.created and not second.created
    report = _get_report(ctx, first.report_id)
    assert report.status is ReportStatus.DRAFT
    with ctx.session_factory() as db:
        assert len(db.scalars(select(Report)).all()) == 1
        # The PUBLISHED->DRAFT demotion is audited, never silent (finding: a
        # regeneration un-publishing the day's record left no trace).
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "report_regenerated"))
        assert audit is not None
        assert audit.actor == "worker"
        assert audit.detail["old_status"] == ReportStatus.PUBLISHED.value
        assert audit.detail["reason"] == "regenerated by pipeline run"


def test_injected_writer_rewords_and_context_is_honest(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    writer = RecordingWriter()
    result = generate_report(ctx, session_id, writer=writer)
    assert result.writer == "injected"
    assert result.fallback_reason is None
    body = _get_report(ctx, result.report_id).body
    assert body["main_correction"]["text"].startswith("Coach says: ")
    assert body["positive"] == "Loved the energy out there."
    context = writer.contexts[0]
    assert context["history"] == {}
    assert context["rules"] == []
    assert context["tone"] == "encouraging"
    assert context["safety"] is None
    assert len(context["findings"]) == 1


def test_injected_writer_accepted_on_an_honesty_body(ctx: WorkerContext) -> None:
    """A well-behaved writer is honored even with no main correction: the guard
    skips the identity check when main_correction is None (US-G4)."""
    session_id = _seed_session(ctx)  # no findings -> honesty body, main is None
    result = generate_report(ctx, session_id, writer=RecordingWriter())
    assert result.writer == "injected"
    assert result.fallback_reason is None
    assert result.honest
    body = _get_report(ctx, result.report_id).body
    assert body["main_correction"] is None
    assert body["honesty_banner"] == HONESTY_CLEAN
    assert body["positive"] == "Loved the energy out there."  # writer reworded the positive


def test_exploding_writer_falls_back_and_audits_why(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    result = generate_report(ctx, session_id, writer=ExplodingWriter())
    assert result.writer == "rule_based"
    assert result.fallback_reason == "RuntimeError: LLM provider down"
    body = _get_report(ctx, result.report_id).body
    assert body["positive"] == DEFAULT_POSITIVE  # rule-based wording shipped
    with ctx.session_factory() as db:
        audit = db.scalars(select(AuditLog).where(AuditLog.action == "writer_fallback")).one()
        assert audit.actor == AUDIT_ACTOR
        assert audit.entity_id == str(result.report_id)
        assert audit.detail == {"reason": "RuntimeError: LLM provider down"}


def _drop_claims(body: dict[str, Any]) -> dict[str, Any]:
    body["claims"] = []
    return body


def _drop_key(body: dict[str, Any]) -> dict[str, Any]:
    del body["goal"]
    return body


def _swap_main_identity(body: dict[str, Any]) -> dict[str, Any]:
    body["main_correction"]["finding_id"] = str(uuid.uuid4())
    return body


def _remove_main(body: dict[str, Any]) -> dict[str, Any]:
    body["main_correction"] = None
    return body


def _reword_safety(body: dict[str, Any]) -> dict[str, Any]:
    body["safety"] = {"active": False, "codes": [], "text": "all good, bowl on", "sha256": "0"}
    return body


def _not_a_dict(body: dict[str, Any]) -> Any:
    del body
    return "just some prose"


@pytest.mark.safety
@pytest.mark.parametrize(
    ("mutate", "reason_fragment"),
    [
        (_drop_claims, "claims"),
        (_drop_key, "keys"),
        (_swap_main_identity, "identity"),
        (_remove_main, "main_correction"),
        (_reword_safety, "safety"),
        (_not_a_dict, "non-dict"),
    ],
)
def test_tampering_writers_are_rejected_and_fall_back(
    ctx: WorkerContext, mutate: Any, reason_fragment: str
) -> None:
    """SAF: a writer that changes numbers, keys or safety NEVER ships (US-G4)."""
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    safety = _safety()
    result = generate_report(ctx, session_id, writer=TamperingWriter(mutate), safety=safety)
    assert result.writer == "rule_based"
    assert result.fallback_reason is not None
    assert reason_fragment in result.fallback_reason
    body = _get_report(ctx, result.report_id).body
    assert body["safety"] == safety  # verbatim verdict shipped regardless


@pytest.mark.safety
def test_safety_verdict_is_inserted_verbatim_post_writer_and_hashed(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    safety = _safety("WORKLOAD_CEILING: 16 overs reached. No more bowling this week.")
    result = generate_report(ctx, session_id, writer=RecordingWriter(), safety=safety)
    report = _get_report(ctx, result.report_id)
    assert report.body["safety"] == safety
    assert report.safety_sha256 == hashlib.sha256(safety["text"].encode("utf-8")).hexdigest()
    assert report.safety_sha256 == safety["sha256"]


def test_quality_dict_is_persisted_and_thin_data_goes_honest(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    quality = {"components": {"sync": 0.2}, "composite": 0.3, "banner": "Data was thin today."}
    result = generate_report(ctx, session_id, quality=quality)
    assert result.honest
    report = _get_report(ctx, result.report_id)
    assert report.quality == quality
    assert report.body["honesty_banner"] == "Data was thin today."
    assert report.body["main_correction"] is None


# --- trends + drill threading (US-G3 ranking/drill, finding [4/48]) -----------


def test_trends_thread_into_the_ranking(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id, severity="minor", n=20, metric="m1")  # score 2*20 = 40
    b = _seed_finding(ctx, session_id, severity="major", n=10, metric="m2")  # score 4*10 = 40
    result = generate_report(ctx, session_id, trends={"m2": "regressing"})
    body = _get_report(ctx, result.report_id).body
    assert body["main_correction"]["finding_id"] == str(b)  # regressing m2 (x1.5) headlines


def test_drill_for_threads_into_the_drill_section(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)

    def drill_for(_finding: Any) -> dict[str, Any]:
        return {"drill_id": "lib-7", "text": "Cover-drive block.", "ball_count": 60}

    result = generate_report(ctx, session_id, drill_for=drill_for)
    body = _get_report(ctx, result.report_id).body
    assert body["drill"]["drill_id"] == "lib-7"
    assert "Do 60 balls." in body["drill"]["text"]


# --- whole-day merge + upsert race retry (finding [25/6]) --------------------


def _seed_two_sessions_one_day(ctx: WorkerContext) -> tuple[uuid.UUID, uuid.UUID]:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        morning = Session(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        afternoon = Session(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([morning, afternoon])
        db.commit()
        return morning.id, afternoon.id


def test_daily_report_merges_every_session_that_day(ctx: WorkerContext) -> None:
    morning, afternoon = _seed_two_sessions_one_day(ctx)
    head = _seed_finding(ctx, morning, severity="major", metric="m_a")
    tail = _seed_finding(ctx, afternoon, severity="minor", metric="m_b")
    result = generate_report(ctx, morning)  # triggered by the morning session
    report = _get_report(ctx, result.report_id)
    assert report.session_id == morning  # session_id column = the triggering session
    body = report.body
    assert body["main_correction"]["finding_id"] == str(head)  # major from the morning
    assert [item["finding_id"] for item in body["secondary"]] == [str(tail)]  # afternoon merged
    with ctx.session_factory() as db:
        assert len(db.scalars(select(Report)).all()) == 1  # one report for the whole day


def test_upsert_retries_once_on_the_integrity_race(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    real = gr_module._upsert_report
    calls = {"n": 0}

    def flaky(*args: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT", {}, Exception("uq_report_player_kind_period"))
        return real(*args)

    monkeypatch.setattr(gr_module, "_upsert_report", flaky)
    result = generate_report(ctx, session_id)
    assert calls["n"] == 2  # first attempt raced, retried once
    assert _get_report(ctx, result.report_id).body["main_correction"] is not None


def test_upsert_reraises_when_the_race_persists(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)

    def always_fail(*args: Any) -> Any:
        raise IntegrityError("INSERT", {}, Exception("uq_report_player_kind_period"))

    monkeypatch.setattr(gr_module, "_upsert_report", always_fail)
    with pytest.raises(IntegrityError):
        generate_report(ctx, session_id)


# --- LLM exchange auditing (US-G4, finding [15/41]) --------------------------


def _canned_writer(template: dict[str, Any], exchanges: list[dict[str, Any]]) -> LLMReportWriter:
    return LLMReportWriter(
        FakeLLMProvider(canned={"report_wording": json.dumps(template)}),
        on_exchange=exchanges.append,
    )


def test_accepted_llm_exchange_persists_a_linked_audit_row(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    first = generate_report(ctx, session_id)  # rule-based: gives us the exact body to echo
    template = {k: v for k, v in _get_report(ctx, first.report_id).body.items() if k != "safety"}
    exchanges: list[dict[str, Any]] = []
    result = generate_report(
        ctx, session_id, writer=_canned_writer(template, exchanges), llm_exchanges=exchanges
    )
    assert result.writer == "injected"
    with ctx.session_factory() as db:
        rows = db.scalars(
            select(ReportLLMAudit).where(ReportLLMAudit.report_id == result.report_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].accepted is True
        assert rows[0].prompt_key == "report_wording"


def test_rejected_llm_exchange_persists_a_linked_audit_row(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    exchanges: list[dict[str, Any]] = []
    writer = LLMReportWriter(
        FakeLLMProvider(canned={"report_wording": "not valid json"}),
        on_exchange=exchanges.append,
    )
    result = generate_report(ctx, session_id, writer=writer, llm_exchanges=exchanges)
    assert result.writer == "rule_based"  # rejection fell back
    with ctx.session_factory() as db:
        rows = db.scalars(
            select(ReportLLMAudit).where(ReportLLMAudit.report_id == result.report_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].accepted is False
        assert "not valid JSON" in str(rows[0].reason)


def test_rule_based_default_writes_no_llm_audit_rows(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    _seed_finding(ctx, session_id)
    result = generate_report(ctx, session_id)
    del result
    with ctx.session_factory() as db:
        assert db.scalars(select(ReportLLMAudit)).all() == []


# --- US-H3 fatigue note computed from the session balls (finding [53]) -------


def _add_ball_series(
    db: Any,
    session_id: uuid.UUID,
    block_id: uuid.UUID,
    start: int,
    count: int,
    *,
    control_rate: float,
    head: float,
    front: float,
    drop_front_on_last: bool = False,
) -> None:
    controlled = round(count * control_rate)
    for i in range(count):
        ball_no = start + i
        db.add(
            BallTag(
                session_id=session_id,
                ball_no=ball_no,
                block_id=block_id,
                line=Line.OFF,
                length=Length.GOOD,
                shot=Shot.DEFEND,
                footwork=Footwork.FRONT,
                contact=Contact.MIDDLE,
                outcome=Outcome.CONTROLLED_GROUND_SHOT,
                control=i < controlled,
                created_by="coach",
            )
        )
        metrics: dict[str, Any] = {
            "head_stability_score": {"value": head, "confidence": 1.0, "source": "auto"}
        }
        if not (drop_front_on_last and i == count - 1):
            metrics["front_foot_direction_cm"] = {
                "value": front,
                "confidence": 1.0,
                "source": "auto",
            }
        db.add(
            BallMetrics(
                session_id=session_id,
                ball_no=ball_no,
                phase=MetricPhase.PRE_RELEASE,
                metrics=metrics,
            )
        )


def _seed_block(
    db: Any, session_id: uuid.UUID, block_no: int, intent: BlockIntent = BlockIntent.TECHNICAL
) -> uuid.UUID:
    block = SessionBlock(
        session_id=session_id,
        block_no=block_no,
        start_s=0.0,
        end_s=None,
        bowler_source=BowlerSource.MACHINE,
        intent=intent,
    )
    db.add(block)
    db.flush()
    return block.id


def test_fatigue_note_computed_from_the_session_balls(ctx: WorkerContext) -> None:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        block = _seed_block(db, session.id, 1)
        _add_ball_series(db, session.id, block, 1, 10, control_rate=0.9, head=0.8, front=20.0)
        _add_ball_series(
            db,
            session.id,
            block,
            11,
            10,
            control_rate=0.6,
            head=0.65,
            front=14.0,
            drop_front_on_last=True,  # one window ball lacks front_foot: absent-metric path
        )
        db.commit()
        session_id = session.id
    result = generate_report(ctx, session_id, fatigue_config=FATIGUE_CFG)
    note = _get_report(ctx, result.report_id).body["fatigue_note"]
    assert note is not None
    assert note["control_drop_points"] == 30.0
    assert set(note["degrading_metrics"]) == {"head_stability_score", "front_foot_direction_cm"}


def test_block_context_change_suppresses_the_fatigue_note(ctx: WorkerContext) -> None:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        block_a = _seed_block(db, session.id, 1)
        block_b = _seed_block(db, session.id, 2)
        _add_ball_series(db, session.id, block_a, 1, 10, control_rate=0.9, head=0.8, front=20.0)
        _add_ball_series(db, session.id, block_b, 11, 10, control_rate=0.6, head=0.65, front=14.0)
        db.commit()
        session_id = session.id
    result = generate_report(ctx, session_id, fatigue_config=FATIGUE_CFG)
    # the window is all block B; too few same-context baseline balls -> not evaluated
    assert _get_report(ctx, result.report_id).body["fatigue_note"] is None


# ------------------------------------- US-J5 review gate seam (findings [14/30/38])


def _enable_coach_gate(ctx: WorkerContext, *, timeout_hours: int = 24) -> None:
    with ctx.session_factory() as db:
        db.add(
            AppSetting(
                version=2,
                settings={
                    "report_review": {"mode": "coach_gate", "timeout_hours": timeout_hours},
                    "live_mode": {"enabled": False, "allowlist": []},
                },
                approved_by="coach",
                reason="test",
            )
        )
        db.commit()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


T0 = datetime(2026, 7, 9, 18, 0, tzinfo=UTC)


def test_coach_gate_draft_carries_the_review_deadline(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-J5 finding [30]: the drafting seam is value-asserted, not just run —
    a fresh coach_gate draft MUST carry now + timeout_hours, tz-aware."""
    session_id = _seed_session(ctx)
    _enable_coach_gate(ctx)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: T0)
    result = generate_report(ctx, session_id)
    report = _get_report(ctx, result.report_id)
    assert report.review_due_at is not None
    assert _utc(report.review_due_at) == T0 + timedelta(hours=24)


def test_auto_publish_draft_carries_no_deadline(ctx: WorkerContext) -> None:
    session_id = _seed_session(ctx)
    result = generate_report(ctx, session_id)  # unseeded settings: auto_publish
    assert _get_report(ctx, result.report_id).review_due_at is None


def test_regenerating_a_held_draft_keeps_its_deadline(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-J5 finding [14/38]: pipeline re-runs must not creep the coach's
    timeout — a held draft keeps its original review window."""
    session_id = _seed_session(ctx)
    _enable_coach_gate(ctx)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: T0)
    first = generate_report(ctx, session_id)

    # One hour before the deadline a routine re-run regenerates the report.
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: T0 + timedelta(hours=23))
    second = generate_report(ctx, session_id)

    assert second.report_id == first.report_id
    report = _get_report(ctx, second.report_id)
    assert report.review_due_at is not None
    assert _utc(report.review_due_at) == T0 + timedelta(hours=24)  # unchanged


def test_demoted_published_report_gets_a_fresh_review_window(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regenerated body after a publish needs fresh coach review: the
    demotion is audited and the deadline restarts."""
    session_id = _seed_session(ctx)
    _enable_coach_gate(ctx)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: T0)
    first = generate_report(ctx, session_id)
    with ctx.session_factory() as db:
        report = db.get(Report, first.report_id)
        assert report is not None
        report.status = ReportStatus.PUBLISHED
        db.commit()

    later = T0 + timedelta(hours=5)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: later)
    generate_report(ctx, session_id)

    report = _get_report(ctx, first.report_id)
    assert report.status is ReportStatus.DRAFT
    assert report.review_due_at is not None
    assert _utc(report.review_due_at) == later + timedelta(hours=24)


def test_draft_without_deadline_gains_one_when_the_gate_turns_on(
    ctx: WorkerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auto_publish-era draft regenerated under coach_gate becomes held."""
    session_id = _seed_session(ctx)
    generate_report(ctx, session_id)  # auto_publish: no deadline
    _enable_coach_gate(ctx)
    monkeypatch.setattr("cricai_coaching.review_gate.utcnow", lambda: T0)
    result = generate_report(ctx, session_id)
    report = _get_report(ctx, result.report_id)
    assert report.review_due_at is not None
    assert _utc(report.review_due_at) == T0 + timedelta(hours=24)


# ------------------- US-H2 plan-vs-actual batting split (findings [12/19/31])
#
# The US-H2 AC "Report shows plan-vs-actual per block; >25% deviation flagged"
# gets a production surface: a batting/MIXED daily report whose day carries
# tagged blocks ships the additive structured ``batting_split`` block computed
# by ``cricai_coaching.workload.reconcile_batting_split`` against the governing
# safety config's batting split. Numbers below mirror the T5 #2 golden's
# hand-computed table (test_golden_mixed_session._assert_h2_reconciliation).


def _seed_split_config(ctx: WorkerContext) -> None:
    """A governing safety config whose season split makes the numbers exact:
    technical (3-4)/4 = -25% sits AT the threshold (must NOT flag), decision
    (3-2)/2 = +50% flags, spin_specific is on plan, the empty fun block is
    -100% and flags."""
    config = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    config["batting_split"]["blocks"] = {
        "technical": 4,
        "decision": 2,
        "spin_specific": 3,
        "fun": 1,
    }
    with ctx.session_factory() as db:
        db.add(
            SafetyConfig(version=2, config=config, approved_by="coach", reason="test season plan")
        )
        db.commit()


def _seed_mixed_day(ctx: WorkerContext) -> uuid.UUID:
    """A MIXED session with four intent blocks: 3/3/3 tagged balls and an
    empty fun block (declared, never played)."""
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=SESSION_DATE,
            session_type=SessionType.MIXED,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.flush()
        technical = _seed_block(db, session.id, 1, BlockIntent.TECHNICAL)
        decision = _seed_block(db, session.id, 2, BlockIntent.DECISION)
        spin = _seed_block(db, session.id, 3, BlockIntent.SPIN_SPECIFIC)
        _seed_block(db, session.id, 4, BlockIntent.FUN)  # declared, zero balls
        _add_ball_series(db, session.id, technical, 1, 3, control_rate=1.0, head=0.8, front=20.0)
        _add_ball_series(db, session.id, decision, 4, 3, control_rate=1.0, head=0.8, front=20.0)
        _add_ball_series(db, session.id, spin, 7, 3, control_rate=1.0, head=0.8, front=20.0)
        db.commit()
        return session.id


def test_mixed_day_report_ships_the_plan_vs_actual_split(ctx: WorkerContext) -> None:
    """US-H2 AC: plan-vs-actual per block, >25% deviation flagged, fun-block
    predicate — hand-computed against the governing config split."""
    session_id = _seed_mixed_day(ctx)
    _seed_split_config(ctx)
    result = generate_report(ctx, session_id)
    body = _get_report(ctx, result.report_id).body
    contracts.validate_report_body(body)  # additive key passes contract #4
    split = body["batting_split"]
    assert split["intents"] == [
        {
            "intent": "technical",
            "planned_balls": 4,
            "actual_balls": 3,
            "deviation_pct": -25.0,
            "flagged": False,  # exactly AT the 25% threshold: not over it
        },
        {
            "intent": "decision",
            "planned_balls": 2,
            "actual_balls": 3,
            "deviation_pct": 50.0,
            "flagged": True,
        },
        {
            "intent": "spin_specific",
            "planned_balls": 3,
            "actual_balls": 3,
            "deviation_pct": 0.0,
            "flagged": False,
        },
        {
            "intent": "fun",
            "planned_balls": 1,
            "actual_balls": 0,
            "deviation_pct": -100.0,
            "flagged": True,
        },
    ]
    assert split["planned_total"] == 10
    assert split["actual_total"] == 9
    assert split["flagged_intents"] == ["decision", "fun"]
    assert split["fun_block_intact"] is False  # the fun block never got a ball
    # The producer note is fixed and digit-free (claim-exempt structured data,
    # like the fatigue note's components — the k4 precedent).
    assert split["note"]
    assert not any(ch.isdigit() for ch in split["note"])


def test_split_reconciles_against_the_default_config_when_none_is_stored(
    ctx: WorkerContext,
) -> None:
    """No safety_configs row: the seed DEFAULT_SAFETY_CONFIG split governs."""
    session_id = _seed_mixed_day(ctx)
    result = generate_report(ctx, session_id)
    split = _get_report(ctx, result.report_id).body["batting_split"]
    assert split["planned_total"] == 500  # v1 default daily split total
    assert split["actual_total"] == 9


def test_split_merges_blocks_from_every_batting_session_that_day(ctx: WorkerContext) -> None:
    """A daily report is the WHOLE day: a sibling same-day batting session's
    blocks reconcile together with the triggering session's."""
    session_id = _seed_mixed_day(ctx)
    _seed_split_config(ctx)
    with ctx.session_factory() as db:
        session = db.get(Session, session_id)
        assert session is not None
        sibling = Session(
            player_id=session.player_id,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(sibling)
        db.flush()
        fun = _seed_block(db, sibling.id, 1, BlockIntent.FUN)
        _add_ball_series(db, sibling.id, fun, 1, 1, control_rate=1.0, head=0.8, front=20.0)
        db.commit()
    result = generate_report(ctx, session_id)
    split = _get_report(ctx, result.report_id).body["batting_split"]
    fun_row = next(row for row in split["intents"] if row["intent"] == "fun")
    assert fun_row == {
        "intent": "fun",
        "planned_balls": 1,
        "actual_balls": 1,
        "deviation_pct": 0.0,
        "flagged": False,
    }
    assert split["fun_block_intact"] is True  # the sibling's fun block played
    assert split["actual_total"] == 10


def test_report_without_day_blocks_carries_no_batting_split(ctx: WorkerContext) -> None:
    """No tagged blocks anywhere in the day: the key stays absent (additive,
    never a fabricated zero-plan reconciliation)."""
    session_id = _seed_session(ctx)
    result = generate_report(ctx, session_id)
    assert "batting_split" not in _get_report(ctx, result.report_id).body


@pytest.mark.safety
def test_writer_cannot_tamper_the_batting_split(ctx: WorkerContext) -> None:
    """SAF: the split is structural data — a writer that edits it never ships."""
    session_id = _seed_mixed_day(ctx)

    def _tamper(body: dict[str, Any]) -> dict[str, Any]:
        body["batting_split"]["intents"][0]["actual_balls"] = 400
        return body

    result = generate_report(ctx, session_id, writer=TamperingWriter(_tamper))
    assert result.writer == "rule_based"
    assert result.fallback_reason is not None
    assert "batting_split" in result.fallback_reason
    split = _get_report(ctx, result.report_id).body["batting_split"]
    assert split["intents"][0]["actual_balls"] == 3  # the honest number shipped
