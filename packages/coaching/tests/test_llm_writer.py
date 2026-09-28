"""LLM writer hard guarantees (US-G4): egress allow-list, template conformance,
numeric-claim validation, safety verbatim passthrough, audit rows, fallback."""

import copy
import hashlib
import json
import uuid
from datetime import UTC
from typing import Any

import pytest
from cricai_coaching.llm import FakeLLMProvider, LLMError
from cricai_coaching.llm_writer import (
    DEFAULT_REWRITABLE_FIELDS,
    LLMRejection,
    LLMReportWriter,
    WordingPrompt,
    build_audit_row,
)
from cricai_coaching.report import validate_claims_coverage

PROMPT_KEY = "report_wording"

SAFETY_TEXT = "STOP: weekly bowling ceiling of 16 overs reached. No more bowling this window."
SAFETY = {
    "active": True,
    "codes": ["workload_ceiling"],
    "text": SAFETY_TEXT,
    "sha256": hashlib.sha256(SAFETY_TEXT.encode("utf-8")).hexdigest(),
}


def _body() -> dict[str, Any]:
    """A Report-body-v1-shaped daily report (pinned contract 4)."""
    return {
        "kind": "daily",
        "period": {"start": "2026-07-09", "end": "2026-07-09"},
        "main_correction": {
            "finding_id": "f-1",
            "text": "Front foot too straight on 42 balls; control 57 to 81 when moved.",
            "evidence": {"7": {"cam1": "clip-7a"}, "9": {"cam1": "clip-9a"}},
        },
        "drill": {
            "drill_id": "d-1",
            "text": "Tomorrow: 80-ball cover-drive block.",
            "machine_settings": {"speed_kph": 92.0, "line": "outside_off"},
            "success_metric": "control_pct",
        },
        "goal": {"metric": "control_pct", "target": 70, "condition": "full outside off"},
        "secondary": [
            {"finding_id": "f-2", "text": "Head falls over on straight balls.", "evidence": {}}
        ],
        "positive": "Great intent against spin: 12 controlled sweeps.",
        "safety": copy.deepcopy(SAFETY),
        "honesty_banner": None,
        "claims": [
            {"value": 42, "metric": "balls", "recompute_key": "n:zone4"},
            {"value": 57, "metric": "control_pct", "recompute_key": "before"},
            {"value": 81, "metric": "control_pct", "recompute_key": "after"},
            {"value": 80, "metric": "balls", "recompute_key": "drill"},
            {"value": 12, "metric": "count", "recompute_key": "sweeps"},
        ],
    }


def _context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "findings": [{"finding_id": "f-1", "metric": "control_pct", "n": 42}],
        "history": {"sessions_summarised": 5},
        "rules": ["Front foot moves to the ball on full deliveries."],
        "tone": "encouraging",
        "safety": copy.deepcopy(SAFETY),
    }
    context.update(overrides)
    return context


def _template(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "safety"}


def _reworded_template(body: dict[str, Any]) -> dict[str, Any]:
    template = copy.deepcopy(_template(body))
    template["main_correction"]["text"] = (
        "Your front foot stayed straight on 42 balls - control jumped from 57 to 81 "
        "when you stepped to the ball."
    )
    template["drill"]["text"] = "Tomorrow you get an 80-ball cover-drive block."
    template["secondary"][0]["text"] = "Watch your head on the straight ones."
    template["positive"] = "Loved the 12 controlled sweeps - keep that intent going."
    return template


def _writer(canned_template: dict[str, Any], **kwargs: Any) -> LLMReportWriter:
    provider = FakeLLMProvider(canned={PROMPT_KEY: json.dumps(canned_template)})
    return LLMReportWriter(provider, **kwargs)


def test_write_returns_reworded_body_with_safety_reinserted_verbatim() -> None:
    body = _body()
    result = _writer(_reworded_template(body)).write(body, _context())
    assert result["positive"] == "Loved the 12 controlled sweeps - keep that intent going."
    assert result["safety"] == SAFETY
    assert result["safety"] is not body["safety"]  # deep copy: caller can't mutate the verdict
    assert result["safety"]["text"] == SAFETY_TEXT
    assert result["goal"] == body["goal"]
    assert result["claims"] == body["claims"]


def test_write_accepts_a_verbatim_template_echo() -> None:
    body = _body()
    assert _writer(_template(body)).write(body, _context()) == body


def test_safety_is_never_sent_to_the_provider() -> None:
    """US-H5: warning text is not LLM-reachable content."""
    rows: list[dict[str, Any]] = []
    body = _body()
    writer = _writer(_reworded_template(body), on_exchange=rows.append)
    writer.write(body, _context())
    sent = json.dumps(rows[0]["request"]["payload"])
    assert SAFETY_TEXT not in sent
    assert "sha256" not in sent


def test_body_without_safety_key_round_trips_without_one() -> None:
    body = _body()
    del body["safety"]
    result = _writer(_reworded_template(body)).write(body, _context(safety=None))
    assert "safety" not in result


def test_null_safety_passes_through_as_null() -> None:
    body = _body()
    body["safety"] = None
    result = _writer(_reworded_template(body)).write(body, _context(safety=None))
    assert result["safety"] is None


def test_int_evidence_keys_are_canonicalised_to_strings() -> None:
    body = _body()
    body["main_correction"]["evidence"] = {7: {"cam1": "clip-7a"}}
    reworded = _reworded_template(body)
    reworded["main_correction"]["evidence"] = {"7": {"cam1": "clip-7a"}}
    result = _writer(reworded).write(body, _context())
    assert result["main_correction"]["evidence"] == {"7": {"cam1": "clip-7a"}}


def test_tuple_context_values_pass_the_egress_walk() -> None:
    body = _body()
    result = _writer(_reworded_template(body)).write(body, _context(rules=("rule a", "rule b")))
    assert result["kind"] == "daily"


# --- audit rows -----------------------------------------------------------


def test_accepted_exchange_emits_an_accepted_audit_row() -> None:
    rows: list[dict[str, Any]] = []
    body = _body()
    _writer(_reworded_template(body), on_exchange=rows.append).write(body, _context())
    (row,) = rows
    assert row["accepted"] is True
    assert row["reason"] is None
    assert row["prompt_key"] == PROMPT_KEY
    assert row["report_id"] is None
    assert row["request"]["payload"]["template"]["kind"] == "daily"
    assert row["request"]["max_tokens"] == 2048
    assert row["response"]["model"] == "fake-llm-seed0"
    assert row["created_at"].tzinfo == UTC


def test_provider_failure_re_raises_and_audits_the_reason() -> None:
    rows: list[dict[str, Any]] = []
    provider = FakeLLMProvider(failures={PROMPT_KEY: "socket timeout"})
    writer = LLMReportWriter(provider, on_exchange=rows.append)
    with pytest.raises(LLMError, match="socket timeout"):
        writer.write(_body(), _context())
    (row,) = rows
    assert row["accepted"] is False
    assert row["reason"] == "provider: socket timeout"
    assert row["response"] == {}


def test_rejection_emits_a_rejected_audit_row_with_the_response() -> None:
    rows: list[dict[str, Any]] = []
    body = _body()
    writer = LLMReportWriter(
        FakeLLMProvider(canned={PROMPT_KEY: "not json at all"}), on_exchange=rows.append
    )
    with pytest.raises(LLMRejection, match="response: not valid JSON"):
        writer.write(body, _context())
    (row,) = rows
    assert row["accepted"] is False
    assert row["reason"].startswith("response: not valid JSON")
    assert row["response"]["text"] == "not json at all"


def test_rejections_do_not_require_an_audit_sink() -> None:
    writer = LLMReportWriter(FakeLLMProvider(canned={PROMPT_KEY: "[]"}))
    with pytest.raises(LLMRejection, match="must be a JSON object"):
        writer.write(_body(), _context())


def test_build_audit_row_derives_accepted_from_the_reason() -> None:
    accepted = build_audit_row(prompt_key="k", request={}, response={"text": "ok"})
    assert accepted["accepted"] is True
    assert accepted["reason"] is None
    rejected = build_audit_row(prompt_key="k", request={}, response={}, reason="claims: 40")
    assert rejected["accepted"] is False
    assert rejected["reason"] == "claims: 40"


def test_build_audit_row_passes_report_id_through_and_defaults_response() -> None:
    report_id = uuid.uuid4()
    row = build_audit_row(
        prompt_key="k",
        request={"payload": {}},
        response=None,
        reason="provider: down",
        report_id=report_id,
    )
    assert row["report_id"] == report_id
    assert row["response"] == {}
    assert row["created_at"].tzinfo == UTC


# --- template conformance --------------------------------------------------


def _rejects(
    body: dict[str, Any],
    candidate: dict[str, Any],
    match: str,
    context: dict[str, Any] | None = None,
) -> None:
    with pytest.raises(LLMRejection, match=match):
        _writer(candidate).write(body, context if context is not None else _context())


def test_added_section_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["bowling_advice"] = "Bowl 40 overs this week."
    _rejects(body, candidate, r"unexpected keys \['bowling_advice'\]")


def test_missing_section_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    del candidate["positive"]
    _rejects(body, candidate, r"missing keys \['positive'\]")


def test_changed_number_leaf_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["goal"]["target"] = 90
    _rejects(body, candidate, "value changed at body.goal.target")


def test_number_type_drift_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["goal"]["target"] = 70.0
    _rejects(body, candidate, "value changed at body.goal.target")


def test_null_field_turned_into_text_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["honesty_banner"] = "all clear"
    _rejects(body, candidate, "value changed at body.honesty_banner")


def test_non_wording_string_change_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["main_correction"]["finding_id"] = "f-9"
    _rejects(body, candidate, "non-wording text changed at body.main_correction.finding_id")


def test_wording_field_must_stay_a_string() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["positive"] = 42
    _rejects(body, candidate, "text changed type at body.positive")


def test_list_length_change_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["secondary"].append({"finding_id": "f-3", "text": "extra", "evidence": {}})
    _rejects(body, candidate, r"list length changed at body.secondary \(1 -> 2\)")


def test_object_replaced_by_list_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["main_correction"] = []
    _rejects(body, candidate, "expected an object at body.main_correction")


def test_list_replaced_by_string_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["secondary"] = "none"
    _rejects(body, candidate, "expected a list at body.secondary")


def test_rewritable_fields_are_configurable() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["goal"]["condition"] = "full deliveries outside off stump"
    provider = FakeLLMProvider(canned={PROMPT_KEY: json.dumps(candidate)})
    fields = DEFAULT_REWRITABLE_FIELDS | {"condition"}
    writer = LLMReportWriter(provider, rewritable_fields=fields)
    result = writer.write(body, _context())
    assert result["goal"]["condition"] == "full deliveries outside off stump"


def test_custom_wording_prompt_routes_canned_output_and_audits() -> None:
    rows: list[dict[str, Any]] = []
    body = _body()
    provider = FakeLLMProvider(canned={"weekly_wording": json.dumps(_reworded_template(body))})
    prompt = WordingPrompt(prompt_key="weekly_wording", system="short words", max_tokens=999)
    writer = LLMReportWriter(provider, prompt=prompt, on_exchange=rows.append)
    writer.write(body, _context())
    assert rows[0]["prompt_key"] == "weekly_wording"
    assert rows[0]["request"]["system"] == "short words"
    assert rows[0]["request"]["max_tokens"] == 999


# --- numeric-claim validation ----------------------------------------------


def _claims_body(positive: str, claims: Any) -> dict[str, Any]:
    return {"positive": positive, "claims": claims, "safety": None}


def _word_claims(body: dict[str, Any], worded_positive: str) -> dict[str, Any]:
    candidate = _template(body)
    candidate["positive"] = worded_positive
    return candidate


def _claims_context() -> dict[str, Any]:
    return {"findings": [], "history": {}, "rules": [], "tone": "", "safety": None}


@pytest.mark.parametrize(
    "worded",
    [
        "You hit 57 in control.",  # exact claim value
        "Control jumped from 57 to 81.",  # two exact claim values
        "Call it 57.34 on average.",  # exact claim value, decimals intact
        "No numbers here at all.",
    ],
)
def test_numbers_derivable_from_claims_are_accepted(worded: str) -> None:
    claims = [
        {"value": 57, "metric": "m", "recompute_key": "a"},
        {"value": 81, "metric": "m", "recompute_key": "b"},
        {"value": 57.34, "metric": "m", "recompute_key": "c"},
        {"value": 12, "metric": "m", "recompute_key": "d"},
        {"value": 16, "metric": "m", "recompute_key": "e"},
    ]
    body = _claims_body("placeholder", claims)
    result = _writer(_word_claims(body, worded)).write(body, _claims_context())
    assert result["positive"] == worded


@pytest.mark.parametrize(
    ("worded", "offending"),
    [
        ("That is 138 balls across both blocks.", "138"),  # 57 + 81 sum — no longer allowed
        ("An improvement of 24 points.", "24"),  # 81 - 57 difference — no longer allowed
        ("Call it 57.3 on average.", "57.3"),  # 57.34 rounded to 1dp — no longer allowed
    ],
)
def test_derived_arithmetic_is_no_longer_accepted(worded: str, offending: str) -> None:
    """finding [44/56]: the writer accepts ONLY exact claim values, so its
    verdict matches the publish gate's byte-for-byte claims coverage."""
    claims = [
        {"value": 57, "metric": "m", "recompute_key": "a"},
        {"value": 81, "metric": "m", "recompute_key": "b"},
        {"value": 57.34, "metric": "m", "recompute_key": "c"},
    ]
    body = _claims_body("placeholder", claims)
    _rejects(
        body,
        _word_claims(body, worded),
        f"claims: number {offending} is not a claim value",
        _claims_context(),
    )


def test_negative_claim_values_match_negative_numbers_in_text() -> None:
    claims = [{"value": -4.2, "metric": "cm", "recompute_key": "a"}]
    body = _claims_body("placeholder", claims)
    worded = "Foot moved -4.2 cm on average."
    assert _writer(_word_claims(body, worded)).write(body, _claims_context())["positive"] == worded


@pytest.mark.parametrize(
    ("worded", "offending"),
    [
        ("You should bowl 40 overs.", "40"),  # not a claim at all
        ("Control was 57.34 exactly.", "57.34"),  # only 57.3 is claimed
        ("That makes 42 sweeps.", "42"),  # 6 * 7 — products are not whitelisted
    ],
)
def test_numbers_not_derivable_from_claims_are_rejected(worded: str, offending: str) -> None:
    claims = [
        {"value": 57.3, "metric": "m", "recompute_key": "a"},
        {"value": 6, "metric": "m", "recompute_key": "b"},
        {"value": 7, "metric": "m", "recompute_key": "c"},
    ]
    body = _claims_body("placeholder", claims)
    _rejects(
        body,
        _word_claims(body, worded),
        f"claims: number {offending} is not a claim value",
        _claims_context(),
    )


def test_missing_or_malformed_claims_reject_every_number() -> None:
    body = {"positive": "placeholder", "safety": None}
    _rejects(body, {"positive": "Bowl 5 overs."}, "claims: number 5", _claims_context())
    malformed = _claims_body(
        "placeholder",
        [{"value": "57"}, "not-a-dict", {"metric": "n"}, {"value": True}],
    )
    _rejects(
        malformed,
        _word_claims(malformed, "Control hit 57."),
        "claims: number 57",
        _claims_context(),
    )
    non_list = _claims_body("placeholder", "57")
    _rejects(
        non_list,
        _word_claims(non_list, "Control hit 57."),
        "claims: number 57",
        _claims_context(),
    )


def test_writer_reads_the_same_numbers_as_the_publish_gate() -> None:
    """finding [56]: one shared regex — the writer reads a digit exactly where
    the publish gate does. 'v1.2' yields 1.2 (no lookbehind that the gate lacks),
    so an unclaimed 1.2 is rejected; claiming it makes writer AND gate accept."""
    body = _claims_body("placeholder", [{"value": 3, "metric": "m", "recompute_key": "a"}])
    worded = "Drill v1.2 has 3 stations."
    # 1.2 is unclaimed: rejected by the writer, exactly as the publish gate would.
    _rejects(
        body,
        _word_claims(body, worded),
        "claims: number 1.2 is not a claim value",
        _claims_context(),
    )
    # Claim 1.2 too: now writer-accepted, and the finished body clears the gate.
    claimed = _claims_body(
        "placeholder",
        [
            {"value": 3, "metric": "m", "recompute_key": "a"},
            {"value": 1.2, "metric": "m", "recompute_key": "b"},
        ],
    )
    result = _writer(_word_claims(claimed, worded)).write(claimed, _claims_context())
    assert result["positive"] == worded
    assert validate_claims_coverage(result) == []


# --- finding [56]: writer/publish-gate number regex unified (no divergence) --


def test_t95_style_glued_number_is_rejected_like_the_publish_gate() -> None:
    """'T95': the shared regex reads 95, so an unclaimed 95 is rejected by the
    writer exactly as the publish gate rejects it. Pre-fix the writer's lookbehind
    regex never saw 95 (glued to 'T') and accepted a body the gate then 409'd."""
    claims = [{"value": 57, "metric": "control", "recompute_key": "a"}]
    body = _claims_body("placeholder", claims)
    _rejects(
        body,
        _word_claims(body, "You reached T95 form with 57 in control."),
        "claims: number 95 is not a claim value",
        _claims_context(),
    )


def test_hyphen_range_is_rejected_like_the_publish_gate() -> None:
    """'12-16': the shared report.py regex binds the hyphen as a minus and reads
    -16, so the range is rejected by the writer exactly as the publish gate does.
    Pre-fix the writer's lookbehind read 12 and 16 and accepted the wording, which
    the publish gate then rejected forever ('-16') with no rule-based fallback."""
    claims = [
        {"value": 12, "metric": "overs", "recompute_key": "a"},
        {"value": 16, "metric": "overs", "recompute_key": "b"},
    ]
    body = _claims_body("placeholder", claims)
    _rejects(
        body,
        _word_claims(body, "Range was 12-16 overs."),
        "claims: number -16 is not a claim value",
        _claims_context(),
    )


# --- safety pre-flight -------------------------------------------------------


def test_corrupt_safety_hash_is_rejected_before_the_provider_is_called() -> None:
    rows: list[dict[str, Any]] = []
    body = _body()
    body["safety"]["sha256"] = "0" * 64
    provider = FakeLLMProvider(failures={PROMPT_KEY: "provider must not be reached"})
    writer = LLMReportWriter(provider, on_exchange=rows.append)
    with pytest.raises(LLMRejection, match="sha256 does not match"):
        writer.write(body, _context(safety=copy.deepcopy(body["safety"])))
    assert rows[0]["request"]["payload"] == {"redacted": "pre-flight rejection"}


def test_body_and_context_safety_must_agree() -> None:
    body = _body()
    softened = copy.deepcopy(SAFETY)
    softened["text"] = "Maybe ease off the bowling a touch."
    with pytest.raises(LLMRejection, match="body and context verdicts differ"):
        _writer(_reworded_template(body)).write(body, _context(safety=softened))


def test_context_verdict_without_a_body_slot_is_rejected() -> None:
    body = _body()
    del body["safety"]
    with pytest.raises(LLMRejection, match="context carries a verdict the body lacks"):
        _writer(_reworded_template(body)).write(body, _context())


def test_non_dict_safety_verdict_is_rejected() -> None:
    body = _body()
    body["safety"] = "all clear"
    with pytest.raises(LLMRejection, match="must be an object or null"):
        _writer(_reworded_template(body)).write(body, _context(safety=None))


def test_safety_verdict_missing_text_or_sha_is_rejected() -> None:
    body = _body()
    body["safety"] = {"active": True, "codes": [], "text": SAFETY_TEXT}
    with pytest.raises(LLMRejection, match="missing text/sha256"):
        _writer(_reworded_template(body)).write(body, _context(safety=None))


# --- egress guard ------------------------------------------------------------


def _egress_rejects(context: dict[str, Any], match: str) -> None:
    rows: list[dict[str, Any]] = []
    body = _body()
    provider = FakeLLMProvider(failures={PROMPT_KEY: "provider must not be reached"})
    writer = LLMReportWriter(provider, on_exchange=rows.append)
    with pytest.raises(LLMRejection, match=match):
        writer.write(body, context)
    assert rows[0]["accepted"] is False
    assert rows[0]["request"]["payload"] == {"redacted": "pre-flight rejection"}


def test_forbidden_keys_are_rejected_at_any_depth_case_insensitively() -> None:
    _egress_rejects(
        _context(history={"summary": {"Player_Name": "someone"}}),
        "egress: forbidden key 'Player_Name'",
    )
    _egress_rejects(
        _context(findings=[{"finding_id": "f-1", "notes": "free text"}]),
        "egress: forbidden key 'notes'",
    )
    _egress_rejects(
        _context(history={"birthdate": "2014-03-02"}), "egress: forbidden key 'birthdate'"
    )
    _egress_rejects(
        _context(findings=[{"evidence": [{"pain_note": "sore"}]}]),
        "egress: forbidden key 'pain_note'",
    )


def test_coach_note_body_is_forbidden_egress() -> None:
    """US-K3/G4 (finding 20): ``coach_notes.body`` is untrusted free text about
    a child — the ``body`` key is rejected on the ACTUAL LLM egress path (this
    enforcer runs on every outbound request), not only in the API-side
    sanitizer that nothing on this path calls."""
    _egress_rejects(
        _context(findings=[{"kind": "coach_note", "body": "Timmy cried after nets"}]),
        "egress: forbidden key 'body'",
    )
    _egress_rejects(
        _context(history={"summary": {"Body": "nested free text"}}),
        "egress: forbidden key 'Body'",
    )


def test_storage_keys_are_rejected_as_values_and_keys() -> None:
    _egress_rejects(
        _context(findings=[{"clip": "sessions/2026-07-09/cam1/ball7.mp4"}]),
        "egress: storage key 'sessions/",
    )
    _egress_rejects(
        _context(history={"sessions/2026-07-09": 1}), "used as a key at payload.history"
    )


def test_binary_payload_content_is_rejected() -> None:
    _egress_rejects(_context(findings=[{"frame": b"\x89PNG"}]), "egress: binary value")
    _egress_rejects(_context(history={b"raw": 1}), "egress: binary key")


def test_forbidden_key_inside_the_body_template_is_rejected() -> None:
    body = _body()
    body["drill"]["setup"] = "two cones on a length"
    provider = FakeLLMProvider(failures={PROMPT_KEY: "provider must not be reached"})
    with pytest.raises(LLMRejection, match="egress: forbidden key 'setup'"):
        LLMReportWriter(provider).write(body, _context())


def test_set_and_frozenset_values_are_egress_walked() -> None:
    """finding [16/57]: set/frozenset values are walked, not skipped silently."""
    _egress_rejects(
        _context(findings=[{"tags": {"sessions/2026-07-09/cam1/x.mp4"}}]),
        "egress: storage key",
    )
    _egress_rejects(
        _context(history={"frames": frozenset([b"\x89PNG"])}),
        "egress: binary value",
    )


# --- content lint over rewritten wording (US-G2/H4, finding [55]) ------------


def test_shaming_wording_from_the_provider_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["positive"] = "Honestly you were lazy and hopeless out there."
    _rejects(body, candidate, "content lint")


def test_medical_diagnosis_wording_from_the_provider_is_rejected() -> None:
    body = _body()
    candidate = _reworded_template(body)
    candidate["main_correction"]["text"] = "That looks like a stress fracture, honestly."
    _rejects(body, candidate, "content lint")
