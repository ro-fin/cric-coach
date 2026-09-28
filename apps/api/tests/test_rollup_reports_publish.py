"""US-G5 x US-G3/H5: rollup bodies pass the EXISTING publish gate end-to-end.

The weekly/monthly bodies ship their numbers in the structured ``trends`` /
``milestones`` keys AND mirror each one in ``claims`` with a recompute key, so
the publish gate re-derives every rollup number from ``metric_baselines`` /
``milestones`` at publish (finding [24]) — wording stays number-free so the
coverage validator passes with the structured numbers covered. This suite runs
the real rollup job against the real API engine and pushes the result through
``POST /reports/{id}/publish`` — the binding proof for contract #3 — and proves
a rollup number mutated in the stored body after generation is BLOCKED.
"""

import copy
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import make_session_factory
from cricai_data.enums import ReportKind, ReportStatus
from cricai_data.models import MetricBaseline, Milestone, Report
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.rollup_reports import rollup_reports
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as DbSession

from cricai_testing.apptest import (
    COACH_TOKEN,
    PARENT_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)

AS_OF = date(2026, 6, 17)


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


@pytest.fixture
def ctx(engine: Engine, tmp_path: Path) -> WorkerContext:
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _create_player(client: TestClient) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20", "handedness": "right"},
        headers=auth(PARENT_TOKEN),
    ).json()
    player_id: str = player["id"]
    return player_id


def _seed_baselines(engine: Engine, player_id: str) -> None:
    """Three qualified points (>=3 sessions, >=30 balls each, US-G5 guards)."""
    with DbSession(engine) as db:
        for day, value in ((3, 0.5714), (10, 0.6123), (17, 0.7051)):
            db.add(
                MetricBaseline(
                    player_id=uuid.UUID(player_id),
                    metric="control_pct",
                    zone_key="all",
                    window="session",
                    snapshot_date=date(2026, 6, day),
                    value=value,
                    n=40,
                    payload={"session_id": None, "session_ids": []},
                )
            )
        db.commit()


def _weekly_report(client: TestClient, player_id: str) -> dict[str, Any]:
    reports = client.get(
        "/reports",
        params={"player_id": player_id, "kind": "weekly"},
        headers=auth(COACH_TOKEN),
    ).json()
    assert len(reports) == 1
    report: dict[str, Any] = reports[0]
    return report


def test_weekly_and_monthly_rollups_publish_through_the_existing_gate(
    client: TestClient, engine: Engine, ctx: WorkerContext
) -> None:
    player_id = _create_player(client)
    _seed_baselines(engine, player_id)
    summary = rollup_reports(ctx, uuid.UUID(player_id), as_of=AS_OF)

    for report_id in (summary.weekly_report_id, summary.monthly_report_id):
        # The body's structured numbers each ship a recompute claim (finding
        # [24]); the gate re-derives them all before publishing.
        claims = client.get(f"/reports/{report_id}/claims", headers=auth(COACH_TOKEN)).json()
        assert claims, "rollup body must carry recompute claims for its numbers"
        assert all(check["match"] for check in claims), claims

        response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
        assert response.status_code == 200, response.json()
        assert response.json() == {"status": "published", "reasons": []}


def _weekly_draft_id(ctx: WorkerContext, client: TestClient, engine: Engine) -> str:
    player_id = _create_player(client)
    _seed_baselines(engine, player_id)
    return rollup_reports(ctx, uuid.UUID(player_id), as_of=AS_OF).weekly_report_id


def _publish(client: TestClient, report_id: str) -> Any:
    return client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))


def test_tampered_milestone_value_is_blocked_at_publish(
    client: TestClient, engine: Engine, ctx: WorkerContext
) -> None:
    """A fabricated personal-best value in the stored body fails the gate.

    This is the finding-[24] threat: edit ``milestones[0].value`` (the number
    the k4 dashboard renders to the child) in the DRAFT after generation. The
    milestone claim still holds the true value, so the publish gate sees the
    rendered number diverge from its claim and lands the report in BLOCKED,
    naming the offending claim — the fabricated best never reaches the child.
    """
    report_id = _weekly_draft_id(ctx, client, engine)
    fabricated = 0.999
    with DbSession(engine) as db:
        report = db.get(Report, uuid.UUID(report_id))
        assert report is not None
        body: dict[str, Any] = copy.deepcopy(report.body)
        assert body["milestones"], "expected a personal-best milestone in the body"
        assert body["milestones"][0]["value"] != fabricated
        body["milestones"][0]["value"] = fabricated  # only the rendered number, not its claim
        report.body = body
        db.add(report)
        db.commit()

    response = _publish(client, report_id)
    # Reverting claims-emission makes this publish succeed (200) — the red proof
    # the recompute gate now covers rollup numbers.
    assert response.status_code == 409, response.json()
    payload = response.json()
    assert payload["status"] == "blocked"
    assert any(r.startswith("claim milestone:") for r in payload["reasons"]), payload["reasons"]
    # The blocked report never reaches the child.
    assert client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN)).status_code == 404


def test_tampered_trend_point_value_is_blocked_at_publish(
    client: TestClient, engine: Engine, ctx: WorkerContext
) -> None:
    """A fabricated trend-point value in the stored body fails the gate too.

    Edit ``trends[0].points[-1].value`` (a rendered number) after generation;
    the baseline-scheme claim for that snapshot date still holds the true value,
    so publish is BLOCKED naming that claim.
    """
    report_id = _weekly_draft_id(ctx, client, engine)
    fabricated = 123.456
    with DbSession(engine) as db:
        report = db.get(Report, uuid.UUID(report_id))
        assert report is not None
        body = copy.deepcopy(report.body)
        point = body["trends"][0]["points"][-1]
        assert point["value"] != fabricated
        point["value"] = fabricated  # only the rendered number, not its claim
        report.body = body
        db.add(report)
        db.commit()

    response = _publish(client, report_id)
    assert response.status_code == 409, response.json()
    payload = response.json()
    assert payload["status"] == "blocked"
    assert any(r.startswith("claim baseline:") for r in payload["reasons"]), payload["reasons"]


def test_publish_gate_rejects_malformed_and_dangling_structured_claims(
    client: TestClient, engine: Engine
) -> None:
    """Finding [24] defensive paths: a rollup body whose structured claims carry
    malformed recompute keys (wrong part count, unparseable date/kind), a
    boolean value, or reference a trend/milestone the body does not render must
    BLOCK — never 500 and never publish. The claims are hand-crafted (a normal
    rollup only ever emits well-formed keys), so this pins the guard branches
    the recompute path and the structured-claim check fall back through.
    """
    player_id = _create_player(client)
    body: dict[str, Any] = {
        "kind": "weekly",
        "period": {"start": "2026-06-15", "end": "2026-06-21"},
        "main_correction": None,
        "drill": None,
        "goal": None,
        "secondary": [],
        "positive": "Keep showing up.",
        "safety": None,
        "honesty_banner": None,
        "coverage_note": None,
        "fatigue_note": None,
        "trends": [
            {
                "metric": "control_pct",
                "zone_key": "all",
                "points": [{"date": "2026-06-17", "value": 0.7, "n": 40}],
                "direction": "up",
                "qualified": True,
            },
            # a second, non-matching trend so the rendered-value lookup iterates
            # past a slice that does not match before returning None.
            {
                "metric": "other",
                "zone_key": "off/good",
                "points": [],
                "direction": "flat",
                "qualified": False,
            },
        ],
        "milestones": [
            {
                "kind": "personal_best",
                "metric": "control_pct",
                "value": 0.7,
                "achieved_on": "2026-06-17",
            }
        ],
        "claims": [
            # baseline, right scheme but an unparseable date: recompute returns
            # None (parse-date guard), and the rendered lookup matches the slice
            # yet no point date, then a non-matching slice, then misses.
            {
                "value": 0.7,
                "metric": "control_pct",
                "recompute_key": "baseline:control_pct:all:session:not-a-date",
            },
            # baseline with too few parts: recompute None (length guard).
            {"value": 1.0, "metric": "control_pct", "recompute_key": "baseline:short"},
            # milestone with an unknown kind: recompute None (kind guard), and the
            # rendered lookup iterates past the real milestone before missing.
            {
                "value": 1.0,
                "metric": "control_pct",
                "recompute_key": "milestone:not_a_kind:control_pct:2026-06-17",
            },
            # milestone with too few parts: recompute None (length guard).
            {"value": 1.0, "metric": "control_pct", "recompute_key": "milestone:onlyone"},
            # a boolean value is not a number: the structured check skips it
            # (float() still accepts it for the recompute check, so no crash).
            {
                "value": True,
                "metric": "control_pct",
                "recompute_key": "baseline:control_pct:all:session:2026-06-17",
            },
        ],
    }
    with DbSession(engine) as db:
        report = Report(
            player_id=uuid.UUID(player_id),
            session_id=None,
            kind=ReportKind.WEEKLY,
            period_start=date(2026, 6, 15),
            period_end=date(2026, 6, 21),
            status=ReportStatus.DRAFT,
            body=body,
        )
        db.add(report)
        db.commit()
        report_id = str(report.id)

    response = _publish(client, report_id)
    assert response.status_code == 409, response.json()
    payload = response.json()
    assert payload["status"] == "blocked"
    reasons = payload["reasons"]
    # malformed/unknown keys are not recomputable; the dangling claims have no
    # matching rendered number — every one is named, nothing raises.
    assert any("not recomputable" in reason for reason in reasons), reasons
    assert any("no matching rendered number" in reason for reason in reasons), reasons
    # a blocked report never reaches the child.
    assert client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN)).status_code == 404


def test_published_weekly_report_is_player_readable(
    client: TestClient, engine: Engine, ctx: WorkerContext
) -> None:
    """The child sees the rollup only after it published (US-G3/H5 rule)."""
    player_id = _create_player(client)
    _seed_baselines(engine, player_id)
    summary = rollup_reports(ctx, uuid.UUID(player_id), as_of=AS_OF)

    draft = client.get(f"/reports/{summary.weekly_report_id}", headers=auth(PLAYER_TOKEN))
    assert draft.status_code == 404  # drafts never reach the child

    client.post(f"/reports/{summary.weekly_report_id}/publish", headers=auth(COACH_TOKEN))
    shown = client.get(f"/reports/{summary.weekly_report_id}", headers=auth(PLAYER_TOKEN))
    assert shown.status_code == 200
    assert shown.json()["body"]["kind"] == "weekly"


def test_trend_numbers_are_recomputable_from_the_baseline_rows(
    client: TestClient, engine: Engine, ctx: WorkerContext
) -> None:
    """US-K4 data-parity, server side: the body's numbers ARE the DB numbers."""
    player_id = _create_player(client)
    _seed_baselines(engine, player_id)
    rollup_reports(ctx, uuid.UUID(player_id), as_of=AS_OF)

    body = _weekly_report(client, player_id)["body"]
    (trend,) = body["trends"]
    with DbSession(engine) as db:
        rows = db.scalars(
            select(MetricBaseline)
            .where(MetricBaseline.player_id == uuid.UUID(player_id))
            .order_by(MetricBaseline.snapshot_date)
        ).all()
    assert trend["points"] == [
        {"date": row.snapshot_date.isoformat(), "value": row.value, "n": row.n} for row in rows
    ]


def test_body_milestones_match_the_milestone_rows(
    client: TestClient, engine: Engine, ctx: WorkerContext
) -> None:
    player_id = _create_player(client)
    _seed_baselines(engine, player_id)
    rollup_reports(ctx, uuid.UUID(player_id), as_of=AS_OF)

    body = _weekly_report(client, player_id)["body"]
    with DbSession(engine) as db:
        (row,) = db.scalars(
            select(Milestone).where(Milestone.player_id == uuid.UUID(player_id))
        ).all()
    assert body["milestones"] == [
        {
            "kind": row.kind.value,
            "metric": row.metric,
            "value": row.value,
            "achieved_on": row.achieved_on.isoformat(),
        }
    ]
    feed = client.get(f"/milestones/players/{player_id}", headers=auth(PLAYER_TOKEN)).json()
    assert [(m["metric"], m["value"], m["achieved_on"]) for m in feed] == [
        (row.metric, row.value, row.achieved_on.isoformat())
    ]
