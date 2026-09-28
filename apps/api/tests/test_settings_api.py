"""US-J5/US-L5 settings API: append-only versions, role gates, review queue."""

import copy
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cricai_api.deps import get_db
from cricai_coaching.app_settings import DEFAULT_APP_SETTINGS
from cricai_data.enums import ReportKind, ReportStatus
from cricai_data.models import AppSetting, AuditLog, Player, Report
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app

NOW = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return make_test_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _settings(
    mode: str = "auto_publish",
    timeout_hours: Any = 24,
    *,
    enabled: bool = False,
    allowlist: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "report_review": {"mode": mode, "timeout_hours": timeout_hours},
        "live_mode": {
            "enabled": enabled,
            "allowlist": list(DEFAULT_APP_SETTINGS["live_mode"]["allowlist"])
            if allowlist is None
            else allowlist,
        },
    }


def _post(client: TestClient, settings: dict[str, Any], token: str, reason: str = "change") -> Any:
    return client.post(
        "/settings", json={"settings": settings, "reason": reason}, headers=auth(token)
    )


def _audit_actions(app: FastAPI, action: str) -> list[AuditLog]:
    with app.state.session_factory() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def _seed_player(app: FastAPI) -> uuid.UUID:
    with app.state.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.commit()
        return player.id


def _seed_report(
    app: FastAPI,
    player_id: uuid.UUID,
    *,
    status: ReportStatus = ReportStatus.DRAFT,
    review_due_at: datetime | None = None,
    period: date = date(2026, 7, 10),
) -> uuid.UUID:
    with app.state.session_factory() as db:
        report = Report(
            player_id=player_id,
            kind=ReportKind.DAILY,
            period_start=period,
            period_end=period,
            status=status,
            body={"kind": "daily"},
            review_due_at=review_due_at,
        )
        db.add(report)
        db.commit()
        return report.id


class TestReads:
    def test_unseeded_get_serves_canonical_defaults_as_version_zero(
        self, client: TestClient
    ) -> None:
        response = client.get("/settings", headers=auth(PARENT_TOKEN))
        assert response.status_code == 200
        body = response.json()
        assert body["version"] == 0
        assert body["settings"] == DEFAULT_APP_SETTINGS
        assert body["approved_by"] == "seed"
        assert body["created_at"] is None

    def test_unseeded_versions_list_is_empty(self, client: TestClient) -> None:
        response = client.get("/settings/versions", headers=auth(COACH_TOKEN))
        assert response.status_code == 200
        assert response.json() == []

    def test_get_returns_latest_version(self, client: TestClient) -> None:
        assert _post(client, _settings(timeout_hours=12), PARENT_TOKEN).status_code == 201
        assert _post(client, _settings(timeout_hours=6), COACH_TOKEN).status_code == 201
        body = client.get("/settings", headers=auth(COACH_TOKEN)).json()
        assert body["version"] == 2
        assert body["settings"]["report_review"]["timeout_hours"] == 6
        assert body["created_at"] is not None

    def test_versions_list_oldest_first(self, client: TestClient) -> None:
        _post(client, _settings(timeout_hours=12), PARENT_TOKEN, reason="first")
        _post(client, _settings(timeout_hours=6), PARENT_TOKEN, reason="second")
        versions = client.get("/settings/versions", headers=auth(PARENT_TOKEN)).json()
        assert [v["version"] for v in versions] == [1, 2]
        assert [v["reason"] for v in versions] == ["first", "second"]

    def test_player_cannot_read_settings(self, client: TestClient) -> None:
        assert client.get("/settings", headers=auth(PLAYER_TOKEN)).status_code == 403
        assert client.get("/settings/versions", headers=auth(PLAYER_TOKEN)).status_code == 403


class TestCreateVersion:
    def test_append_writes_version_and_audit(self, app: FastAPI, client: TestClient) -> None:
        response = _post(client, _settings(timeout_hours=12), PARENT_TOKEN, reason="shorter hold")
        assert response.status_code == 201
        body = response.json()
        assert body["version"] == 1
        assert body["approved_by"] == "parent"
        (audit,) = _audit_actions(app, "app_settings_change")
        assert audit.actor == "parent"
        assert audit.entity == "app_settings"
        assert audit.entity_id == "1"
        assert audit.detail is not None
        assert audit.detail["old_version"] == 0
        assert audit.detail["new_version"] == 1
        assert audit.detail["old_settings"] == DEFAULT_APP_SETTINGS
        assert audit.detail["new_settings"]["report_review"]["timeout_hours"] == 12
        assert audit.detail["reason"] == "shorter hold"
        assert audit.detail["review_mode_changed"] is False
        assert audit.detail["live_mode_enabled"] is False

    def test_player_cannot_post(self, client: TestClient) -> None:
        assert _post(client, _settings(), PLAYER_TOKEN).status_code == 403

    def test_mode_change_requires_coach(self, app: FastAPI, client: TestClient) -> None:
        """US-J5: the review gate is the coach's instrument — a parent cannot
        flip it (in either direction) on the coach's behalf."""
        denied = _post(client, _settings(mode="coach_gate"), PARENT_TOKEN)
        assert denied.status_code == 403
        assert "coach approval" in denied.json()["detail"]
        assert _audit_actions(app, "app_settings_change") == []

        allowed = _post(client, _settings(mode="coach_gate"), COACH_TOKEN)
        assert allowed.status_code == 201
        (audit,) = _audit_actions(app, "app_settings_change")
        assert audit.detail is not None
        assert audit.detail["review_mode_changed"] is True

        back = _post(client, _settings(mode="auto_publish"), PARENT_TOKEN)
        assert back.status_code == 403

    def test_live_enable_requires_parent(self, app: FastAPI, client: TestClient) -> None:
        """US-L5: live mode is a guardian opt-in — the coach cannot enable it."""
        denied = _post(client, _settings(enabled=True), COACH_TOKEN)
        assert denied.status_code == 403
        assert "parent approval" in denied.json()["detail"]

        allowed = _post(client, _settings(enabled=True), PARENT_TOKEN)
        assert allowed.status_code == 201
        (audit,) = _audit_actions(app, "app_settings_change")
        assert audit.detail is not None
        assert audit.detail["live_mode_enabled"] is True

    def test_coach_can_disable_live_mode(self, client: TestClient) -> None:
        """Only ENABLING needs the parent; turning live mode off is always open
        to either guardian role (fail-safe direction)."""
        assert _post(client, _settings(enabled=True), PARENT_TOKEN).status_code == 201
        assert _post(client, _settings(enabled=False), COACH_TOKEN).status_code == 201

    def test_allowlist_broadening_requires_parent(self, app: FastAPI, client: TestClient) -> None:
        """US-L5 (finding 19): widening the live surface is a guardian opt-in
        decision like enabling it — once the parent has enabled live mode, a
        coach must not be able to grow the allowlist behind their back.
        Narrowing stays open to either guardian (fail-safe direction)."""
        assert _post(client, _settings(enabled=True), PARENT_TOKEN).status_code == 201
        narrowed = _post(client, _settings(enabled=True, allowlist=["ball_count"]), COACH_TOKEN)
        assert narrowed.status_code == 201  # narrowing: either guardian

        widened = _settings(enabled=True, allowlist=["ball_count", "fatigue_nudge"])
        denied = _post(client, widened, COACH_TOKEN)
        assert denied.status_code == 403
        assert "parent approval" in denied.json()["detail"]

        allowed = _post(client, widened, PARENT_TOKEN)
        assert allowed.status_code == 201
        audit = _audit_actions(app, "app_settings_change")[-1]
        assert audit.detail is not None
        assert audit.detail["live_allowlist_broadened"] is True

    def test_allowlist_broadening_needs_parent_even_while_disabled(
        self, client: TestClient
    ) -> None:
        """A coach must not pre-stage a wider allowlist while live mode is off:
        the parent's later enable click would silently approve it (US-L5)."""
        assert _post(client, _settings(allowlist=["ball_count"]), COACH_TOKEN).status_code == 201
        widened = _settings(allowlist=["ball_count", "target_hit_tally"])
        assert _post(client, widened, COACH_TOKEN).status_code == 403
        assert _post(client, widened, PARENT_TOKEN).status_code == 201

    def test_both_gates_needed_means_two_versions(self, client: TestClient) -> None:
        """One POST changing the mode AND enabling live mode cannot pass as
        either role: each change is its own approved version (two decisions)."""
        both = _settings(mode="coach_gate", enabled=True)
        assert _post(client, both, COACH_TOKEN).status_code == 403
        assert _post(client, both, PARENT_TOKEN).status_code == 403
        assert _post(client, _settings(mode="coach_gate"), COACH_TOKEN).status_code == 201
        assert (
            _post(client, _settings(mode="coach_gate", enabled=True), PARENT_TOKEN).status_code
            == 201
        )

    def test_version_race_is_409(self, app: FastAPI, client: TestClient) -> None:
        """Two concurrent appends compute the same next version; the loser of
        the unique(version) race gets a clear 409, and no audit row survives."""
        factory = app.state.session_factory

        def racing_get_db() -> Iterator[OrmSession]:
            db: OrmSession = factory()
            real_flush = db.flush

            def flush(objects: Any = None) -> None:
                if any(isinstance(obj, AppSetting) for obj in db.new):
                    db.flush = real_flush  # type: ignore[method-assign]
                    with db.no_autoflush:
                        db.execute(
                            insert(AppSetting).values(
                                version=1,
                                settings=_settings(),
                                approved_by="other",
                                reason="race",
                            )
                        )
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
            response = _post(client, _settings(timeout_hours=12), PARENT_TOKEN)
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 409
        assert response.json()["detail"] == "settings version conflict, retry"
        assert _audit_actions(app, "app_settings_change") == []


class TestValidation:
    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda s: s.pop("live_mode"), "sections must be exactly"),
            (lambda s: s.update(extra={}), "sections must be exactly"),
            (lambda s: s.__setitem__("report_review", "auto"), "no 'report_review' section"),
            (
                lambda s: s["report_review"].__setitem__("mode", "sometimes"),
                "unknown report_review.mode",
            ),
            (
                lambda s: s["report_review"].__setitem__("timeout_hours", 0),
                "positive number",
            ),
            (lambda s: s.__setitem__("live_mode", []), "live_mode must be an object"),
            (
                lambda s: s["live_mode"].__setitem__("enabled", "yes"),
                "live_mode.enabled must be a boolean",
            ),
            (
                lambda s: s["live_mode"].__setitem__("allowlist", "ball_count"),
                "live_mode.allowlist must be a list",
            ),
            (
                lambda s: s["live_mode"].__setitem__("allowlist", ["ball_count", 3]),
                "entries must be strings",
            ),
        ],
    )
    def test_invalid_shapes_are_422(self, client: TestClient, mutate: Any, fragment: str) -> None:
        settings = _settings()
        mutate(settings)
        response = _post(client, settings, COACH_TOKEN)
        assert response.status_code == 422
        assert fragment in response.json()["detail"]

    @pytest.mark.safety
    def test_never_live_key_cannot_be_stored_in_allowlist(
        self, app: FastAPI, client: TestClient
    ) -> None:
        """SAF defense in depth (US-L5): even a coach-approved version cannot
        put a technique-correction key on the live allow-list."""
        smuggle = _settings(
            allowlist=[*DEFAULT_APP_SETTINGS["live_mode"]["allowlist"], "brace_state"]
        )
        response = _post(client, smuggle, COACH_TOKEN)
        assert response.status_code == 422
        assert "may not be stored" in response.json()["detail"]
        assert "brace_state" in response.json()["detail"]
        assert _audit_actions(app, "app_settings_change") == []

    def test_narrowing_the_allowlist_is_open_to_guardians(self, client: TestClient) -> None:
        response = _post(client, _settings(allowlist=["ball_count"]), COACH_TOKEN)
        assert response.status_code == 201
        assert response.json()["settings"]["live_mode"]["allowlist"] == ["ball_count"]

    @pytest.mark.safety
    def test_unregistered_live_key_cannot_be_stored_in_allowlist(
        self, app: FastAPI, client: TestClient
    ) -> None:
        """SAF (US-L5, finding 25): the stored allowlist is a CLOSED registry —
        per-ball verdict/measurement keys (outcome, control, speed_kph) and
        near-miss variants of protected names (release_height) are not
        registered live surfaces, so even a parent-approved POST is a 422."""
        for entry in ("outcome", "control", "speed_kph", "release_height"):
            widened = _settings(allowlist=[*DEFAULT_APP_SETTINGS["live_mode"]["allowlist"], entry])
            response = _post(client, widened, PARENT_TOKEN)
            assert response.status_code == 422, entry
            assert entry in response.json()["detail"]
        assert _audit_actions(app, "app_settings_change") == []


class TestReviewQueue:
    def test_queue_lists_only_gate_held_drafts_earliest_first(
        self, app: FastAPI, client: TestClient
    ) -> None:
        player_id = _seed_player(app)
        later = _seed_report(
            app, player_id, review_due_at=NOW + timedelta(hours=2), period=date(2026, 7, 8)
        )
        sooner = _seed_report(
            app, player_id, review_due_at=NOW + timedelta(hours=1), period=date(2026, 7, 9)
        )
        _seed_report(app, player_id, review_due_at=None, period=date(2026, 7, 7))
        _seed_report(
            app,
            player_id,
            status=ReportStatus.PUBLISHED,
            review_due_at=NOW - timedelta(hours=1),
            period=date(2026, 7, 6),
        )
        _seed_report(
            app,
            player_id,
            status=ReportStatus.BLOCKED,
            review_due_at=NOW - timedelta(hours=2),
            period=date(2026, 7, 5),
        )

        response = client.get("/settings/review-queue", headers=auth(COACH_TOKEN))
        assert response.status_code == 200
        queue = response.json()
        assert [item["id"] for item in queue] == [str(sooner), str(later)]
        first = queue[0]
        assert first["player_id"] == str(player_id)
        assert first["kind"] == "daily"
        assert first["period_start"] == "2026-07-09"
        assert first["session_id"] is None
        assert datetime.fromisoformat(first["review_due_at"]) == NOW + timedelta(hours=1)

    def test_empty_queue(self, client: TestClient) -> None:
        assert client.get("/settings/review-queue", headers=auth(COACH_TOKEN)).json() == []

    def test_queue_is_coach_only(self, client: TestClient) -> None:
        """US-J5: the review queue is the coach's worklist — parent and player
        roles are refused (parents read reports through /reports)."""
        assert client.get("/settings/review-queue", headers=auth(PARENT_TOKEN)).status_code == 403
        assert client.get("/settings/review-queue", headers=auth(PLAYER_TOKEN)).status_code == 403

    def test_queue_requires_auth(self, client: TestClient) -> None:
        assert client.get("/settings/review-queue").status_code == 401


def test_gate_lifecycle_round_trip(app: FastAPI, client: TestClient) -> None:
    """ST (US-J5): coach turns the gate on; a held draft appears in the queue;
    settings history keeps every step."""
    assert _post(client, _settings(mode="coach_gate"), COACH_TOKEN).status_code == 201
    player_id = _seed_player(app)
    _seed_report(app, player_id, review_due_at=NOW + timedelta(hours=24))

    queue = client.get("/settings/review-queue", headers=auth(COACH_TOKEN)).json()
    assert len(queue) == 1

    versions = client.get("/settings/versions", headers=auth(PARENT_TOKEN)).json()
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["settings"]["report_review"]["mode"] == "coach_gate"
    assert copy.deepcopy(DEFAULT_APP_SETTINGS)["report_review"]["mode"] == "auto_publish"
