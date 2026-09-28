"""US-J5 review gate decisions: hold under coach_gate, expire at the deadline."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cricai_coaching.app_settings import DEFAULT_APP_SETTINGS
from cricai_coaching.review_gate import (
    active_app_settings,
    review_due_at_for,
    review_settings,
    should_hold,
    timeout_expired,
)
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import ReviewMode
from cricai_data.models import AppSetting, utcnow
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)


def _settings(mode: str = "coach_gate", timeout_hours: Any = 24) -> dict[str, Any]:
    return {
        "report_review": {"mode": mode, "timeout_hours": timeout_hours},
        "live_mode": {"enabled": False, "allowlist": []},
    }


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return make_session_factory(engine)


class TestReviewSettings:
    def test_parses_mode_and_timeout(self) -> None:
        mode, timeout = review_settings(_settings("coach_gate", 6))
        assert mode is ReviewMode.COACH_GATE
        assert timeout == 6.0

    def test_default_settings_parse_to_auto_publish(self) -> None:
        mode, timeout = review_settings(DEFAULT_APP_SETTINGS)
        assert mode is ReviewMode.AUTO_PUBLISH
        assert timeout == 24.0

    def test_missing_section_rejected(self) -> None:
        with pytest.raises(ValueError, match="no 'report_review' section"):
            review_settings({"live_mode": {"enabled": False, "allowlist": []}})

    def test_non_mapping_section_rejected(self) -> None:
        with pytest.raises(ValueError, match="no 'report_review' section"):
            review_settings({"report_review": "coach_gate"})

    def test_unknown_mode_rejected(self) -> None:
        """A corrupt stored mode fails loudly, never coerces to a gate decision."""
        with pytest.raises(ValueError, match=r"unknown report_review\.mode: 'sometimes'"):
            review_settings(_settings(mode="sometimes"))

    @pytest.mark.parametrize("timeout", [0, -1, "24", None, True])
    def test_bad_timeout_rejected(self, timeout: Any) -> None:
        with pytest.raises(ValueError, match="timeout_hours must be a positive number"):
            review_settings(_settings(timeout_hours=timeout))


class TestShouldHold:
    def test_auto_publish_never_holds(self) -> None:
        assert should_hold(_settings(mode="auto_publish"), now=NOW) is None

    def test_coach_gate_holds_until_now_plus_timeout(self) -> None:
        assert should_hold(_settings("coach_gate", 24), now=NOW) == NOW + timedelta(hours=24)

    def test_timeout_is_configurable(self) -> None:
        assert should_hold(_settings("coach_gate", 6), now=NOW) == NOW + timedelta(hours=6)

    def test_default_now_is_current_utc(self) -> None:
        before = utcnow()
        due = should_hold(_settings("coach_gate", 1))
        after = utcnow()
        assert due is not None
        assert before + timedelta(hours=1) <= due <= after + timedelta(hours=1)

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            should_hold(_settings(), now=NOW.replace(tzinfo=None))


class TestTimeoutExpired:
    def test_never_held_never_expires(self) -> None:
        assert timeout_expired(None, NOW) is False

    def test_before_deadline_not_expired(self) -> None:
        assert timeout_expired(NOW + timedelta(seconds=1), NOW) is False

    def test_at_deadline_expired(self) -> None:
        assert timeout_expired(NOW, NOW) is True

    def test_after_deadline_expired(self) -> None:
        assert timeout_expired(NOW - timedelta(hours=1), NOW) is True

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            timeout_expired(NOW, NOW.replace(tzinfo=None))

    def test_naive_deadline_rejected(self) -> None:
        """Dialect-naive round-trips must be coerced by the caller, explicitly."""
        with pytest.raises(ValueError, match="review_due_at must be timezone-aware"):
            timeout_expired(NOW.replace(tzinfo=None), NOW)


class TestActiveAppSettings:
    def test_unseeded_returns_canonical_defaults(self, factory: sessionmaker[Session]) -> None:
        with factory() as db:
            assert active_app_settings(db) == DEFAULT_APP_SETTINGS

    def test_returned_defaults_are_a_copy(self, factory: sessionmaker[Session]) -> None:
        """Mutating the result can never corrupt the canonical constant."""
        with factory() as db:
            settings = active_app_settings(db)
            settings["report_review"]["mode"] = "coach_gate"
            assert DEFAULT_APP_SETTINGS["report_review"]["mode"] == "auto_publish"

    def test_latest_version_wins(self, factory: sessionmaker[Session]) -> None:
        with factory() as db:
            db.add(
                AppSetting(
                    version=1, settings=_settings("auto_publish"), approved_by="seed", reason="v1"
                )
            )
            db.add(
                AppSetting(
                    version=2,
                    settings=_settings("coach_gate", 12),
                    approved_by="coach",
                    reason="gate on",
                )
            )
            db.commit()
            active = active_app_settings(db)
        assert active["report_review"] == {"mode": "coach_gate", "timeout_hours": 12}

    def test_returned_row_settings_are_a_copy(self, factory: sessionmaker[Session]) -> None:
        """Mutating the result can never write through to the ORM row's dict."""
        with factory() as db:
            row = AppSetting(version=1, settings=_settings(), approved_by="coach", reason="v1")
            db.add(row)
            db.commit()
            settings = active_app_settings(db)
            settings["report_review"]["timeout_hours"] = 999
            assert row.settings["report_review"]["timeout_hours"] == 24


class TestReviewDueAtFor:
    def test_unseeded_defaults_do_not_hold(self, factory: sessionmaker[Session]) -> None:
        """The shipped default is auto_publish: no review_due_at (US-J5 opt-in)."""
        with factory() as db:
            assert review_due_at_for(db, now=NOW) is None

    def test_coach_gate_version_holds(self, factory: sessionmaker[Session]) -> None:
        with factory() as db:
            db.add(
                AppSetting(
                    version=1,
                    settings=_settings("coach_gate", 24),
                    approved_by="coach",
                    reason="gate on",
                )
            )
            db.commit()
            assert review_due_at_for(db, now=NOW) == NOW + timedelta(hours=24)
