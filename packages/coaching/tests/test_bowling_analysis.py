"""US-I7 bowling probes: release scatter, target contrast, variation agreement,
checkpoint trend — contract-#2 findings with the Phase-6 bowling kinds, gated,
deterministic, honest about direction (US-I2/I3/I4/I6)."""

from collections.abc import Sequence
from typing import Any

import pytest
from cricai_coaching import bowling_analysis
from cricai_coaching.analysis_agent import AnalysisError, validate_finding
from cricai_coaching.bowling_analysis import (
    AGREEMENT_METRIC,
    BOWLING_FINDING_KINDS,
    CHECKPOINT_METRIC,
    CONSUMED_BOWLING_FIELDS,
    RELEASE_METRIC,
    TARGET_METRIC,
    BowlingAnalysisConfig,
    run_bowling_analysis,
)
from cricai_coaching.content_lint import (
    BANNED_COACHING_PHRASES,
    BANNED_MEDICAL_PHRASES,
    BANNED_SPIN_CLAIMS,
    find_banned_phrases,
)
from cricai_data.ballrecord import BOWLING_FIELDS

#: Tight config so tests seed few balls; gates stay meaningfully exercised.
CFG = BowlingAnalysisConfig(min_sample=5, effect_floor=0.15, top_k=5, release_sigma_gate_cm=8.0)


def ball(ball_no: int, **fields: Any) -> dict[str, Any]:
    """A minimal bowling-mode BallRecord dict (contract #1 essentials)."""
    record: dict[str, Any] = {
        "schema_version": "1.1",
        "ball_id": ball_no,
        "mode": "bowling",
        "confidence": {},
        "clips": {"C5": f"clip-{ball_no}"},
    }
    record.update(fields)
    return record


def release_records(heights: list[float]) -> list[dict[str, Any]]:
    return [ball(i + 1, release_height_cm=h) for i, h in enumerate(heights)]


class TestRecordHandling:
    def test_unknown_major_schema_version_rejected(self) -> None:
        with pytest.raises(AnalysisError, match="schema major"):
            run_bowling_analysis([ball(1) | {"schema_version": "2.0"}], config=CFG)

    def test_missing_ball_id_rejected(self) -> None:
        record = ball(1)
        del record["ball_id"]
        with pytest.raises(AnalysisError, match="integer ball_id"):
            run_bowling_analysis([record], config=CFG)

    def test_batting_records_are_excluded_from_probes(self) -> None:
        # Same heights that would fire the release probe, but on batting records.
        records = [
            record | {"mode": "batting"} for record in release_records([100, 130, 160, 190, 220])
        ]
        assert run_bowling_analysis(records, config=CFG) == []

    def test_contract_invalid_rule_finding_rejected_not_dropped(self) -> None:
        with pytest.raises(AnalysisError):
            run_bowling_analysis([], rule_findings=[{"finding_id": "x"}], config=CFG)


class TestReleaseConsistencyProbe:
    def test_wandering_release_fires_negative_weakness(self) -> None:
        records = release_records([100, 130, 160, 190, 220])  # sigma ~ 42 cm
        findings = run_bowling_analysis(records, config=CFG)
        assert [f["kind"] for f in findings] == ["release_consistency"]
        finding = findings[0]
        assert finding["metric"] == RELEASE_METRIC
        assert finding["agent"] == bowling_analysis.AGENT_NAME
        assert finding["probe"] == "release_consistency"
        assert finding["severity"] == "major"  # far past the gate
        assert finding["effect_size"] < 0  # weakness, never a report strength
        assert finding["n"] == 5
        assert finding["ball_ids"] == [1, 2, 3, 4, 5]
        assert finding["evidence"][1] == {"C5": "clip-1"}
        assert finding["payload"]["value"] == pytest.approx(42.4, abs=0.1)
        assert finding["payload"]["threshold"] == CFG.release_sigma_gate_cm
        assert "correction" in finding["text_data"]
        validate_finding(finding)

    def test_repeatable_release_is_not_a_finding(self) -> None:
        records = release_records([180.0, 181.0, 182.0, 181.0, 180.0])  # sigma < 1 cm
        assert run_bowling_analysis(records, config=CFG) == []

    def test_min_sample_gate_blocks_thin_data(self) -> None:
        records = release_records([100, 200, 100, 200])  # wild scatter, only 4 balls
        assert run_bowling_analysis(records, config=CFG) == []

    def test_sigma_exactly_at_gate_fires_minor(self) -> None:
        # pstdev of [-8, +8] alternating around 180 is exactly 8.0 = the gate.
        heights = [172.0, 188.0, 172.0, 188.0, 172.0, 188.0]
        findings = run_bowling_analysis(release_records(heights), config=CFG)
        assert [f["severity"] for f in findings] == ["minor"]
        assert findings[0]["effect_size"] == 0.0

    def test_null_heights_do_not_enter_the_sample(self) -> None:
        records = [
            *release_records([100, 130, 160, 190]),
            ball(5, release_height_cm=None),
        ]
        assert run_bowling_analysis(records, config=CFG) == []  # only 4 scored


def target_records(
    hits_by_variation: dict[str, list[bool]], start: int = 1
) -> list[dict[str, Any]]:
    records = []
    ball_no = start
    for variation in sorted(hits_by_variation):
        for hit in hits_by_variation[variation]:
            records.append(ball(ball_no, target_hit=hit, variation_intent=variation))
            ball_no += 1
    return records


class TestTargetAccuracyProbe:
    def test_weak_variation_contrasts_negative(self) -> None:
        records = target_records(
            {
                "leg_break": [True] * 8 + [False] * 2,  # 80%
                "googly": [True] * 2 + [False] * 8,  # 20% vs ~50% overall
            }
        )
        findings = run_bowling_analysis(records, config=CFG)
        by_kind = {f["condition"].get("variation_intent"): f for f in findings}
        googly = by_kind["googly"]
        assert googly["kind"] == "target_accuracy"
        assert googly["metric"] == TARGET_METRIC
        assert googly["effect_size"] < 0
        assert googly["text_data"]["correction"]
        assert googly["payload"]["value"] == 20.0
        assert googly["payload"]["target"] == 50.0  # overall share = the goal target
        leg_break = by_kind["leg_break"]
        assert leg_break["severity"] == "info"  # positive delta = strength
        assert leg_break["effect_size"] > 0
        assert "correction" not in leg_break["text_data"]  # strengths carry no correction

    def test_unlabeled_deliveries_count_in_overall_but_form_no_group(self) -> None:
        records = [
            *target_records({"leg_break": [True] * 5}),
            *[ball(10 + i, target_hit=False, variation_intent=None) for i in range(5)],
        ]
        findings = run_bowling_analysis(records, config=CFG)
        assert [f["condition"]["variation_intent"] for f in findings] == ["leg_break"]
        assert findings[0]["payload"]["target"] == 50.0  # overall includes unlabeled

    def test_small_group_is_skipped_by_min_n(self) -> None:
        records = target_records({"leg_break": [True] * 8, "googly": [False] * 2})
        findings = run_bowling_analysis(records, config=CFG)
        assert [f["condition"]["variation_intent"] for f in findings] == ["leg_break"]

    def test_effect_floor_swallows_noise(self) -> None:
        records = target_records(
            {"leg_break": [True] * 5 + [False] * 5, "googly": [True] * 5 + [False] * 5}
        )
        assert run_bowling_analysis(records, config=CFG) == []

    def test_too_few_confident_targets_yield_nothing(self) -> None:
        records = target_records({"leg_break": [True, False, True, False]})
        assert run_bowling_analysis(records, config=CFG) == []


def agreement_records(pairs: Sequence[tuple[str | None, str | None]]) -> list[dict[str, Any]]:
    return [
        ball(i + 1, variation_intent=intent, variation_detected=detected)
        for i, (intent, detected) in enumerate(pairs)
    ]


class TestVariationAgreementProbe:
    def test_below_gate_agreement_fires(self) -> None:
        pairs: list[tuple[str | None, str | None]] = [
            ("leg_break", "leg_break"),
            ("leg_break", "leg_break"),
            ("googly", "leg_break"),
            ("googly", "top_spinner"),
            ("top_spinner", "leg_break"),
        ]  # 2/5 = 40% < 80% gate
        findings = run_bowling_analysis(agreement_records(pairs), config=CFG)
        assert [f["kind"] for f in findings] == ["variation_agreement"]
        finding = findings[0]
        assert finding["metric"] == AGREEMENT_METRIC
        assert finding["payload"] == {"value": 40.0, "threshold": 80.0, "matches": 2}
        assert finding["severity"] == "major"  # 40 points under the gate
        assert finding["effect_size"] == pytest.approx(-0.4)

    def test_at_gate_agreement_is_clean(self) -> None:
        pairs: list[tuple[str | None, str | None]] = [
            ("leg_break", "leg_break"),
            ("leg_break", "leg_break"),
            ("leg_break", "leg_break"),
            ("googly", "googly"),
            ("googly", "leg_break"),
        ]  # 4/5 = 80% == gate
        assert run_bowling_analysis(agreement_records(pairs), config=CFG) == []

    def test_unknown_and_unclear_never_enter_the_denominator(self) -> None:
        pairs: list[tuple[str | None, str | None]] = [
            ("unknown", "leg_break"),  # undeclared intent
            ("leg_break", "unknown"),  # below-gate detection
            ("leg_break", None),  # classifier has not run
            (None, None),
        ] * 3
        assert run_bowling_analysis(agreement_records(pairs), config=CFG) == []

    def test_just_below_gate_is_minor(self) -> None:
        pairs = [("leg_break", "leg_break")] * 7 + [("googly", "leg_break")] * 3
        findings = run_bowling_analysis(agreement_records(pairs), config=CFG)
        assert [f["severity"] for f in findings] == ["minor"]  # 70% vs 80% gate


def brace_records(states: Sequence[str | None]) -> list[dict[str, Any]]:
    return [ball(i + 1, brace_state=state) for i, state in enumerate(states)]


class TestActionCheckpointProbe:
    def test_fading_brace_fires_weakness(self) -> None:
        states = ["braced"] * 5 + ["collapsed"] * 3 + ["bent"] * 2
        findings = run_bowling_analysis(brace_records(states), config=CFG)
        assert [f["kind"] for f in findings] == ["action_checkpoint"]
        finding = findings[0]
        assert finding["metric"] == CHECKPOINT_METRIC
        assert finding["condition"]["checkpoint"] == "front_leg_brace"
        assert finding["condition"]["segment"] == "last_5_vs_first_5"
        assert finding["effect_size"] == -1.0
        assert finding["severity"] == "major"
        assert finding["payload"] == {"first_pct": 100.0, "last_pct": 0.0}
        assert finding["n"] == 10

    def test_improving_brace_is_a_strength(self) -> None:
        states = ["bent"] * 5 + ["braced"] * 5
        findings = run_bowling_analysis(brace_records(states), config=CFG)
        assert [f["severity"] for f in findings] == ["info"]
        assert findings[0]["effect_size"] == 1.0
        assert "correction" not in findings[0]["text_data"]

    def test_odd_middle_ball_joins_neither_half(self) -> None:
        # 11 evaluable balls: halves of 5, the 6th ball is excluded.
        states = ["braced"] * 5 + ["collapsed"] + ["collapsed"] * 5
        findings = run_bowling_analysis(brace_records(states), config=CFG)
        assert findings[0]["n"] == 10
        assert findings[0]["ball_ids"] == [1, 2, 3, 4, 5, 7, 8, 9, 10, 11]

    def test_steady_brace_below_floor_is_clean(self) -> None:
        states = ["braced"] * 10
        assert run_bowling_analysis(brace_records(states), config=CFG) == []

    def test_null_states_gate_the_sample(self) -> None:
        states: list[str | None] = ["braced"] * 5 + [None] * 5 + ["collapsed"] * 4
        # Only 9 evaluable -> halves of 4 < min_sample.
        assert run_bowling_analysis(brace_records(states), config=CFG) == []


class TestMergeAndRanking:
    def rule_finding(self, finding_id: str, severity: str = "major") -> dict[str, Any]:
        return {
            "finding_id": finding_id,
            "agent": "rules",
            "rule_key": "bowling_short_of_target_length",
            "kind": "rule",
            "severity": severity,
            "metric": "length",
            "condition": {"mode": ["bowling"]},
            "n": 12,
            "effect_size": 0.6,
            "confidence": 0.9,
            "ball_ids": [1, 2, 3],
            "evidence": {},
            "text_data": {"finding": "short again"},
        }

    def test_rule_findings_merge_and_rank_with_probes(self) -> None:
        records = release_records([100, 130, 160, 190, 220])
        findings = run_bowling_analysis(
            records, rule_findings=[self.rule_finding("ru-1")], config=CFG
        )
        assert {f["finding_id"] for f in findings} >= {"ru-1"}
        assert len(findings) == 2
        for finding in findings:
            validate_finding(finding)

    def test_top_k_cuts_the_merged_list(self) -> None:
        records = release_records([100, 130, 160, 190, 220])
        cfg = BowlingAnalysisConfig(
            min_sample=5, effect_floor=0.15, top_k=1, release_sigma_gate_cm=8.0
        )
        findings = run_bowling_analysis(
            records, rule_findings=[self.rule_finding("ru-1")], config=cfg
        )
        assert len(findings) == 1

    def test_identical_inputs_produce_byte_identical_output(self) -> None:
        records = [
            *release_records([100, 130, 160, 190, 220]),
            *target_records(
                {"leg_break": [True] * 8 + [False] * 2, "googly": [False] * 8 + [True] * 2},
                start=100,
            ),
        ]
        assert run_bowling_analysis(records, config=CFG) == run_bowling_analysis(
            list(reversed(records)), config=CFG
        )


class TestContractsAndSafety:
    def test_consumed_fields_are_published_by_ballrecord_v1_1(self) -> None:
        """US-I7 consumes only fields US-I2/I3/I4/I6 actually produce."""
        assert set(CONSUMED_BOWLING_FIELDS) <= set(BOWLING_FIELDS)

    def test_bowling_finding_kinds_are_the_pinned_four(self) -> None:
        assert BOWLING_FINDING_KINDS == (
            "release_consistency",
            "target_accuracy",
            "variation_agreement",
            "action_checkpoint",
        )

    @pytest.mark.safety
    def test_all_probe_strings_pass_banned_claim_and_kid_lints(self) -> None:
        """SAF: geometric language only — no rotation-rate claims, no shaming."""
        banned = BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES + BANNED_SPIN_CLAIMS
        strings = [
            value
            for name, value in vars(bowling_analysis).items()
            if isinstance(value, str) and name.isupper()
        ]
        assert strings, "no module strings to lint"
        offenders = {
            text: [v.phrase for v in find_banned_phrases(text, banned)]
            for text in strings
            if find_banned_phrases(text, banned)
        }
        assert offenders == {}
