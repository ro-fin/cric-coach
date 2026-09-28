"""US-L3/US-G4: egress allow-list — only structured metrics may leave the LAN."""

import pytest
from cricai_api.egress import (
    ALLOWED_EGRESS_FIELDS,
    STRICT_FORBIDDEN_FIELDS,
    EgressViolation,
    assert_no_media,
    sanitize_egress_payload,
)
from cricai_coaching.live_allowlist import NEVER_LIVE
from cricai_coaching.llm_writer import FORBIDDEN_PAYLOAD_KEYS


def test_allow_list_never_contains_identity_or_location_fields() -> None:
    assert not ALLOWED_EGRESS_FIELDS & STRICT_FORBIDDEN_FIELDS
    for field in ALLOWED_EGRESS_FIELDS:
        assert "token" not in field
        assert "path" not in field
        # "speed_kph" is a metric; no allow-listed field names a storage key.
        assert "key" not in field


def test_allowlisted_payload_kept_with_sorted_keys() -> None:
    payload = {
        "session_date": "2026-07-07",
        "balls": [
            {
                "shot": "drive",
                "ball_no": 1,
                "line": "off",
                "length": "good",
                "footwork": "front",
                "contact": "middle",
                "outcome": "controlled_ground_shot",
                "control": True,
                "speed_kph": 85.0,
            }
        ],
        "blocks": [{"intent": "technical", "block_no": 1}],
        "metrics": {"metric": "control", "value": 0.55, "count": 20},
    }
    result = sanitize_egress_payload(payload)
    assert list(result) == ["balls", "blocks", "metrics", "session_date"]
    assert list(result["balls"][0]) == [
        "ball_no",
        "contact",
        "control",
        "footwork",
        "length",
        "line",
        "outcome",
        "shot",
        "speed_kph",
    ]
    assert result["blocks"] == [{"block_no": 1, "intent": "technical"}]
    assert result["metrics"] == {"count": 20, "metric": "control", "value": 0.55}


def test_unknown_benign_keys_are_dropped_silently() -> None:
    result = sanitize_egress_payload({"weather": "sunny", "line": "off", "mood": [1, 2]})
    assert result == {"line": "off"}


def test_numeric_suffix_stats_kept_when_flag_on() -> None:
    payload = {"control_pct": 55.0, "loose_count": 3, "sample_n": 12, "line": "off"}
    result = sanitize_egress_payload(payload)
    assert result == {"control_pct": 55.0, "line": "off", "loose_count": 3, "sample_n": 12}


def test_numeric_suffix_stats_dropped_when_flag_off() -> None:
    payload = {"control_pct": 55.0, "line": "off"}
    assert sanitize_egress_payload(payload, allow_extra_numeric_stats=False) == {"line": "off"}


def test_suffix_stats_require_numeric_values() -> None:
    assert sanitize_egress_payload({"control_pct": "55%"}) == {}
    assert sanitize_egress_payload({"control_pct": True}) == {}
    assert sanitize_egress_payload({"drives_count": None}) == {}


@pytest.mark.safety
@pytest.mark.parametrize(
    "payload",
    [
        {"player_name": "Arjun"},
        {"notes": "free text"},
        {"object_key": "sessions/x/C1/a.mp4"},
        {"birthdate": "2014-11-20"},
        {"api_token": "secret"},
        {"file_path": "/data/video.mp4"},
        {"s3_key": "bucket/key"},
        # Phase-5 wellness/plan free text is health-adjacent PII (US-H4/J3).
        {"pain_note": "left knee twinge"},
        {"soreness": {"knee": 2}},
        {"note": "coach free text"},
        {"setup": "single stump, cone at cover"},
        # Phase-6 coach-note body is untrusted human free text (US-K3).
        {"body": "worked on the googly grip today"},
    ],
)
def test_strict_fields_rejected_at_top_level(payload: dict[str, object]) -> None:
    with pytest.raises(EgressViolation):
        sanitize_egress_payload(payload)


@pytest.mark.safety
def test_strict_fields_rejected_at_any_depth_and_listed() -> None:
    payload = {
        "metrics": {"player_name": "Arjun", "control_pct": 55.0},
        "balls": [{"ball_no": 1, "notes": "edged one"}],
        "session_date": "2026-07-07",
    }
    with pytest.raises(EgressViolation) as excinfo:
        sanitize_egress_payload(payload)
    assert excinfo.value.keys == ["notes", "player_name"]
    assert "notes" in str(excinfo.value)
    assert "player_name" in str(excinfo.value)


@pytest.mark.safety
def test_media_detected_anywhere_in_payload() -> None:
    cases: list[dict[str, object]] = [
        {"clip": b"\x00\x01"},
        {"clip": bytearray(b"\x00")},
        {"frame": "data:video/mp4;base64,AAAA"},
        {"ref": "sessions/abc/C1/a.mp4"},
        {"balls": [{"thumb": "data:image/png;base64,iVBOR"}]},
    ]
    for payload in cases:
        with pytest.raises(EgressViolation):
            assert_no_media(payload)


@pytest.mark.safety
def test_media_offender_paths_are_reported() -> None:
    payload = {"balls": [{"ball_no": 1}, {"clip": b"raw"}], "poster": "data:image/png;base64,x"}
    with pytest.raises(EgressViolation) as excinfo:
        assert_no_media(payload)
    assert excinfo.value.keys == ["$.balls[1].clip", "$.poster"]


def test_clean_structured_payload_has_no_media() -> None:
    assert_no_media(
        {
            "session_date": "2026-07-07",
            "balls": [{"ball_no": 1, "control": True, "speed_kph": 85.0}],
            "summary": "session summary text",
        }
    )


@pytest.mark.safety
def test_coaching_mirrors_of_the_strict_set_never_drift() -> None:
    """Findings 20/22: the coaching package cannot import the API app, so its
    two egress nets mirror ``STRICT_FORBIDDEN_FIELDS`` by hand — the LLM-path
    enforcer (``llm_writer.FORBIDDEN_PAYLOAD_KEYS``, the ONLY guard that runs
    on outbound LLM requests) and the live-surface net (``NEVER_LIVE``). This
    is the drift test: any strict field added API-side must land in both."""
    assert STRICT_FORBIDDEN_FIELDS <= FORBIDDEN_PAYLOAD_KEYS
    assert STRICT_FORBIDDEN_FIELDS <= NEVER_LIVE
