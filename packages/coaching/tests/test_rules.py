"""US-G2 coaching-rule DSL and runner: parse/validate (reject unknown ops,
metrics, malformed conditions/gates, banned text), the min-sample + trigger
gates, per-player overrides (disable/adjust with re-validation), latest-approved
rule selection, and Finding-shaped emission (pinned contract #2).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cricai_coaching.rules import (
    DEFAULT_MIN_N,
    ParsedRule,
    RuleError,
    apply_overrides,
    evaluate_rule,
    parse_rule_definition,
    run_rules,
    select_active_rules,
)
from cricai_data.models import CoachingRule, RuleOverride

_BASE = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def numeric_def(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "metric": "front_foot_direction_cm",
        "op": "lt",
        "value": 15.0,
        "condition": {"line": ["outside_off"], "length": ["full"]},
        "min_n": 10,
        "severity": "minor",
        "trigger_share": 0.5,
        "text_data": {"finding": "Front foot is short to the pitch of the ball."},
    }
    definition.update(overrides)
    return definition


def categorical_def(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "metric": "contact_quality",
        "op": "eq",
        "value": "edge",
        "condition": {"line": ["outside_off"]},
        "min_n": 10,
        "severity": "major",
        "text_data": {"finding": "Edges outside off."},
    }
    definition.update(overrides)
    return definition


def rec(ball_id: int, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"ball_id": ball_id, "clips": {}, "confidence": {}}
    record.update(fields)
    return record


def rule_row(rule_key: str, **overrides: Any) -> CoachingRule:
    fields: dict[str, Any] = {
        "rule_key": rule_key,
        "version": 1,
        "author": "coach",
        "approved_by": "coach",
        "rationale": "because",
        "definition": numeric_def(),
        "enabled": True,
    }
    fields.update(overrides)
    return CoachingRule(**fields)


def override_row(rule_key: str, action: str, order: int = 0, **overrides: Any) -> RuleOverride:
    fields: dict[str, Any] = {
        "rule_key": rule_key,
        "player_id": uuid.uuid4(),
        "action": action,
        "params": {},
        "reason": "player-specific",
        "actor": "coach",
        "created_at": _BASE + timedelta(minutes=order),
    }
    fields.update(overrides)
    return RuleOverride(**fields)


# --------------------------------------------------------------------------
# parse_rule_definition
# --------------------------------------------------------------------------


class TestParseValid:
    def test_numeric_rule_round_trips(self) -> None:
        parsed = parse_rule_definition(numeric_def())
        assert parsed == ParsedRule(
            metric="front_foot_direction_cm",
            op="lt",
            value=15.0,
            condition={"line": ("outside_off",), "length": ("full",)},
            min_n=10,
            severity="minor",
            text_data={"finding": "Front foot is short to the pitch of the ball."},
            trigger_share=0.5,
        )

    def test_defaults_fill_min_n_and_condition(self) -> None:
        parsed = parse_rule_definition(
            {
                "metric": "control",
                "op": "eq",
                "value": False,
                "severity": "info",
                "text_data": {"note": "Short balls hurried you."},
            }
        )
        assert parsed.min_n == DEFAULT_MIN_N
        assert parsed.condition == {}
        assert parsed.trigger_share == 0.5
        assert parsed.value is False

    def test_categorical_open_vocabulary_accepts_any_string(self) -> None:
        parsed = parse_rule_definition(
            categorical_def(metric="bat_path", value="across", condition={})
        )
        assert parsed.value == "across"


class TestParseRejects:
    def test_not_a_mapping(self) -> None:
        with pytest.raises(RuleError, match="definition must be a mapping"):
            parse_rule_definition(["nope"])  # type: ignore[arg-type]

    def test_unknown_keys(self) -> None:
        with pytest.raises(RuleError, match="unknown definition keys"):
            parse_rule_definition(numeric_def(bogus=1))

    def test_missing_keys(self) -> None:
        definition = numeric_def()
        del definition["severity"]
        with pytest.raises(RuleError, match=r"missing definition keys.*severity"):
            parse_rule_definition(definition)

    def test_unknown_metric(self) -> None:
        with pytest.raises(RuleError, match="unknown metric 'wobble'"):
            parse_rule_definition(numeric_def(metric="wobble"))

    def test_unknown_op(self) -> None:
        with pytest.raises(RuleError, match="unknown op 'ne'"):
            parse_rule_definition(numeric_def(op="ne"))

    def test_eq_on_numeric_metric_rejected(self) -> None:
        with pytest.raises(RuleError, match="op 'eq' is not allowed on numeric"):
            parse_rule_definition(numeric_def(op="eq", value=15.0))

    def test_numeric_metric_needs_number(self) -> None:
        with pytest.raises(RuleError, match="needs a numeric threshold"):
            parse_rule_definition(numeric_def(value="low"))

    def test_ordering_op_on_categorical_rejected(self) -> None:
        with pytest.raises(RuleError, match="needs a numeric metric"):
            parse_rule_definition(categorical_def(op="lt"))

    def test_boolean_metric_needs_bool(self) -> None:
        with pytest.raises(RuleError, match="needs a boolean value"):
            parse_rule_definition(
                {
                    "metric": "control",
                    "op": "eq",
                    "value": "false",
                    "severity": "info",
                    "text_data": {"note": "x"},
                }
            )

    def test_categorical_value_unknown_vocabulary(self) -> None:
        with pytest.raises(RuleError, match=r"unknown 'contact_quality'|unknown 'line'"):
            parse_rule_definition(categorical_def(metric="line", value="wide", condition={}))

    def test_unknown_severity(self) -> None:
        with pytest.raises(RuleError, match="unknown severity 'critical'"):
            parse_rule_definition(numeric_def(severity="critical"))

    @pytest.mark.parametrize("bad", [0, -1, 1.0, True])
    def test_min_n_must_be_positive_int(self, bad: Any) -> None:
        with pytest.raises(RuleError, match="min_n must be an integer"):
            parse_rule_definition(numeric_def(min_n=bad))

    @pytest.mark.parametrize("bad", [0.0, 1.5, "half", -0.1])
    def test_trigger_share_range(self, bad: Any) -> None:
        with pytest.raises(RuleError, match="trigger_share must be a number"):
            parse_rule_definition(numeric_def(trigger_share=bad))


class TestParseCondition:
    def test_condition_not_mapping(self) -> None:
        with pytest.raises(RuleError, match="condition must be a mapping"):
            parse_rule_definition(numeric_def(condition=["off"]))

    def test_unknown_condition_field(self) -> None:
        with pytest.raises(RuleError, match="unknown condition field 'weather'"):
            parse_rule_definition(numeric_def(condition={"weather": ["wet"]}))

    def test_condition_values_must_be_nonempty_list(self) -> None:
        with pytest.raises(RuleError, match="must list at least one allowed value"):
            parse_rule_definition(numeric_def(condition={"line": []}))

    def test_boolean_condition_field_needs_bool_values(self) -> None:
        with pytest.raises(RuleError, match="condition 'control' values must be booleans"):
            parse_rule_definition(numeric_def(condition={"control": ["true"]}))

    def test_boolean_condition_field_accepts_bool(self) -> None:
        parsed = parse_rule_definition(numeric_def(condition={"control": [False]}))
        assert parsed.condition["control"] == (False,)

    def test_categorical_condition_value_must_be_string(self) -> None:
        with pytest.raises(RuleError, match="condition 'line' values must be non-empty strings"):
            parse_rule_definition(numeric_def(condition={"line": [7]}))

    def test_categorical_condition_value_unknown(self) -> None:
        with pytest.raises(RuleError, match="unknown 'line' condition value 'wide'"):
            parse_rule_definition(numeric_def(condition={"line": ["wide"]}))


class TestParseTextData:
    def test_text_data_must_be_nonempty_mapping(self) -> None:
        with pytest.raises(RuleError, match="text_data must be a non-empty mapping"):
            parse_rule_definition(numeric_def(text_data={}))

    def test_text_data_entry_must_be_nonempty_string(self) -> None:
        with pytest.raises(RuleError, match="must be a non-empty string"):
            parse_rule_definition(numeric_def(text_data={"finding": "   "}))

    def test_text_data_non_string_name(self) -> None:
        with pytest.raises(RuleError, match="must be a non-empty string"):
            parse_rule_definition(numeric_def(text_data={1: "hello there"}))

    def test_text_data_content_lint_rejects_banned_phrase(self) -> None:
        with pytest.raises(RuleError, match="failed the content lint"):
            parse_rule_definition(numeric_def(text_data={"finding": "You always drop your head."}))


# --------------------------------------------------------------------------
# evaluate_rule
# --------------------------------------------------------------------------


class TestEvaluateRule:
    def _matching(self, n: int, value: float) -> list[dict[str, Any]]:
        return [
            rec(i, line="outside_off", length="full", front_foot_direction_cm=value)
            for i in range(1, n + 1)
        ]

    def test_below_min_sample_never_fires(self) -> None:
        rule = parse_rule_definition(numeric_def(min_n=10))
        assert evaluate_rule("k", rule, self._matching(9, 10.0)) is None

    def test_below_trigger_share_never_fires(self) -> None:
        rule = parse_rule_definition(numeric_def(min_n=4, trigger_share=0.75))
        records = self._matching(2, 10.0) + self._matching(2, 30.0)  # only half hit
        assert evaluate_rule("k", rule, records) is None

    def test_fires_with_finding_shape(self) -> None:
        rule = parse_rule_definition(numeric_def(min_n=10, trigger_share=0.5))
        records = [
            rec(
                i,
                line="outside_off",
                length="full",
                front_foot_direction_cm=8.0,
                clips={"C1": f"clip-{i}"},
                confidence={"front_foot_direction_cm": 0.8},
            )
            for i in range(1, 13)
        ]
        finding = evaluate_rule("front_foot", rule, records, version=3, agent="rules")
        assert finding is not None
        assert finding["finding_id"].startswith("ru-")  # deterministic id (finding [37])
        assert finding["agent"] == "rules"
        assert finding["rule_key"] == "front_foot"
        assert finding["kind"] == "rule"
        assert finding["severity"] == "minor"
        assert finding["metric"] == "front_foot_direction_cm"
        assert finding["condition"] == {"line": ["outside_off"], "length": ["full"]}
        assert finding["n"] == 12
        assert finding["effect_size"] == 1.0
        assert finding["confidence"] == pytest.approx(0.8)
        assert finding["ball_ids"] == list(range(1, 13))
        assert finding["evidence"]["1"] == {"C1": "clip-1"}
        assert finding["payload"]["aggregate"] == pytest.approx(8.0)  # numeric mean
        assert finding["payload"]["threshold"] == 15.0
        assert finding["payload"]["hits"] == 12
        assert finding["payload"]["rule_version"] == 3

    def test_finding_id_is_deterministic_for_the_same_session_and_data(self) -> None:
        """finding [37]: same rule + hit balls -> byte-identical id (regression snapshots)."""
        rule = parse_rule_definition(numeric_def(min_n=10, trigger_share=0.5))
        records = self._matching(12, 8.0)
        first = evaluate_rule("front_foot", rule, records, version=3)
        second = evaluate_rule("front_foot", rule, records, version=3)
        assert first is not None and second is not None
        assert first["finding_id"] == second["finding_id"]
        assert first["finding_id"].startswith("ru-")

    def test_finding_id_changes_with_the_hit_ball_set(self) -> None:
        rule = parse_rule_definition(numeric_def(min_n=10, trigger_share=0.5))
        twelve = evaluate_rule("k", rule, self._matching(12, 8.0), version=1)
        thirteen = evaluate_rule("k", rule, self._matching(13, 8.0), version=1)
        assert twelve is not None and thirteen is not None
        assert twelve["finding_id"] != thirteen["finding_id"]

    def test_finding_id_changes_with_rule_version(self) -> None:
        rule = parse_rule_definition(numeric_def(min_n=10, trigger_share=0.5))
        records = self._matching(12, 8.0)
        v1 = evaluate_rule("k", rule, records, version=1)
        v2 = evaluate_rule("k", rule, records, version=2)
        assert v1 is not None and v2 is not None
        assert v1["finding_id"] != v2["finding_id"]

    def test_non_matching_and_null_metric_balls_excluded_from_denominator(self) -> None:
        rule = parse_rule_definition(numeric_def(min_n=2, trigger_share=0.5))
        records = [
            rec(1, line="outside_off", length="full", front_foot_direction_cm=8.0),
            rec(2, line="outside_off", length="full", front_foot_direction_cm=10.0),
            rec(3, line="leg", length="full", front_foot_direction_cm=8.0),  # wrong zone
            rec(4, line="outside_off", length="full", front_foot_direction_cm=None),  # null metric
        ]
        finding = evaluate_rule("k", rule, records)
        assert finding is not None
        assert finding["n"] == 2  # balls 1 and 2 only

    def test_categorical_aggregate_is_hit_share(self) -> None:
        rule = parse_rule_definition(categorical_def(metric="contact_quality", min_n=2))
        records = [
            rec(1, line="outside_off", contact_quality="edge"),
            rec(2, line="outside_off", contact_quality="middle"),
        ]
        finding = evaluate_rule("edges", rule, records)
        assert finding is not None
        assert finding["payload"]["aggregate"] == pytest.approx(0.5)

    def test_default_confidence_when_metric_confidence_absent(self) -> None:
        rule = parse_rule_definition(categorical_def(metric="contact_quality", min_n=1))
        finding = evaluate_rule("edges", rule, [rec(1, line="outside_off", contact_quality="edge")])
        assert finding is not None
        assert finding["confidence"] == 1.0

    @pytest.mark.parametrize(
        ("op", "value", "observed", "hits"),
        [
            ("lt", 15.0, 14.0, True),
            ("lt", 15.0, 15.0, False),
            ("le", 15.0, 15.0, True),
            ("gt", 15.0, 16.0, True),
            ("gt", 15.0, 15.0, False),
            ("ge", 15.0, 15.0, True),
            ("ge", 15.0, 14.0, False),
        ],
    )
    def test_ordering_ops(self, op: str, value: float, observed: float, hits: bool) -> None:
        rule = parse_rule_definition(numeric_def(op=op, value=value, min_n=1, trigger_share=1.0))
        record = rec(1, line="outside_off", length="full", front_foot_direction_cm=observed)
        finding = evaluate_rule("k", rule, [record])
        assert (finding is not None) == hits


# --------------------------------------------------------------------------
# select_active_rules + overrides + run_rules
# --------------------------------------------------------------------------


class TestSelectActiveRules:
    def test_latest_version_wins(self) -> None:
        rows = [rule_row("r", version=1), rule_row("r", version=3), rule_row("r", version=2)]
        active = select_active_rules(rows)
        assert active["r"].version == 3

    def test_disabled_latest_retires_rule(self) -> None:
        rows = [rule_row("r", version=1), rule_row("r", version=2, enabled=False)]
        assert select_active_rules(rows) == {}

    def test_unapproved_latest_is_excluded(self) -> None:
        rows = [rule_row("r", version=1, approved_by=None)]
        assert select_active_rules(rows) == {}


class TestApplyOverrides:
    def test_no_overrides_parses_definition(self) -> None:
        parsed = apply_overrides(numeric_def(), [])
        assert parsed is not None
        assert parsed.value == 15.0

    def test_disable_wins(self) -> None:
        overrides = [
            override_row("r", "adjust", order=0, params={"value": 5.0}),
            override_row("r", "disable", order=1),
        ]
        assert apply_overrides(numeric_def(), overrides) is None

    def test_adjust_merges_in_creation_order(self) -> None:
        overrides = [
            override_row("r", "adjust", order=0, params={"value": 5.0}),
            override_row("r", "adjust", order=1, params={"value": 9.0, "min_n": 15}),
        ]
        parsed = apply_overrides(numeric_def(), overrides)
        assert parsed is not None
        assert parsed.value == 9.0  # later override wins
        assert parsed.min_n == 15

    def test_malformed_adjustment_raises(self) -> None:
        overrides = [override_row("r", "adjust", params={"op": "eq"})]
        with pytest.raises(RuleError, match="op 'eq' is not allowed on numeric"):
            apply_overrides(numeric_def(), overrides)


class TestRunRules:
    def _hitting_records(self, n: int = 12) -> list[dict[str, Any]]:
        return [
            rec(i, line="outside_off", length="full", front_foot_direction_cm=8.0)
            for i in range(1, n + 1)
        ]

    def test_runs_active_rules_and_emits_findings_sorted(self) -> None:
        rules = [
            rule_row("bbb", version=1),
            rule_row("aaa", version=1, definition=numeric_def(severity="major")),
        ]
        findings = run_rules(rules, self._hitting_records())
        assert [f["rule_key"] for f in findings] == ["aaa", "bbb"]

    def test_player_disable_override_skips_a_rule(self) -> None:
        rules = [rule_row("r", version=1)]
        overrides = [override_row("r", "disable")]
        assert run_rules(rules, self._hitting_records(), overrides) == []

    def test_rule_that_does_not_fire_emits_nothing(self) -> None:
        rules = [rule_row("r", version=1, definition=numeric_def(min_n=50))]
        assert run_rules(rules, self._hitting_records()) == []

    def test_overrides_for_other_rules_are_ignored(self) -> None:
        rules = [rule_row("r", version=1)]
        overrides = [override_row("other", "disable")]
        findings = run_rules(rules, self._hitting_records(), overrides)
        assert [f["rule_key"] for f in findings] == ["r"]

    def test_precision_only_the_planted_fault_fires(self) -> None:
        # US-G2 IT precision: with a planted front-foot fault and no edges in the
        # sample, exactly the front-foot rule fires and the edges rule stays silent.
        rules = [
            rule_row("front_foot", version=1),
            rule_row(
                "edges",
                version=1,
                definition=categorical_def(metric="contact_quality", min_n=10),
            ),
        ]
        findings = run_rules(rules, self._hitting_records())
        assert [f["rule_key"] for f in findings] == ["front_foot"]
