"""US-H4 acceptance: check-in validation, pain state machine, escalation, absence."""

import uuid
from datetime import date

import pytest
from cricai_coaching.wellness import (
    ENERGY_MAX,
    ENERGY_MIN,
    SLEEP_HOURS_MAX,
    SLEEP_HOURS_MIN,
    SORENESS_BODY_KEYS,
    adult_cleared_checkin_ids,
    assert_valid_checkin,
    checkin_gaps,
    evaluate_wellness,
    pain_cluster,
    validate_checkin,
)
from cricai_data.models import PainClearance, WellnessCheckin

AS_OF = date(2026, 7, 10)


def _checkin(
    checkin_date: date,
    *,
    pain: bool = False,
    checkin_id: uuid.UUID | None = None,
    player_id: uuid.UUID | None = None,
) -> WellnessCheckin:
    return WellnessCheckin(
        id=checkin_id or uuid.uuid4(),
        player_id=player_id or uuid.uuid4(),
        checkin_date=checkin_date,
        soreness={},
        pain=pain,
        created_by="player",
    )


def _clearance(checkin: WellnessCheckin, role: str) -> PainClearance:
    return PainClearance(
        id=uuid.uuid4(),
        player_id=checkin.player_id,
        checkin_id=checkin.id,
        cleared_by=role,
        role=role,
        note="rested two days, no pain on shadow bowling",
    )


class TestValidateCheckin:
    def test_valid_full_checkin(self) -> None:
        soreness = {"back_lower": 3, "shoulder_right": 0}
        assert validate_checkin(soreness, ENERGY_MIN, SLEEP_HOURS_MIN) == []
        assert validate_checkin(soreness, ENERGY_MAX, SLEEP_HOURS_MAX) == []
        assert validate_checkin({}, None, None) == []

    def test_unknown_body_key_rejected(self) -> None:
        problems = validate_checkin({"left_arm": 1}, None, None)
        assert problems == ["unknown soreness body key: 'left_arm'"]

    def test_soreness_level_type_and_range(self) -> None:
        assert validate_checkin({"knee_left": True}, None, None) == [
            "soreness['knee_left'] must be an integer level, got True"
        ]
        assert validate_checkin({"knee_left": "sore"}, None, None) == [
            "soreness['knee_left'] must be an integer level, got 'sore'"
        ]
        assert validate_checkin({"knee_left": -1}, None, None) == [
            "soreness['knee_left'] must be 0..3, got -1"
        ]
        assert validate_checkin({"knee_left": 4}, None, None) == [
            "soreness['knee_left'] must be 0..3, got 4"
        ]

    def test_energy_bounds(self) -> None:
        assert validate_checkin({}, ENERGY_MIN - 1, None) == ["energy must be 1..5, got 0"]
        assert validate_checkin({}, ENERGY_MAX + 1, None) == ["energy must be 1..5, got 6"]

    def test_sleep_bounds(self) -> None:
        assert validate_checkin({}, None, -0.5) == ["sleep_hours must be 0.0..14.0, got -0.5"]
        assert validate_checkin({}, None, 14.5) == ["sleep_hours must be 0.0..14.0, got 14.5"]

    def test_problems_accumulate_in_deterministic_order(self) -> None:
        problems = validate_checkin({"tail": 9, "back_lower": 9}, 0, 20.0)
        assert problems == [
            "soreness['back_lower'] must be 0..3, got 9",
            "unknown soreness body key: 'tail'",
            "soreness['tail'] must be 0..3, got 9",
            "energy must be 1..5, got 0",
            "sleep_hours must be 0.0..14.0, got 20.0",
        ]

    def test_assert_valid_checkin(self) -> None:
        assert_valid_checkin({"neck": 1}, 3, 9.0)
        with pytest.raises(ValueError, match="invalid wellness check-in: energy must be"):
            assert_valid_checkin({}, 99, None)

    def test_whitelist_covers_bowling_relevant_regions(self) -> None:
        """A young leg-spinner's risk map must at least cover back and shoulder."""
        assert {"back_lower", "back_upper", "shoulder_right", "shoulder_left"} <= (
            SORENESS_BODY_KEYS
        )


class TestAdultClearance:
    def test_only_adult_roles_clear(self) -> None:
        checkin = _checkin(AS_OF, pain=True)
        parent = _clearance(checkin, "parent")
        coach = _clearance(checkin, "coach")
        player = _clearance(checkin, "player")
        assert adult_cleared_checkin_ids([parent]) == {checkin.id}
        assert adult_cleared_checkin_ids([coach]) == {checkin.id}
        assert adult_cleared_checkin_ids([player]) == frozenset()
        assert adult_cleared_checkin_ids([]) == frozenset()


class TestPainCluster:
    def test_empty_and_single(self) -> None:
        assert pain_cluster([]) is False
        assert pain_cluster([AS_OF]) is False

    def test_two_reports_inside_14_days(self) -> None:
        assert pain_cluster([date(2026, 7, 1), date(2026, 7, 14)]) is True

    def test_two_reports_exactly_14_days_apart_are_outside(self) -> None:
        assert pain_cluster([date(2026, 7, 1), date(2026, 7, 15)]) is False

    def test_configurable_window_and_count(self) -> None:
        config = {"pain_escalation_window_days": 7, "pain_escalation_count": 3}
        two = [date(2026, 7, 1), date(2026, 7, 2)]
        assert pain_cluster(two, config) is False
        assert pain_cluster([*two, date(2026, 7, 7)], config) is True

    def test_unordered_input(self) -> None:
        assert pain_cluster([date(2026, 7, 14), date(2026, 7, 1)]) is True


class TestCheckinGaps:
    def test_start_after_end_raises(self) -> None:
        with pytest.raises(ValueError, match="is after end"):
            checkin_gaps([], date(2026, 7, 2), date(2026, 7, 1))

    def test_all_dates_missing(self) -> None:
        assert checkin_gaps([], date(2026, 7, 1), date(2026, 7, 3)) == (
            date(2026, 7, 1),
            date(2026, 7, 2),
            date(2026, 7, 3),
        )

    def test_partial_and_full_coverage(self) -> None:
        rows = [_checkin(date(2026, 7, 1)), _checkin(date(2026, 7, 3))]
        assert checkin_gaps(rows, date(2026, 7, 1), date(2026, 7, 3)) == (date(2026, 7, 2),)
        rows.append(_checkin(date(2026, 7, 2)))
        assert checkin_gaps(rows, date(2026, 7, 1), date(2026, 7, 3)) == ()


class TestEvaluateWellness:
    def test_no_checkins_is_explicit_absence(self) -> None:
        state = evaluate_wellness([], [], AS_OF)
        assert state.no_checkin is True
        assert state.checked_in is False
        assert state.last_checkin_date is None
        assert state.days_since_checkin is None
        assert state.pain_active is False
        assert state.bowling_suppressed is False
        assert state.open_pain_checkin_ids == ()
        assert state.escalation is False
        assert state.pain_reports_in_window == 0

    def test_checkin_today_without_pain(self) -> None:
        state = evaluate_wellness([_checkin(AS_OF)], [], AS_OF)
        assert state.checked_in is True
        assert state.no_checkin is False
        assert state.last_checkin_date == AS_OF
        assert state.days_since_checkin == 0
        assert state.pain_active is False

    def test_stale_checkin_is_visible_as_absence_today(self) -> None:
        state = evaluate_wellness([_checkin(date(2026, 7, 7))], [], AS_OF)
        assert state.checked_in is False
        assert state.no_checkin is True
        assert state.days_since_checkin == 3

    def test_pain_suppresses_bowling_until_adult_clearance(self) -> None:
        checkin = _checkin(AS_OF, pain=True)
        state = evaluate_wellness([checkin], [], AS_OF)
        assert state.pain_active is True
        assert state.bowling_suppressed is True
        assert state.open_pain_checkin_ids == (checkin.id,)

        cleared = evaluate_wellness([checkin], [_clearance(checkin, "parent")], AS_OF)
        assert cleared.pain_active is False
        assert cleared.bowling_suppressed is False
        assert cleared.open_pain_checkin_ids == ()

    def test_player_role_clearance_never_clears(self) -> None:
        checkin = _checkin(AS_OF, pain=True)
        state = evaluate_wellness([checkin], [_clearance(checkin, "player")], AS_OF)
        assert state.pain_active is True

    def test_clearance_is_per_checkin(self) -> None:
        first = _checkin(date(2026, 7, 8), pain=True)
        second = _checkin(AS_OF, pain=True)
        state = evaluate_wellness([first, second], [_clearance(first, "coach")], AS_OF)
        assert state.pain_active is True
        assert state.open_pain_checkin_ids == (second.id,)

    def test_open_pain_ordering_is_deterministic(self) -> None:
        low = _checkin(AS_OF, pain=True, checkin_id=uuid.UUID(int=1))
        high = _checkin(AS_OF, pain=True, checkin_id=uuid.UUID(int=2))
        earlier = _checkin(date(2026, 7, 9), pain=True, checkin_id=uuid.UUID(int=3))
        state = evaluate_wellness([high, low, earlier], [], AS_OF)
        assert state.open_pain_checkin_ids == (earlier.id, low.id, high.id)

    def test_escalation_two_pain_reports_in_trailing_14_days(self) -> None:
        rows = [_checkin(date(2026, 6, 27), pain=True), _checkin(AS_OF, pain=True)]
        state = evaluate_wellness(rows, [], AS_OF)
        assert state.pain_reports_in_window == 2
        assert state.escalation is True

    def test_escalation_window_boundary(self) -> None:
        rows = [_checkin(date(2026, 6, 26), pain=True), _checkin(AS_OF, pain=True)]
        state = evaluate_wellness(rows, [], AS_OF)
        assert state.pain_reports_in_window == 1
        assert state.escalation is False

    def test_clearance_never_erases_escalation_history(self) -> None:
        first = _checkin(date(2026, 7, 1), pain=True)
        second = _checkin(AS_OF, pain=True)
        clearances = [_clearance(first, "parent"), _clearance(second, "coach")]
        state = evaluate_wellness([first, second], clearances, AS_OF)
        assert state.pain_active is False
        assert state.escalation is True

    def test_future_checkins_are_ignored(self) -> None:
        future = _checkin(date(2026, 7, 11), pain=True)
        state = evaluate_wellness([future], [], AS_OF)
        assert state.pain_active is False
        assert state.no_checkin is True
        assert state.last_checkin_date is None

    def test_explicit_config_is_honored(self) -> None:
        config = {"pain_escalation_window_days": 3, "pain_escalation_count": 1}
        state = evaluate_wellness([_checkin(AS_OF, pain=True)], [], AS_OF, config)
        assert state.escalation is True
        outside = evaluate_wellness([_checkin(date(2026, 7, 1), pain=True)], [], AS_OF, config)
        assert outside.escalation is False
        assert outside.pain_active is True
