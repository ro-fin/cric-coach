"""US-L4 alert routing API: audience visibility per role, filters, ack + audit."""

import datetime
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from cricai_data.enums import AlertAudience
from cricai_data.models import Alert, AuditLog
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


@contextmanager
def _db(client: TestClient) -> Iterator[Any]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _seed_alert(
    client: TestClient,
    *,
    audience: AlertAudience,
    code: str = "drift_agreement",
    severity: str = "warning",
    acknowledged: bool = False,
    created_at: datetime.datetime | None = None,
) -> str:
    alert = Alert(
        audience=audience,
        code=code,
        severity=severity,
        detail={"dedupe_key": f"{code}:{uuid.uuid4()}"},
        acknowledged=acknowledged,
    )
    if created_at is not None:
        alert.created_at = created_at
    with _db(client) as db:
        db.add(alert)
        db.flush()
        return str(alert.id)


class TestListRouting:
    def test_parent_sees_parent_audience_only(self, client: TestClient) -> None:
        parent_id = _seed_alert(client, audience=AlertAudience.PARENT, code="data_quality_low")
        _seed_alert(client, audience=AlertAudience.DEVELOPER)
        response = client.get("/alerts", headers=auth(PARENT_TOKEN))
        assert response.status_code == 200
        rows = response.json()
        assert [row["id"] for row in rows] == [parent_id]
        assert rows[0]["audience"] == "parent"
        assert rows[0]["code"] == "data_quality_low"

    def test_coach_sees_developer_audience_only(self, client: TestClient) -> None:
        _seed_alert(client, audience=AlertAudience.PARENT)
        developer_id = _seed_alert(client, audience=AlertAudience.DEVELOPER)
        response = client.get("/alerts", headers=auth(COACH_TOKEN))
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [developer_id]

    def test_player_has_no_alert_access(self, client: TestClient) -> None:
        assert client.get("/alerts", headers=auth(PLAYER_TOKEN)).status_code == 403

    def test_requesting_a_foreign_audience_is_forbidden(self, client: TestClient) -> None:
        response = client.get(
            "/alerts", params={"audience": "developer"}, headers=auth(PARENT_TOKEN)
        )
        assert response.status_code == 403
        response = client.get("/alerts", params={"audience": "parent"}, headers=auth(COACH_TOKEN))
        assert response.status_code == 403

    def test_explicit_own_audience_param_is_allowed(self, client: TestClient) -> None:
        alert_id = _seed_alert(client, audience=AlertAudience.PARENT)
        response = client.get("/alerts", params={"audience": "parent"}, headers=auth(PARENT_TOKEN))
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [alert_id]


class TestListFiltersAndShape:
    def test_acknowledged_filter(self, client: TestClient) -> None:
        open_id = _seed_alert(client, audience=AlertAudience.DEVELOPER)
        acked_id = _seed_alert(client, audience=AlertAudience.DEVELOPER, acknowledged=True)
        headers = auth(COACH_TOKEN)
        open_rows = client.get("/alerts", params={"acknowledged": "false"}, headers=headers)
        assert [row["id"] for row in open_rows.json()] == [open_id]
        acked_rows = client.get("/alerts", params={"acknowledged": "true"}, headers=headers)
        assert [row["id"] for row in acked_rows.json()] == [acked_id]
        both = client.get("/alerts", headers=headers)
        assert {row["id"] for row in both.json()} == {open_id, acked_id}

    def test_newest_first_ordering(self, client: TestClient) -> None:
        old = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
        new = datetime.datetime(2026, 7, 8, tzinfo=datetime.UTC)
        old_id = _seed_alert(client, audience=AlertAudience.DEVELOPER, created_at=old)
        new_id = _seed_alert(client, audience=AlertAudience.DEVELOPER, created_at=new)
        response = client.get("/alerts", headers=auth(COACH_TOKEN))
        assert [row["id"] for row in response.json()] == [new_id, old_id]

    def test_row_shape(self, client: TestClient) -> None:
        alert_id = _seed_alert(client, audience=AlertAudience.DEVELOPER, code="drift_canary")
        row = client.get("/alerts", headers=auth(COACH_TOKEN)).json()[0]
        assert row["id"] == alert_id
        assert row["audience"] == "developer"
        assert row["code"] == "drift_canary"
        assert row["severity"] == "warning"
        assert "dedupe_key" in row["detail"]
        assert row["session_id"] is None
        assert row["acknowledged"] is False
        assert row["created_at"] is not None


class TestAcknowledge:
    def test_ack_flips_flag_and_writes_audit(self, client: TestClient) -> None:
        alert_id = _seed_alert(client, audience=AlertAudience.PARENT, code="data_quality_low")
        response = client.post(f"/alerts/{alert_id}/ack", headers=auth(PARENT_TOKEN))
        assert response.status_code == 200
        assert response.json()["acknowledged"] is True
        with _db(client) as db:
            assert db.get(Alert, uuid.UUID(alert_id)).acknowledged is True
            audits = list(db.scalars(select(AuditLog).where(AuditLog.action == "alert_ack")))
            assert len(audits) == 1
            assert audits[0].actor == "parent"
            assert audits[0].entity == "alert"
            assert audits[0].entity_id == alert_id
            assert audits[0].detail == {"code": "data_quality_low", "audience": "parent"}

    def test_double_ack_is_idempotent_and_audited_once(self, client: TestClient) -> None:
        alert_id = _seed_alert(client, audience=AlertAudience.DEVELOPER)
        headers = auth(COACH_TOKEN)
        first = client.post(f"/alerts/{alert_id}/ack", headers=headers)
        second = client.post(f"/alerts/{alert_id}/ack", headers=headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["acknowledged"] is True
        with _db(client) as db:
            audits = list(db.scalars(select(AuditLog).where(AuditLog.action == "alert_ack")))
            assert len(audits) == 1

    def test_ack_outside_visible_audience_is_forbidden(self, client: TestClient) -> None:
        parent_alert = _seed_alert(client, audience=AlertAudience.PARENT)
        developer_alert = _seed_alert(client, audience=AlertAudience.DEVELOPER)
        assert (
            client.post(f"/alerts/{parent_alert}/ack", headers=auth(COACH_TOKEN)).status_code == 403
        )
        assert (
            client.post(f"/alerts/{developer_alert}/ack", headers=auth(PARENT_TOKEN)).status_code
            == 403
        )
        with _db(client) as db:
            assert db.get(Alert, uuid.UUID(parent_alert)).acknowledged is False

    def test_player_cannot_ack(self, client: TestClient) -> None:
        alert_id = _seed_alert(client, audience=AlertAudience.PARENT)
        response = client.post(f"/alerts/{alert_id}/ack", headers=auth(PLAYER_TOKEN))
        assert response.status_code == 403

    def test_ack_missing_alert_is_404(self, client: TestClient) -> None:
        response = client.post(f"/alerts/{uuid.uuid4()}/ack", headers=auth(PARENT_TOKEN))
        assert response.status_code == 404
