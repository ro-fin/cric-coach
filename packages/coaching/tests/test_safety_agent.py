"""US-H5 unit acceptance: verdict assembly, hash verification, publish gate."""

import hashlib
import uuid
from datetime import date
from typing import Any

import pytest
from cricai_coaching.planner_agent import PlanContext, build_plan
from cricai_coaching.safety_agent import (
    CODE_ORDER,
    WARNING_TEXTS,
    SafetySupremacyError,
    canonical_text,
    check_artifact,
    check_verdict,
    evaluate,
    scheduled_bowling,
    sha256_text,
    validate_artifact,
)
from cricai_coaching.wellness import WellnessState
from cricai_data.enums import SafetyCode

AS_OF = date(2026, 7, 10)


def _wellness_state(pain_active: bool) -> WellnessState:
    checkin_ids = (uuid.uuid4(),) if pain_active else ()
    return WellnessState(
        as_of=AS_OF,
        checked_in=True,
        no_checkin=False,
        last_checkin_date=AS_OF,
        days_since_checkin=0,
        pain_active=pain_active,
        bowling_suppressed=pain_active,
        open_pain_checkin_ids=checkin_ids,
        escalation=False,
        pain_reports_in_window=1 if pain_active else 0,
    )


def _artifact_for(verdict: dict[str, Any], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"kind": "daily", "safety": dict(verdict)}
    body.update(extra)
    return body


class TestEvaluate:
    def test_inactive_verdict_hashes_the_empty_text(self) -> None:
        verdict = evaluate(None, None)
        assert verdict == {
            "active": False,
            "codes": [],
            "text": "",
            "sha256": hashlib.sha256(b"").hexdigest(),
        }

    def test_ledger_violations_become_codes_with_verbatim_text(self) -> None:
        verdict = evaluate({"violations": ["workload_ceiling"]}, None)
        assert verdict["active"] is True
        assert verdict["codes"] == ["workload_ceiling"]
        assert verdict["text"] == WARNING_TEXTS[SafetyCode.WORKLOAD_CEILING]
        assert verdict["sha256"] == sha256_text(verdict["text"])

    def test_codes_are_deduplicated_and_canonically_ordered(self) -> None:
        summary = {
            "violations": [
                "day_pattern_violation",
                "workload_ceiling",
                "workload_ceiling",
            ]
        }
        verdict = evaluate(summary, _wellness_state(pain_active=True))
        assert verdict["codes"] == [
            "workload_ceiling",
            "day_pattern_violation",
            "pain_flag",
        ]
        assert verdict["text"] == "\n".join(WARNING_TEXTS[code] for code in CODE_ORDER)

    def test_pain_from_wellness_state_and_mapping(self) -> None:
        assert evaluate(None, _wellness_state(pain_active=True))["codes"] == ["pain_flag"]
        assert evaluate(None, _wellness_state(pain_active=False))["codes"] == []
        assert evaluate(None, {"pain_active": True})["codes"] == ["pain_flag"]
        assert evaluate(None, {"pain_active": False})["codes"] == []
        assert evaluate(None, {})["codes"] == []

    def test_unknown_violation_code_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid SafetyCode"):
            evaluate({"violations": ["be_quiet_about_it"]}, None)

    def test_config_seam_type_checked(self) -> None:
        assert evaluate(None, None, config={"workload": {}})["active"] is False
        with pytest.raises(TypeError, match="config must be a mapping"):
            evaluate(None, None, config=42)  # type: ignore[arg-type]

    def test_ledger_summary_without_violations_key(self) -> None:
        assert evaluate({"weekly_overs": 12.5}, None)["active"] is False


class TestCheckVerdict:
    def test_canonical_verdicts_pass(self) -> None:
        assert check_verdict(evaluate(None, None)) == []
        active = evaluate({"violations": ["workload_ceiling"]}, {"pain_active": True})
        assert check_verdict(active) == []

    def test_wrong_key_set_fails_fast(self) -> None:
        problems = check_verdict({"active": True, "codes": [], "text": ""})
        assert problems == [
            "verdict keys must be exactly ['active', 'codes', 'sha256', 'text'], "
            "got ['active', 'codes', 'text']"
        ]
        extra = dict(evaluate(None, None), override=True)
        assert "verdict keys" in check_verdict(extra)[0]

    def test_unknown_code_rejected(self) -> None:
        verdict = dict(evaluate(None, None), codes=["trust_me"])
        assert check_verdict(verdict) == ["unknown safety code: 'trust_me'"]

    def test_non_canonical_code_order_rejected(self) -> None:
        good = evaluate({"violations": ["workload_ceiling", "day_pattern_violation"]}, None)
        reordered = dict(good, codes=list(reversed(good["codes"])))
        assert any("canonical order" in p for p in check_verdict(reordered))

    def test_duplicate_codes_rejected(self) -> None:
        good = evaluate({"violations": ["workload_ceiling"]}, None)
        doubled = dict(good, codes=["workload_ceiling", "workload_ceiling"])
        assert any("canonical order" in p for p in check_verdict(doubled))

    def test_tampered_text_with_stale_hash(self) -> None:
        verdict = dict(evaluate({"violations": ["workload_ceiling"]}, None))
        verdict["text"] = "All clear, bowl away!"
        problems = check_verdict(verdict)
        assert "verdict text is not the canonical warning text for its codes" in problems
        assert "verdict sha256 does not match its text" in problems

    def test_tampered_text_with_recomputed_hash(self) -> None:
        verdict = dict(evaluate({"violations": ["workload_ceiling"]}, None))
        verdict["text"] = "All clear, bowl away!"
        verdict["sha256"] = sha256_text(verdict["text"])
        problems = check_verdict(verdict)
        assert problems == ["verdict text is not the canonical warning text for its codes"]

    def test_active_flag_must_match_codes(self) -> None:
        flipped = dict(evaluate({"violations": ["pain_flag"]}, None), active=False)
        assert "verdict active flag contradicts its codes" in check_verdict(flipped)
        forged = dict(evaluate(None, None), active=True)
        assert "verdict active flag contradicts its codes" in check_verdict(forged)


class TestScheduledBowling:
    def test_no_bowling(self) -> None:
        assert scheduled_bowling({"blocks": [{"intent": "technical", "balls": 150}]}) == (0, [])

    def test_counts_top_level_and_nested(self) -> None:
        artifact = {
            "bowling_balls": 6,
            "blocks": [{"bowling_balls": 12}, {"nested": ({"bowling_balls": 6},)}],
        }
        assert scheduled_bowling(artifact) == (24, [])

    def test_intent_and_discipline_markers_count(self) -> None:
        assert scheduled_bowling({"blocks": [{"intent": "bowling"}]}) == (1, [])
        assert scheduled_bowling({"drill": {"discipline": "bowling"}}) == (1, [])

    def test_bowling_intent_block_counts_its_balls_not_one(self) -> None:
        """finding [49]: the planner's real block is {intent: bowling, balls: N}."""
        assert scheduled_bowling({"blocks": [{"intent": "bowling", "balls": 30}]}) == (30, [])
        assert scheduled_bowling({"drill": {"discipline": "bowling", "balls": 12}}) == (12, [])

    def test_bowling_intent_block_with_malformed_balls_is_a_problem(self) -> None:
        count, problems = scheduled_bowling({"blocks": [{"intent": "bowling", "balls": "30"}]})
        assert count == 0
        assert problems == ["artifact.blocks[0].balls must be a non-negative integer, got '30'"]

    def test_counts_a_real_planner_bowling_block(self) -> None:
        """Cross-module: a real build_plan bowling block counts its full volume."""
        split = {"daily_balls": 500, "blocks": {"fun": 50}}
        result = build_plan(
            [], [], PlanContext(split=split, bowling_allowance_balls=30), bowling_request_balls=30
        )
        balls, problems = scheduled_bowling({"blocks": result.blocks})
        assert problems == []
        assert balls == 30

    def test_malformed_bowling_balls_is_a_problem_not_zero(self) -> None:
        for bad in ("12", True, -3, 1.5):
            count, problems = scheduled_bowling({"bowling_balls": bad})
            assert count == 0
            assert problems == [
                f"artifact.bowling_balls must be a non-negative integer, got {bad!r}"
            ]

    def test_scalars_and_strings_contribute_nothing(self) -> None:
        assert scheduled_bowling({"note": "bowling_balls", "n": 3, "xs": ["bowling"]}) == (0, [])


class TestCheckArtifact:
    def test_verdict_problems_short_circuit(self) -> None:
        bad_verdict = {"active": True}
        problems = check_artifact({}, bad_verdict)
        assert len(problems) == 1
        assert "verdict keys" in problems[0]

    def test_active_verdict_with_verbatim_safety_block_passes(self) -> None:
        verdict = evaluate({"violations": ["workload_ceiling"]}, None)
        assert check_artifact(_artifact_for(verdict), verdict) == []

    def test_active_verdict_requires_a_safety_block(self) -> None:
        verdict = evaluate(None, {"pain_active": True})
        assert check_artifact({"kind": "daily"}, verdict) == [
            "active safety state but artifact has no safety block"
        ]
        assert check_artifact({"kind": "daily", "safety": "trust me"}, verdict) == [
            "active safety state but artifact has no safety block"
        ]

    def test_artifact_safety_block_must_match_verbatim(self) -> None:
        verdict = evaluate(None, {"pain_active": True})
        artifact = _artifact_for(verdict)
        artifact["safety"]["text"] = verdict["text"] + " (player requested removal)"
        problems = check_artifact(artifact, verdict)
        assert problems == ["artifact safety block does not match the verdict 'text' verbatim"]

    def test_declared_safety_sha256_checked_when_present(self) -> None:
        verdict = evaluate(None, {"pain_active": True})
        ok = _artifact_for(verdict, safety_sha256=verdict["sha256"])
        assert check_artifact(ok, verdict) == []
        bad = _artifact_for(verdict, safety_sha256=sha256_text("something else"))
        assert check_artifact(bad, verdict) == [
            "artifact safety_sha256 does not match the verdict text hash"
        ]

    def test_bowling_blocked_while_ceiling_or_pain_active(self) -> None:
        for summary, wellness in (
            ({"violations": ["workload_ceiling"]}, None),
            (None, {"pain_active": True}),
        ):
            verdict = evaluate(summary, wellness)
            artifact = _artifact_for(verdict, blocks=[{"bowling_balls": 12}])
            problems = check_artifact(artifact, verdict)
            assert len(problems) == 1
            assert "schedules 12 bowling balls while" in problems[0]

    def test_day_pattern_alone_recommends_but_does_not_block_bowling(self) -> None:
        verdict = evaluate({"violations": ["day_pattern_violation"]}, None)
        artifact = _artifact_for(verdict, blocks=[{"bowling_balls": 6}])
        assert check_artifact(artifact, verdict) == []

    def test_inactive_verdict_with_no_or_null_safety_passes(self) -> None:
        verdict = evaluate(None, None)
        assert check_artifact({"kind": "daily"}, verdict) == []
        assert check_artifact({"kind": "daily", "safety": None}, verdict) == []
        assert check_artifact(_artifact_for(verdict), verdict) == []

    def test_inactive_verdict_rejects_forged_safety_block(self) -> None:
        verdict = evaluate(None, None)
        forged = dict(evaluate({"violations": ["workload_ceiling"]}, None))
        assert check_artifact({"safety": forged}, verdict) == [
            "inactive safety state but artifact carries a non-matching safety block"
        ]
        assert check_artifact({"safety": "fine"}, verdict) == [
            "inactive safety state but artifact carries a non-matching safety block"
        ]

    def test_malformed_bowling_volume_reported_alongside(self) -> None:
        verdict = evaluate({"violations": ["workload_ceiling"]}, None)
        artifact = _artifact_for(verdict, blocks=[{"bowling_balls": "many"}])
        problems = check_artifact(artifact, verdict)
        assert problems == [
            "artifact.blocks[0].bowling_balls must be a non-negative integer, got 'many'"
        ]


class TestValidateArtifact:
    def test_passes_silently_when_safe(self) -> None:
        verdict = evaluate({"violations": ["workload_ceiling"]}, None)
        validate_artifact(_artifact_for(verdict), verdict)

    def test_raises_with_every_problem_listed(self) -> None:
        verdict = evaluate({"violations": ["workload_ceiling"]}, None)
        artifact = {"blocks": [{"bowling_balls": 12}]}
        with pytest.raises(SafetySupremacyError) as excinfo:
            validate_artifact(artifact, verdict)
        assert excinfo.value.problems == (
            "active safety state but artifact has no safety block",
            "artifact schedules 12 bowling balls while workload_ceiling active",
        )
        assert "safety supremacy violation" in str(excinfo.value)


class TestCanonicalText:
    def test_empty_codes_empty_text(self) -> None:
        assert canonical_text([]) == ""

    def test_order_is_fixed_regardless_of_input_order(self) -> None:
        assert canonical_text(list(reversed(CODE_ORDER))) == "\n".join(
            WARNING_TEXTS[code] for code in CODE_ORDER
        )
