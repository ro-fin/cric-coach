"""US-G3/G6 acceptance: report read surfaces, the publish gate (SAF: tampered
safety text is rejected 100% of the time), claim recomputation and coach
evidence verdicts with rule analytics."""

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cricai_api.deps import get_db
from cricai_api.routers.reports import get_recomputer
from cricai_data.db import create_all
from cricai_data.enums import BlockIntent, ClipStatus, ReportKind
from cricai_data.models import AuditLog, Clip, Drill, Finding, Player, Report
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app

PERIOD = date(2026, 7, 9)
UNKNOWN = "00000000-0000-0000-0000-000000000000"


@contextmanager
def _db(client: TestClient) -> Iterator[OrmSession]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db: OrmSession = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _create_player(client: TestClient) -> str:
    response = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    )
    player_id: str = response.json()["id"]
    return player_id


def _create_session(client: TestClient, player_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "player_id": player_id,
            "date": PERIOD.isoformat(),
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    )
    session_id: str = response.json()["id"]
    return session_id


def _seed_clips(
    client: TestClient,
    session_id: str,
    count: int = 2,
    status: ClipStatus = ClipStatus.CUT,
    camera_id: str = "C1",
) -> list[str]:
    """Seed ``count`` clips; ``camera_id`` keeps repeated calls on one session
    from colliding on the (session, ball_no, camera) uniqueness constraint."""
    with _db(client) as db:
        ids: list[str] = []
        for i in range(count):
            clip = Clip(
                session_id=uuid.UUID(session_id),
                ball_no=i + 1,
                camera_id=camera_id,
                start_ms=0,
                end_ms=1000,
                status=status,
            )
            db.add(clip)
            db.flush()
            ids.append(str(clip.id))
        return ids


def _seed_finding(
    client: TestClient,
    session_id: str,
    *,
    rule_key: str | None = "front_foot_stride",
    kind: str = "technique",
    n: int = 12,
    effect_size: float | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    with _db(client) as db:
        finding = Finding(
            session_id=uuid.UUID(session_id),
            agent="rules",
            rule_key=rule_key,
            kind=kind,
            severity="major",
            metric="control_pct",
            condition={"line": "outside_off"},
            n=n,
            effect_size=effect_size,
            confidence=0.9,
            ball_ids=list(range(1, n + 1)),
            evidence={},
            payload=payload if payload is not None else {},
        )
        db.add(finding)
        db.flush()
        return str(finding.id)


def _body(
    finding_id: str,
    clip_ids: list[str],
    *,
    n: int = 12,
    safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = {str(i + 1): {"C1": clip_id} for i, clip_id in enumerate(clip_ids)}
    return {
        "kind": "daily",
        "period": {"start": PERIOD.isoformat(), "end": PERIOD.isoformat()},
        "main_correction": {
            "finding_id": finding_id,
            "text": f"Move your front foot to the ball. Seen on {n} balls.",
            "evidence": evidence,
        },
        "drill": {
            "drill_id": None,
            "text": "Front-foot ladder drill.",
            "machine_settings": {},
            "success_metric": "control_pct",
        },
        "goal": {"metric": "control_pct", "target": None, "condition": {}},
        "secondary": [],
        "positive": "Great effort today.",
        "safety": safety,
        "honesty_banner": None,
        "claims": [
            {"value": n, "metric": "ball_count", "recompute_key": f"finding:{finding_id}:n"}
        ],
    }


def _seed_report(
    client: TestClient,
    player_id: str,
    session_id: str,
    body: dict[str, Any],
    *,
    kind: ReportKind = ReportKind.DAILY,
    period: date = PERIOD,
    safety_sha256: str | None = None,
) -> str:
    with _db(client) as db:
        report = Report(
            player_id=uuid.UUID(player_id),
            session_id=uuid.UUID(session_id),
            kind=kind,
            period_start=period,
            period_end=period,
            body=body,
            safety_sha256=safety_sha256,
        )
        db.add(report)
        db.flush()
        return str(report.id)


def _publishable_report(client: TestClient, safety: dict[str, Any] | None = None) -> str:
    """A fully valid draft: real finding, 2 CUT clips, consistent claims."""
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id)
    body = _body(finding_id, clip_ids, safety=safety)
    sha = _sha(str(safety["text"])) if safety is not None else None
    return _seed_report(client, player_id, session_id, body, safety_sha256=sha)


def _safety(text: str = "WORKLOAD_CEILING reached. Stop bowling.") -> dict[str, Any]:
    return {"active": True, "codes": ["workload_ceiling"], "text": text, "sha256": _sha(text)}


# --- read surfaces -----------------------------------------------------------


def test_get_report_roundtrips_the_body_for_reviewer_roles(client: TestClient) -> None:
    report_id = _publishable_report(client)
    for token in (PARENT_TOKEN, COACH_TOKEN):
        response = client.get(f"/reports/{report_id}", headers=auth(token))
        assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "draft"
    assert payload["kind"] == "daily"
    assert "Move your front foot" in payload["body"]["main_correction"]["text"]


def test_get_report_404_and_401(client: TestClient) -> None:
    assert client.get(f"/reports/{UNKNOWN}", headers=auth(PARENT_TOKEN)).status_code == 404
    assert client.get(f"/reports/{UNKNOWN}").status_code == 401


def test_list_reports_filters_by_player_and_kind(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id)
    daily = _seed_report(client, player_id, session_id, _body(finding_id, clip_ids))
    weekly = _seed_report(
        client,
        player_id,
        session_id,
        _body(finding_id, clip_ids),
        kind=ReportKind.WEEKLY,
        period=date(2026, 7, 5),
    )
    both = client.get(f"/reports?player_id={player_id}", headers=auth(PARENT_TOKEN)).json()
    assert [r["id"] for r in both] == [daily, weekly]  # newest period first
    weekly_only = client.get(
        f"/reports?player_id={player_id}&kind=weekly", headers=auth(COACH_TOKEN)
    ).json()
    assert [r["id"] for r in weekly_only] == [weekly]
    assert client.get(f"/reports?player_id={UNKNOWN}", headers=auth(PARENT_TOKEN)).json() == []


def test_report_html_render_flags_unpublished_status_for_reviewers(client: TestClient) -> None:
    report_id = _publishable_report(client)
    response = client.get(f"/reports/{report_id}/html", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Move your front foot to the ball." in response.text
    assert "STATUS: DRAFT" in response.text  # status is prominent on reviewer renders


# --- claim recomputation (US-G3 IT surface) -----------------------------------


def test_claims_recompute_from_the_database(client: TestClient) -> None:
    report_id = _publishable_report(client)
    checks = client.get(f"/reports/{report_id}/claims", headers=auth(COACH_TOKEN)).json()
    assert len(checks) == 1
    assert checks[0]["match"] is True
    assert checks[0]["claimed"] == 12.0
    assert checks[0]["recomputed"] == 12.0


def test_claim_recompute_key_vocabulary(client: TestClient) -> None:
    """Every default-recomputer branch: finding fields, drills, junk keys."""
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(
        client,
        session_id,
        effect_size=0.4,
        payload={"threshold": 60, "note": "not numeric"},
    )
    with _db(client) as db:
        drill = Drill(
            name="Cover drive block",
            setup="Machine, full outside off",
            ball_count=80,
            target_metric="control_pct",
            intent=BlockIntent.TECHNICAL,
            author="coach",
        )
        db.add(drill)
        db.flush()
        drill_id = str(drill.id)
    body = _body(finding_id, clip_ids)
    body["claims"] = [
        {"value": 12, "metric": "ball_count", "recompute_key": f"finding:{finding_id}:n"},
        {"value": 0.4, "metric": "effect", "recompute_key": f"finding:{finding_id}:effect_size"},
        {"value": 60, "metric": "control_pct", "recompute_key": f"finding:{finding_id}:threshold"},
        {"value": 1, "metric": "junk", "recompute_key": f"finding:{finding_id}:note"},
        {"value": 1, "metric": "junk", "recompute_key": "finding:not-a-uuid:n"},
        {"value": 1, "metric": "junk", "recompute_key": f"finding:{UNKNOWN}:n"},
        {"value": 80, "metric": "drill_balls", "recompute_key": f"drill:{drill_id}:ball_count"},
        {"value": 80, "metric": "junk", "recompute_key": f"drill:{UNKNOWN}:ball_count"},
        {"value": 80, "metric": "junk", "recompute_key": f"drill:{drill_id}:name"},
        {"value": 1, "metric": "junk", "recompute_key": "goal:target"},
        {"value": 1, "metric": "junk", "recompute_key": "martian:x:y"},
    ]
    report_id = _seed_report(client, player_id, session_id, body)
    checks = client.get(f"/reports/{report_id}/claims", headers=auth(PARENT_TOKEN)).json()
    by_key = {c["recompute_key"]: c for c in checks}
    assert by_key[f"finding:{finding_id}:n"]["match"] is True
    assert by_key[f"finding:{finding_id}:effect_size"]["match"] is True
    assert by_key[f"finding:{finding_id}:threshold"]["match"] is True
    for junk in (
        f"finding:{finding_id}:note",
        "finding:not-a-uuid:n",
        f"finding:{UNKNOWN}:n",
        f"drill:{UNKNOWN}:ball_count",
        f"drill:{drill_id}:name",
        "goal:target",
        "martian:x:y",
    ):
        assert by_key[junk]["match"] is False
        assert by_key[junk]["recomputed"] is None
    assert by_key[f"drill:{drill_id}:ball_count"]["match"] is True


def test_missing_effect_size_is_not_recomputable(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id, effect_size=None)
    body = _body(finding_id, clip_ids)
    body["claims"].append(
        {"value": 0.4, "metric": "effect", "recompute_key": f"finding:{finding_id}:effect_size"}
    )
    report_id = _seed_report(client, player_id, session_id, body)
    checks = client.get(f"/reports/{report_id}/claims", headers=auth(COACH_TOKEN)).json()
    assert checks[-1]["match"] is False


def test_recomputer_is_injectable(client: TestClient) -> None:
    """Dependency override proves the recompute callback seam (publish + IT)."""
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id)
    body = _body(finding_id, clip_ids)
    body["claims"].append({"value": 7, "metric": "magic", "recompute_key": "magic:7:x"})
    report_id = _seed_report(client, player_id, session_id, body)

    def trusting(_db: OrmSession, _report: Report, claim: dict[str, Any]) -> float:
        return float(claim["value"])

    client.app.dependency_overrides[get_recomputer] = lambda: trusting  # type: ignore[union-attr]
    try:
        checks = client.get(f"/reports/{report_id}/claims", headers=auth(COACH_TOKEN)).json()
        assert all(c["match"] for c in checks)
        publish = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
        assert publish.status_code == 200
    finally:
        client.app.dependency_overrides.clear()  # type: ignore[union-attr]


# --- publish gate --------------------------------------------------------------


def test_publish_happy_path_with_safety(client: TestClient) -> None:
    safety = _safety()
    report_id = _publishable_report(client, safety=safety)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"status": "published", "reasons": []}
    shown = client.get(f"/reports/{report_id}", headers=auth(PARENT_TOKEN)).json()
    assert shown["status"] == "published"
    with _db(client) as db:
        audit = db.scalars(select(AuditLog).where(AuditLog.action == "report_published")).one()
        assert audit.entity_id == report_id


def test_publish_honesty_report_without_safety(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    body = {
        "kind": "daily",
        "period": {"start": PERIOD.isoformat(), "end": PERIOD.isoformat()},
        "main_correction": None,
        "drill": None,
        "goal": None,
        "secondary": [],
        "positive": "Great effort today.",
        "safety": None,
        "honesty_banner": "Clean session - keep the same plan.",
        "claims": [],
    }
    report_id = _seed_report(client, player_id, session_id, body)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 200


def test_publish_404_and_roles(client: TestClient) -> None:
    assert client.post(f"/reports/{UNKNOWN}/publish", headers=auth(PARENT_TOKEN)).status_code == 404
    report_id = _publishable_report(client)
    assert (
        client.post(f"/reports/{report_id}/publish", headers=auth(PLAYER_TOKEN)).status_code == 403
    )


def _tamper_text(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    body["safety"]["text"] = "Feeling great - bowl as much as you like!"
    return body, _sha("WORKLOAD_CEILING reached. Stop bowling.")


def _tamper_text_with_matching_inner_sha(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    body["safety"]["text"] = "Limits lifted, coach said so."
    body["safety"]["sha256"] = _sha("Limits lifted, coach said so.")
    return body, _sha("WORKLOAD_CEILING reached. Stop bowling.")


def _tamper_inner_sha(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    body["safety"]["sha256"] = "0" * 64
    return body, _sha("WORKLOAD_CEILING reached. Stop bowling.")


def _tamper_column_hash(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    return body, "f" * 64


def _tamper_missing_column_hash(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    return body, None


def _tamper_remove_safety(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    body["safety"] = None
    return body, _sha("WORKLOAD_CEILING reached. Stop bowling.")


@pytest.mark.safety
@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_text,
        _tamper_text_with_matching_inner_sha,
        _tamper_inner_sha,
        _tamper_column_hash,
        _tamper_missing_column_hash,
        _tamper_remove_safety,
    ],
)
def test_publish_rejects_every_safety_tampering_attempt(client: TestClient, tamper: Any) -> None:
    """SAF (US-H5): tampered safety text is blocked 100% of attempts."""
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id)
    body, column_sha = tamper(_body(finding_id, clip_ids, safety=_safety()))
    report_id = _seed_report(client, player_id, session_id, body, safety_sha256=column_sha)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    payload = response.json()
    assert payload["status"] == "blocked"
    assert any("safety" in reason for reason in payload["reasons"])
    shown = client.get(f"/reports/{report_id}", headers=auth(COACH_TOKEN)).json()
    assert shown["status"] == "blocked"  # the block is persisted, not advisory
    with _db(client) as db:
        audit = db.scalars(
            select(AuditLog).where(AuditLog.action == "report_publish_blocked")
        ).one()
        assert audit.entity_id == report_id
        assert audit.detail is not None and audit.detail["reasons"] == payload["reasons"]


@pytest.mark.safety
def test_publish_rejects_unrecomputable_and_uncovered_numbers(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id, n=12)
    body = _body(finding_id, clip_ids)
    body["claims"][0]["value"] = 99  # lies about the ball count
    body["positive"] = "You hit 9 sixes!"  # 9 appears nowhere in claims
    report_id = _seed_report(client, player_id, session_id, body)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    reasons = response.json()["reasons"]
    assert any("not recomputable" in reason for reason in reasons)
    assert any("no matching claim" in reason for reason in reasons)
    # the tampered claim also uncovers the wording's true number
    assert any("number 12" in reason for reason in reasons)


@pytest.mark.safety
def test_publish_rejects_dead_links_and_thin_evidence(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    cut_id = _seed_clips(client, session_id, count=1)[0]
    pending_id = _seed_clips(
        client, session_id, count=1, status=ClipStatus.PENDING, camera_id="C2"
    )[0]
    finding_id = _seed_finding(client, session_id)
    body = _body(finding_id, [cut_id, pending_id, str(uuid.uuid4()), "not-a-uuid"])
    report_id = _seed_report(client, player_id, session_id, body)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    reasons = response.json()["reasons"]
    dead = [reason for reason in reasons if "dead evidence link" in reason]
    assert len(dead) == 3  # pending, unknown uuid, junk id — the CUT one is fine
    assert any("status:pending" in reason for reason in dead)
    assert sum("missing" in reason for reason in dead) == 2


@pytest.mark.safety
def test_publish_rejects_a_main_correction_with_one_clip(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_id = _seed_clips(client, session_id, count=1)[0]
    finding_id = _seed_finding(client, session_id)
    report_id = _seed_report(client, player_id, session_id, _body(finding_id, [clip_id]))
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    assert any("fewer than 2 evidence clips" in r for r in response.json()["reasons"])


# --- current-state publish gate (US-H5 stale-verdict SAF) -----------------------


def _inactive_safety() -> dict[str, Any]:
    return {"active": False, "codes": [], "text": "", "sha256": _sha("")}


def _pain_checkin_today(client: TestClient, player_id: str) -> None:
    response = client.post(
        f"/wellness/{player_id}/checkins",
        json={"checkin_date": date.today().isoformat(), "pain": True},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


def _valid_report(client: TestClient, *, safety: dict[str, Any] | None = None) -> tuple[str, str]:
    """(player_id, report_id) for a hash/claims/links-valid draft."""
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id)
    body = _body(finding_id, clip_ids, safety=safety)
    sha = _sha(str(safety["text"])) if safety is not None else None
    report_id = _seed_report(client, player_id, session_id, body, safety_sha256=sha)
    return player_id, report_id


@pytest.mark.safety
def test_publish_blocked_when_state_active_but_verdict_absent(client: TestClient) -> None:
    """SAF (US-H5): a report with NO safety verdict cannot publish while the
    server's CURRENT wellness state is active — hashes alone say nothing."""
    player_id, report_id = _valid_report(client, safety=None)
    _pain_checkin_today(client, player_id)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    payload = response.json()
    assert payload["status"] == "blocked"
    assert any("no safety verdict" in reason for reason in payload["reasons"])
    assert any("pain_flag" in reason for reason in payload["reasons"])


@pytest.mark.safety
def test_publish_blocked_when_verdict_stale_inactive(client: TestClient) -> None:
    """SAF (US-H5): an inactive verdict drafted before a pain report is stale —
    it passes every hash gate but must not publish while state is active."""
    player_id, report_id = _valid_report(client, safety=_inactive_safety())
    _pain_checkin_today(client, player_id)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    assert any("stale" in reason for reason in response.json()["reasons"])


@pytest.mark.safety
def test_publish_allows_active_verdict_while_state_active(client: TestClient) -> None:
    player_id, report_id = _valid_report(client, safety=_safety())
    _pain_checkin_today(client, player_id)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 200


def test_publish_blocked_when_player_row_is_gone(client: TestClient) -> None:
    player_id, report_id = _valid_report(client, safety=None)
    with _db(client) as db:
        db.execute(sa_delete(Player).where(Player.id == uuid.UUID(player_id)))
    response = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    assert any("player" in reason for reason in response.json()["reasons"])


# --- player visibility (US-G3/H5: only PUBLISHED reports reach the player) ------


@pytest.mark.safety
def test_player_reads_only_published_reports(client: TestClient) -> None:
    player_id, report_id = _valid_report(client, safety=None)
    # Draft: invisible to the player on every read surface.
    assert client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN)).status_code == 404
    assert client.get(f"/reports/{report_id}/html", headers=auth(PLAYER_TOKEN)).status_code == 404
    assert client.get(f"/reports?player_id={player_id}", headers=auth(PLAYER_TOKEN)).json() == []
    # Reviewers still see the draft, with its status.
    shown = client.get(f"/reports/{report_id}", headers=auth(COACH_TOKEN)).json()
    assert shown["status"] == "draft"
    listed = client.get(f"/reports?player_id={player_id}", headers=auth(PARENT_TOKEN)).json()
    assert [r["id"] for r in listed] == [report_id]
    # After publishing, the player can read it — with no status banner.
    assert (
        client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN)).status_code == 200
    )
    assert client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN)).status_code == 200
    html = client.get(f"/reports/{report_id}/html", headers=auth(PLAYER_TOKEN))
    assert html.status_code == 200
    assert "STATUS:" not in html.text
    assert [
        r["id"]
        for r in client.get(f"/reports?player_id={player_id}", headers=auth(PLAYER_TOKEN)).json()
    ] == [report_id]


@pytest.mark.safety
def test_player_cannot_fetch_a_blocked_report(client: TestClient) -> None:
    """SAF: a safety-tampered report lands in blocked — and blocked content
    never ships to the player, on JSON or the printable HTML."""
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id)
    body = _body(finding_id, clip_ids, safety=_safety())
    body["safety"]["text"] = "Feeling great - bowl as much as you like!"  # tampered
    report_id = _seed_report(
        client,
        player_id,
        session_id,
        body,
        safety_sha256=_sha("WORKLOAD_CEILING reached. Stop bowling."),
    )
    blocked = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert blocked.status_code == 409
    assert client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN)).status_code == 404
    assert client.get(f"/reports/{report_id}/html", headers=auth(PLAYER_TOKEN)).status_code == 404
    assert client.get(f"/reports?player_id={player_id}", headers=auth(PLAYER_TOKEN)).json() == []
    # Reviewers see the blocked report with the status front and center.
    reviewer_html = client.get(f"/reports/{report_id}/html", headers=auth(COACH_TOKEN))
    assert reviewer_html.status_code == 200
    assert "STATUS: BLOCKED" in reviewer_html.text


# --- evidence-verdict FK race (finding deleted concurrently → 409, not 500) -----


def test_verdict_conflicts_when_finding_deleted_concurrently(tmp_path: Path) -> None:
    """A re-derive can delete the finding between the route's existence check
    and its INSERT; the coach must get a clear 409, not a 500."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    create_all(engine)
    app = make_test_app(tmp_path, engine)
    client = TestClient(app)
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    finding_id = _seed_finding(client, session_id)

    factory = app.state.session_factory

    def racing_get_db() -> Iterator[OrmSession]:
        """Real session whose FIRST flush loses the race: the finding is
        deleted (committed elsewhere in production) before the INSERT lands."""
        db: OrmSession = factory()
        real_flush = db.flush

        def flush(objects: Any = None) -> None:
            db.flush = real_flush  # type: ignore[method-assign]
            with db.no_autoflush:
                db.execute(sa_delete(Finding).where(Finding.id == uuid.UUID(finding_id)))
            real_flush(objects)

        db.flush = flush  # type: ignore[method-assign]
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = racing_get_db
    try:
        response = client.post(
            f"/reports/findings/{finding_id}/verdicts",
            json={"verdict": "confirms", "note": "clear on C1"},
            headers=auth(COACH_TOKEN),
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert "re-fetch" in response.json()["detail"]


def test_publish_checks_secondary_evidence_links_too(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    clip_ids = _seed_clips(client, session_id)
    finding_id = _seed_finding(client, session_id)
    body = _body(finding_id, clip_ids)
    body["secondary"] = [
        {
            "finding_id": finding_id,
            "text": "Also watch the back lift.",
            "evidence": {"3": {"C1": str(uuid.uuid4())}},
        }
    ]
    report_id = _seed_report(client, player_id, session_id, body)
    response = client.post(f"/reports/{report_id}/publish", headers=auth(PARENT_TOKEN))
    assert response.status_code == 409
    assert any("dead evidence link" in r for r in response.json()["reasons"])


# --- evidence verdicts (US-G6) ---------------------------------------------------


def test_coach_verdicts_are_stored_and_listed(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    finding_id = _seed_finding(client, session_id)
    created = client.post(
        f"/reports/findings/{finding_id}/verdicts",
        json={"verdict": "confirms", "note": "clear on C1"},
        headers=auth(COACH_TOKEN),
    )
    assert created.status_code == 201
    assert created.json()["verdict"] == "confirms"
    assert created.json()["actor"] == "coach"
    client.post(
        f"/reports/findings/{finding_id}/verdicts",
        json={"verdict": "not_supported"},
        headers=auth(COACH_TOKEN),
    )
    listed = client.get(f"/reports/findings/{finding_id}/verdicts", headers=auth(PARENT_TOKEN))
    assert [v["verdict"] for v in listed.json()] == ["confirms", "not_supported"]
    assert listed.json()[1]["note"] is None


def test_verdict_rbac_and_404(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    finding_id = _seed_finding(client, session_id)
    payload = {"verdict": "confirms"}
    url = f"/reports/findings/{finding_id}/verdicts"
    assert client.post(url, json=payload, headers=auth(PARENT_TOKEN)).status_code == 403
    assert client.post(url, json=payload, headers=auth(PLAYER_TOKEN)).status_code == 403
    assert client.get(url, headers=auth(PLAYER_TOKEN)).status_code == 403
    missing = f"/reports/findings/{UNKNOWN}/verdicts"
    assert client.post(missing, json=payload, headers=auth(COACH_TOKEN)).status_code == 404
    assert client.get(missing, headers=auth(COACH_TOKEN)).status_code == 404


def test_rule_analytics_flags_unsupported_rules(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    weak_rule = _seed_finding(client, session_id, rule_key="head_position")
    probe = _seed_finding(client, session_id, rule_key=None, kind="zone_probe")
    for verdict in ("not_supported", "not_supported", "confirms"):
        client.post(
            f"/reports/findings/{weak_rule}/verdicts",
            json={"verdict": verdict},
            headers=auth(COACH_TOKEN),
        )
    client.post(
        f"/reports/findings/{probe}/verdicts",
        json={"verdict": "confirms"},
        headers=auth(COACH_TOKEN),
    )
    stats = client.get("/reports/verdicts/analytics", headers=auth(PARENT_TOKEN)).json()
    by_rule = {s["rule_key"]: s for s in stats}
    assert by_rule["head_position"]["flagged"] is True
    assert by_rule["head_position"]["not_supported"] == 2
    assert by_rule["head_position"]["confirms"] == 1
    assert by_rule["probe:zone_probe"]["flagged"] is False
    assert by_rule["probe:zone_probe"]["not_supported_rate"] == 0.0
    assert client.get("/reports/verdicts/analytics", headers=auth(PLAYER_TOKEN)).status_code == 403


def test_analytics_empty_when_no_verdicts(client: TestClient) -> None:
    assert client.get("/reports/verdicts/analytics", headers=auth(COACH_TOKEN)).json() == []
