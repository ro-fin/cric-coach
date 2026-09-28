"""US-J3 planner agent: split discipline, traceability, SAF bowling blocks."""

import hashlib
from typing import Any

import pytest
from cricai_coaching.planner_agent import (
    BOWLING_INTENT,
    BOWLING_SUCCESS_METRIC,
    DEFAULT_BOWLING_BALLS,
    DEFAULT_SUCCESS_METRIC,
    FUN_SUCCESS_METRIC,
    MAINTENANCE,
    MachineEnvelope,
    PlanContext,
    PlannerError,
    build_plan,
    validate_machine_settings,
    validate_plan,
    verify_safety_verdict,
)
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG

SPLIT: dict[str, Any] = DEFAULT_SAFETY_CONFIG["batting_split"]


def finding(finding_key: str = "f-1", metric: str = "control_pct") -> dict[str, Any]:
    return {
        "finding_id": finding_key,
        "agent": "analysis",
        "probe": "zone_contrast",
        "kind": "zone_contrast",
        "severity": "major",
        "metric": metric,
        "condition": {"line": "outside_off", "length": "full"},
        "n": 20,
        "effect_size": -0.3,
        "confidence": 0.9,
        "ball_ids": [1, 2],
        "evidence": {},
        "text_data": {"summary": "template text"},
    }


def drill(
    drill_key: str = "d-1",
    *,
    metric: str = "control_pct",
    intent: str = "technical",
    enabled: bool = True,
    machine_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": drill_key,
        "name": f"drill {drill_key}",
        "target_metric": metric,
        "intent": intent,
        "enabled": enabled,
        "machine_settings": (
            machine_settings
            if machine_settings is not None
            else {"speed_kph": 90, "line": "outside_off", "length": "full"}
        ),
    }


def verdict(*codes: str, active: bool = True, text: str = "Stop bowling.") -> dict[str, Any]:
    return {
        "active": active,
        "codes": list(codes),
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def block_by_intent(blocks: list[dict[str, Any]], intent: str) -> dict[str, Any]:
    matches = [block for block in blocks if block["intent"] == intent]
    assert len(matches) == 1
    return matches[0]


class TestPlanShape:
    def test_blocks_follow_the_split_and_cite_findings_or_maintenance(self) -> None:
        plan = build_plan([finding()], [drill()], PlanContext(split=SPLIT))
        assert [b["intent"] for b in plan.blocks] == [
            "technical",
            "decision",
            "match_scenario",
            "spin_specific",
            "fun",
        ]
        technical = block_by_intent(plan.blocks, "technical")
        assert technical["drill_id"] == "d-1"
        assert technical["finding_id"] == "f-1"
        assert technical["success_metric"] == "control_pct"
        assert technical["balls"] == SPLIT["blocks"]["technical"]
        for intent in ("decision", "match_scenario", "spin_specific"):
            block = block_by_intent(plan.blocks, intent)
            assert block["finding_id"] == MAINTENANCE
            assert block["drill_id"] is None
            assert block["success_metric"] == DEFAULT_SUCCESS_METRIC
        assert plan.finding_ids == ["f-1"]
        assert plan.total_balls == SPLIT["daily_balls"]
        assert plan.safety is None
        assert plan.safety_sha256 is None

    def test_fun_block_present_unconverted_and_uncited(self) -> None:
        fun_drill = drill("d-fun", intent="fun")
        plan = build_plan([finding()], [fun_drill], PlanContext(split=SPLIT))
        fun = block_by_intent(plan.blocks, "fun")
        assert fun == {
            "intent": "fun",
            "balls": SPLIT["blocks"]["fun"],
            "drill_id": None,
            "machine_settings": {},
            "success_metric": FUN_SUCCESS_METRIC,
            "finding_id": None,
        }

    def test_two_findings_map_to_two_intent_blocks(self) -> None:
        findings = [finding("f-1"), finding("f-2")]
        drills = [drill("d-tech", intent="technical"), drill("d-dec", intent="decision")]
        plan = build_plan(findings, drills, PlanContext(split=SPLIT))
        assert block_by_intent(plan.blocks, "technical")["finding_id"] == "f-1"
        assert block_by_intent(plan.blocks, "decision")["finding_id"] == "f-2"
        assert plan.finding_ids == ["f-1", "f-2"]

    def test_finding_without_matching_drill_falls_through_to_next(self) -> None:
        findings = [finding("f-unmapped", metric="head_stability_score"), finding("f-2")]
        plan = build_plan(findings, [drill()], PlanContext(split=SPLIT))
        assert block_by_intent(plan.blocks, "technical")["finding_id"] == "f-2"

    def test_partial_split_config_builds_only_configured_blocks(self) -> None:
        split = {"daily_balls": 200, "blocks": {"technical": 150, "fun": 50}}
        plan = build_plan([], [], PlanContext(split=split))
        assert [b["intent"] for b in plan.blocks] == ["technical", "fun"]

    def test_deterministic_given_identical_inputs(self) -> None:
        context = PlanContext(split=SPLIT, bowling_allowance_balls=30)
        first = build_plan([finding()], [drill()], context)
        second = build_plan([finding()], [drill()], context)
        assert first == second


class TestDrillEligibility:
    def test_disabled_drill_never_enters_a_plan(self) -> None:
        plan = build_plan([finding()], [drill(enabled=False)], PlanContext(split=SPLIT))
        assert block_by_intent(plan.blocks, "technical")["finding_id"] == MAINTENANCE

    def test_out_of_envelope_drill_never_enters_a_plan(self) -> None:
        rogue = drill(machine_settings={"speed_kph": 200})
        plan = build_plan([finding()], [rogue], PlanContext(split=SPLIT))
        assert block_by_intent(plan.blocks, "technical")["drill_id"] is None

    def test_each_drill_used_at_most_once(self) -> None:
        # Two control findings, one drill: the second block stays maintenance.
        findings = [finding("f-1"), finding("f-2")]
        plan = build_plan(findings, [drill()], PlanContext(split=SPLIT))
        assert block_by_intent(plan.blocks, "technical")["finding_id"] == "f-1"
        assert block_by_intent(plan.blocks, "decision")["finding_id"] == MAINTENANCE


class TestMachineEnvelope:
    def test_valid_settings_have_no_problems(self) -> None:
        assert validate_machine_settings({"speed_kph": 90.5, "line": "off", "length": "good"}) == []

    @pytest.mark.parametrize(
        ("settings", "fragment"),
        [
            ({"swing": "in"}, "unknown machine setting"),
            ({"speed_kph": "fast"}, "must be a number"),
            ({"speed_kph": True}, "must be a number"),
            ({"speed_kph": 139.0}, "outside the machine envelope"),
            ({"speed_kph": 12.0}, "outside the machine envelope"),
            ({"line": "wide"}, "not a machine line setting"),
            ({"length": "beamer"}, "not a machine length setting"),
        ],
    )
    def test_invalid_settings_are_reported(self, settings: dict[str, Any], fragment: str) -> None:
        problems = validate_machine_settings(settings)
        assert any(fragment in problem for problem in problems)

    def test_custom_envelope_bounds_apply(self) -> None:
        envelope = MachineEnvelope(speed_kph_min=60.0, speed_kph_max=80.0)
        assert validate_machine_settings({"speed_kph": 90}, envelope) != []


class TestSafetyVerdicts:
    def test_verdict_embedded_verbatim_with_sha256(self) -> None:
        v = verdict("day_pattern_violation")
        plan = build_plan([], [], PlanContext(split=SPLIT, safety=v))
        assert plan.safety == v
        assert plan.safety_sha256 == v["sha256"]

    def test_tampered_verdict_text_refuses_to_plan(self) -> None:
        tampered = verdict("pain_flag") | {"text": "Bowling is fine, actually."}
        with pytest.raises(PlannerError, match="sha256"):
            build_plan([], [], PlanContext(split=SPLIT, safety=tampered))

    def test_verdict_missing_key_refuses_to_plan(self) -> None:
        incomplete = verdict("pain_flag")
        del incomplete["codes"]
        with pytest.raises(PlannerError, match="missing key"):
            verify_safety_verdict(incomplete)

    def test_none_verdict_verifies_to_none(self) -> None:
        assert verify_safety_verdict(None) is None


@pytest.mark.safety
class TestBowlingSafety:
    """SAF (US-H1/H5): the planner NEVER bowls past the allowance or a flag."""

    def test_zero_allowance_means_zero_bowling_blocks(self) -> None:
        plan = build_plan(
            [finding()],
            [drill()],
            PlanContext(split=SPLIT, bowling_allowance_balls=0),
            bowling_request_balls=48,
        )
        assert all(block["intent"] != BOWLING_INTENT for block in plan.blocks)

    def test_default_allowance_is_zero_bowling(self) -> None:
        plan = build_plan([], [], PlanContext(split=SPLIT))
        assert all(block["intent"] != BOWLING_INTENT for block in plan.blocks)

    @pytest.mark.parametrize("code", ["workload_ceiling", "pain_flag"])
    def test_hard_block_codes_suppress_bowling_despite_allowance(self, code: str) -> None:
        context = PlanContext(split=SPLIT, bowling_allowance_balls=100, safety=verdict(code))
        plan = build_plan([finding()], [drill()], context, bowling_request_balls=100)
        assert all(block["intent"] != BOWLING_INTENT for block in plan.blocks)

    def test_inactive_verdict_does_not_block(self) -> None:
        context = PlanContext(
            split=SPLIT,
            bowling_allowance_balls=30,
            safety=verdict("pain_flag", active=False),
        )
        plan = build_plan([], [], context)
        assert block_by_intent(plan.blocks, BOWLING_INTENT)["balls"] == 30

    def test_non_hard_codes_do_not_block(self) -> None:
        context = PlanContext(
            split=SPLIT,
            bowling_allowance_balls=12,
            safety=verdict("day_pattern_violation"),
        )
        plan = build_plan([], [], context)
        assert block_by_intent(plan.blocks, BOWLING_INTENT)["balls"] == 12

    def test_bowling_block_capped_at_allowance(self) -> None:
        context = PlanContext(split=SPLIT, bowling_allowance_balls=18)
        plan = build_plan([], [], context, bowling_request_balls=50)
        bowling = block_by_intent(plan.blocks, BOWLING_INTENT)
        assert bowling["balls"] == 18
        assert bowling["success_metric"] == BOWLING_SUCCESS_METRIC
        assert bowling["finding_id"] == MAINTENANCE

    def test_bowling_defaults_to_thirty_balls_within_allowance(self) -> None:
        plan = build_plan([], [], PlanContext(split=SPLIT, bowling_allowance_balls=100))
        assert block_by_intent(plan.blocks, BOWLING_INTENT)["balls"] == DEFAULT_BOWLING_BALLS

    def test_negative_allowance_is_treated_as_zero(self) -> None:
        plan = build_plan([], [], PlanContext(split=SPLIT, bowling_allowance_balls=-6))
        assert all(block["intent"] != BOWLING_INTENT for block in plan.blocks)

    def test_adversarial_bowling_lure_never_converts_fun_or_bowls(self) -> None:
        # A drill/finding combo engineered to smell like bowling practice:
        # fun-intent drill + a "bowling" metric finding. Zero allowance stands.
        lure_finding = finding("f-lure", metric="landing_accuracy_pct")
        lure_drill = drill("d-lure", metric="landing_accuracy_pct", intent="fun")
        plan = build_plan([lure_finding], [lure_drill], PlanContext(split=SPLIT))
        assert all(block["intent"] != BOWLING_INTENT for block in plan.blocks)
        assert block_by_intent(plan.blocks, "fun")["drill_id"] is None
        assert plan.finding_ids == []


class TestConfigErrors:
    def test_split_without_fun_block_refuses(self) -> None:
        split = {"daily_balls": 500, "blocks": {"technical": 100}}
        with pytest.raises(PlannerError, match="fun block"):
            build_plan([], [], PlanContext(split=split))

    def test_split_without_blocks_mapping_refuses(self) -> None:
        with pytest.raises(PlannerError, match="fun block"):
            build_plan([], [], PlanContext(split={"daily_balls": 500}))

    def test_split_total_above_daily_range_refuses(self) -> None:
        split = {"daily_balls": 100, "blocks": {"technical": 90, "fun": 50}}
        with pytest.raises(PlannerError, match="daily range"):
            build_plan([], [], PlanContext(split=split))

    def test_zero_ball_block_fails_the_builders_own_validator(self) -> None:
        split = {"daily_balls": 500, "blocks": {"technical": 0, "fun": 50}}
        with pytest.raises(PlannerError, match="balls must be positive"):
            build_plan([], [], PlanContext(split=split))


class TestValidatePlan:
    """The publish-time check (US-H5): coach edits run through this too."""

    @staticmethod
    def valid_blocks() -> list[dict[str, Any]]:
        return build_plan(
            [finding()], [drill()], PlanContext(split=SPLIT, bowling_allowance_balls=30)
        ).blocks

    def test_built_plans_validate_clean(self) -> None:
        context = PlanContext(split=SPLIT, bowling_allowance_balls=30)
        assert validate_plan(self.valid_blocks(), context) == []

    def test_bowling_beyond_allowance_is_a_violation(self) -> None:
        blocks = self.valid_blocks()
        context = PlanContext(split=SPLIT, bowling_allowance_balls=10)
        assert any("H1 allowance" in v for v in validate_plan(blocks, context))

    @pytest.mark.safety
    def test_edited_bowling_survives_no_hard_block(self) -> None:
        blocks = self.valid_blocks()  # includes a 30-ball bowling block
        context = PlanContext(split=SPLIT, bowling_allowance_balls=30, safety=verdict("pain_flag"))
        assert any("H1 allowance" in v for v in validate_plan(blocks, context))

    def test_missing_fun_block_is_a_violation(self) -> None:
        blocks = [b for b in self.valid_blocks() if b["intent"] != "fun"]
        violations = validate_plan(blocks, PlanContext(split=SPLIT, bowling_allowance_balls=30))
        assert any("fun block missing" in v for v in violations)

    def test_converted_fun_block_is_a_violation(self) -> None:
        blocks = self.valid_blocks()
        block_by_intent(blocks, "fun")["drill_id"] = "d-sneaky"
        violations = validate_plan(blocks, PlanContext(split=SPLIT, bowling_allowance_balls=30))
        assert any("US-H2 violation" in v for v in violations)

    def test_uncited_non_fun_block_is_a_violation(self) -> None:
        blocks = self.valid_blocks()
        block_by_intent(blocks, "technical")["finding_id"] = None
        violations = validate_plan(blocks, PlanContext(split=SPLIT, bowling_allowance_balls=30))
        assert any("traceability" in v for v in violations)

    def test_unknown_intent_is_a_violation(self) -> None:
        blocks = [*self.valid_blocks(), {"intent": "grind", "balls": 10}]
        violations = validate_plan(blocks, PlanContext(split=SPLIT, bowling_allowance_balls=30))
        assert any("unknown intent" in v for v in violations)

    def test_batting_total_above_daily_range_is_a_violation(self) -> None:
        blocks = self.valid_blocks()
        block_by_intent(blocks, "technical")["balls"] = 400
        violations = validate_plan(blocks, PlanContext(split=SPLIT, bowling_allowance_balls=30))
        assert any("exceeds daily range" in v for v in violations)

    def test_out_of_envelope_block_settings_are_a_violation(self) -> None:
        blocks = self.valid_blocks()
        block_by_intent(blocks, "technical")["machine_settings"] = {"speed_kph": 500}
        violations = validate_plan(blocks, PlanContext(split=SPLIT, bowling_allowance_balls=30))
        assert any("outside the machine envelope" in v for v in violations)
