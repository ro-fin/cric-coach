"""US-L5 SAF: live-surface allow-list — technique corrections are never live."""

import pytest
from cricai_coaching.app_settings import DEFAULT_APP_SETTINGS
from cricai_coaching.live_allowlist import (
    ALLOWED_LIVE_KEYS,
    NEVER_LIVE,
    LiveSurfaceViolation,
    assert_storable_allowlist,
    validate_live_payload,
)
from cricai_coaching.llm_writer import FORBIDDEN_PAYLOAD_KEYS
from cricai_data.ballrecord import BOWLING_FIELDS

pytestmark = pytest.mark.safety

ALLOWLIST: list[str] = list(DEFAULT_APP_SETTINGS["live_mode"]["allowlist"])


class TestValidateLivePayload:
    def test_allow_listed_counters_pass(self) -> None:
        validate_live_payload(
            {
                "ball_count": 42,
                "target_hit_tally": 7,
                "workload_remaining_balls": 120,
                "fatigue_nudge": {"suggestion": "water break"},
            },
            ALLOWLIST,
        )

    def test_empty_payload_passes(self) -> None:
        validate_live_payload({}, ALLOWLIST)

    def test_key_outside_allowlist_raises(self) -> None:
        with pytest.raises(LiveSurfaceViolation, match="outside the stored allow-list: score"):
            validate_live_payload({"ball_count": 1, "score": 4}, ALLOWLIST)

    def test_never_live_key_raises_even_when_allow_listed(self) -> None:
        """The hard-coded SAF net: a technique key smuggled INTO the stored
        allowlist is still rejected — live technique correction is a product
        decision behind coach opt-in, never a config edit (US-L5 AC)."""
        smuggled = [*ALLOWLIST, "brace_state"]
        with pytest.raises(LiveSurfaceViolation, match="can never go live: brace_state"):
            validate_live_payload({"brace_state": "collapsed"}, smuggled)

    def test_never_live_check_wins_over_allowlist_check(self) -> None:
        """A technique key absent from the allowlist reports as the SAF
        violation it is, not as a mere allow-list miss."""
        with pytest.raises(LiveSurfaceViolation, match="can never go live: main_correction"):
            validate_live_payload({"main_correction": {}, "unknown_counter": 1}, ALLOWLIST)

    def test_violation_lists_every_offending_key_sorted(self) -> None:
        with pytest.raises(LiveSurfaceViolation, match="can never go live: brace_state, turn_cm"):
            validate_live_payload({"turn_cm": 3.0, "brace_state": "bent"}, ALLOWLIST)


class TestAssertStorableAllowlist:
    def test_shipped_default_allowlist_is_storable(self) -> None:
        assert_storable_allowlist(ALLOWLIST)

    def test_never_live_entry_rejected_at_write(self) -> None:
        with pytest.raises(LiveSurfaceViolation, match=r"may not be stored .* falling_away_deg"):
            assert_storable_allowlist([*ALLOWLIST, "falling_away_deg"])

    def test_non_string_entries_rejected(self) -> None:
        with pytest.raises(LiveSurfaceViolation, match="entries must be strings: 3, None"):
            assert_storable_allowlist(["ball_count", 3, None])


class TestNeverLiveSet:
    def test_every_ballrecord_bowling_field_is_never_live(self) -> None:
        """Per-ball bowling verdicts (release, brace, turn/flight, variation,
        per-ball target_hit) can never reach a live surface; only the aggregate
        target_hit_tally counter is a different, allowed key."""
        assert set(BOWLING_FIELDS) <= NEVER_LIVE

    def test_correction_surfaces_are_never_live(self) -> None:
        assert {"main_correction", "secondary", "technique_correction", "drill"} <= NEVER_LIVE

    def test_shipped_allowlist_and_never_live_are_disjoint(self) -> None:
        """Drift guard: nobody may ever add a never-live key to the shipped
        default allowlist (or a never-live entry that shadows a counter)."""
        assert not set(ALLOWLIST) & NEVER_LIVE

    def test_every_strict_pii_key_is_never_live(self) -> None:
        """US-L5/L3 (finding 22): the live SAF net bans at least everything the
        LLM-egress strict set bans — identity and free text (body, pain_note,
        soreness, ...) can never ride a live surface either."""
        assert FORBIDDEN_PAYLOAD_KEYS <= NEVER_LIVE
        assert {
            "body",
            "notes",
            "pain_note",
            "soreness",
            "note",
            "setup",
            "player_name",
            "birthdate",
            "object_key",
        } <= NEVER_LIVE

    def test_pii_key_is_rejected_at_write_and_at_emission(self) -> None:
        """Finding 19/22: a settings write carrying pain_note is rejected, and
        even a hand-smuggled stored entry never validates at emission."""
        with pytest.raises(LiveSurfaceViolation, match=r"may not be stored .* pain_note"):
            assert_storable_allowlist([*ALLOWLIST, "pain_note"])
        with pytest.raises(LiveSurfaceViolation, match="can never go live: pain_note"):
            validate_live_payload(
                {"pain_note": "sharp pain in left shoulder"}, [*ALLOWLIST, "pain_note"]
            )


class TestClosedRegistry:
    def test_registry_is_exactly_the_seeded_low_risk_surfaces(self) -> None:
        """US-L5 (finding 25): live mode is counts and safety nudges only; any
        future live key is an explicit registration here, never a config edit."""
        assert frozenset(ALLOWLIST) == ALLOWED_LIVE_KEYS

    def test_registry_and_never_live_are_disjoint(self) -> None:
        assert not ALLOWED_LIVE_KEYS & NEVER_LIVE

    @pytest.mark.parametrize("entry", ["outcome", "control", "speed_kph", "release_height"])
    def test_unregistered_entry_rejected_at_write(self, entry: str) -> None:
        """Per-ball verdict/measurement keys and near-miss variants of
        protected names are not registered — unstorable however approved."""
        with pytest.raises(LiveSurfaceViolation, match=f"registered .*{entry}"):
            assert_storable_allowlist([*ALLOWLIST, entry])

    def test_unregistered_key_rejected_at_emission_even_if_stored(self) -> None:
        """A stored allowlist smuggled past the API (a hand-edited row) still
        cannot emit an unregistered key — write AND emission both check."""
        with pytest.raises(LiveSurfaceViolation, match=r"registered .*outcome"):
            validate_live_payload({"outcome": "edged"}, [*ALLOWLIST, "outcome"])


class TestNestedNeverLive:
    def test_never_live_keys_nested_in_a_dict_value_raise(self) -> None:
        """Finding 23: the SAF net walks values — technique verdicts smuggled
        under an allow-listed counter key are still rejected."""
        smuggled = {
            "ball_count": {
                "head_stability_score": 0.31,
                "turn_cm": 14.2,
                "main_correction": "drop your head",
            }
        }
        with pytest.raises(
            LiveSurfaceViolation,
            match="can never go live: head_stability_score, main_correction, turn_cm",
        ):
            validate_live_payload(smuggled, ALLOWLIST)

    def test_never_live_key_nested_in_a_list_raises(self) -> None:
        with pytest.raises(LiveSurfaceViolation, match="can never go live: pain_note"):
            validate_live_payload({"fatigue_nudge": [{"pain_note": "sharp pain"}]}, ALLOWLIST)

    def test_never_live_key_nested_at_depth_raises(self) -> None:
        deep = {"fatigue_nudge": {"detail": [{"levels": {"brace_state": "collapsed"}}]}}
        with pytest.raises(LiveSurfaceViolation, match="can never go live: brace_state"):
            validate_live_payload(deep, ALLOWLIST)

    def test_benign_nested_payload_passes(self) -> None:
        validate_live_payload(
            {"fatigue_nudge": {"suggestion": "water break", "severity": [1, 2]}}, ALLOWLIST
        )
