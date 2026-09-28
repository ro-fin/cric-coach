"""US-J2 analysis agent: probes, gates, top-k discipline, determinism."""

from typing import Any

import pytest
from cricai_coaching.analysis_agent import (
    AGENT_NAME,
    CONTROL_METRIC,
    DEFAULT_ANALYSIS_CONFIG,
    FATIGUE_TEXT_TEMPLATE,
    ZONE_TEXT_TEMPLATE,
    AnalysisConfig,
    AnalysisError,
    finding_id,
    rank_findings,
    run_analysis,
    validate_finding,
)


def record(
    ball_id: int,
    *,
    line: str = "outside_off",
    length: str = "full",
    control: bool | None = True,
    confidence: dict[str, float] | None = None,
    clips: dict[str, str] | None = None,
    schema_version: str = "1.0",
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "schema_version": schema_version,
        "ball_id": ball_id,
        "session_id": "s-1",
        "mode": "batting",
        "line": line,
        "length": length,
        "control": control,
        "clips": clips if clips is not None else {"C1": f"clip-{ball_id}"},
    }
    if confidence is not None:
        rec["confidence"] = confidence
    return rec


def rule_finding(finding_key: str = "rf-1", severity: str = "major") -> dict[str, Any]:
    """A well-formed contract-#2 rule finding, as story g1's runner emits."""
    return {
        "finding_id": finding_key,
        "agent": AGENT_NAME,
        "rule_key": "front_foot_stride",
        "kind": "rule_hit",
        "severity": severity,
        "metric": "front_foot_direction_cm",
        "condition": {"line": "outside_off", "length": "full"},
        "n": 42,
        "effect_size": None,
        "confidence": 0.9,
        "ball_ids": [1, 2, 3],
        "evidence": {1: {"C1": "clip-1"}},
        "text_data": {"summary": "rule-authored text"},
    }


def planted_zone_records() -> list[dict[str, Any]]:
    """15 weak-cell balls (20% control) + 15 strong-cell balls (100%)."""
    weak = [record(i, line="outside_off", length="full", control=(i < 3)) for i in range(15)]
    strong = [record(100 + i, line="middle", length="good", control=True) for i in range(15)]
    return weak + strong


class TestZoneContrastProbe:
    def test_planted_effect_fires_weak_and_strong_cells(self) -> None:
        findings = run_analysis(planted_zone_records())
        assert [f["kind"] for f in findings] == ["zone_contrast", "zone_contrast"]
        weak, strong = findings
        # Weak cell: 20% vs 60% overall -> effect -0.4 -> major, ranked first.
        assert weak["severity"] == "major"
        assert weak["effect_size"] == pytest.approx(-0.4)
        assert weak["condition"] == {"line": "outside_off", "length": "full"}
        assert weak["n"] == 15
        assert weak["ball_ids"] == list(range(15))
        # Strong cell is a strength: info severity, positive effect.
        assert strong["severity"] == "info"
        assert strong["effect_size"] == pytest.approx(0.4)

    def test_finding_shape_and_prose_from_template_only(self) -> None:
        weak = run_analysis(planted_zone_records())[0]
        assert weak["agent"] == AGENT_NAME
        assert weak["probe"] == "zone_contrast"
        assert weak["metric"] == CONTROL_METRIC
        assert weak["confidence"] == 1.0
        assert weak["evidence"][0] == {"C1": "clip-0"}
        assert weak["text_data"] == {
            "summary": ZONE_TEXT_TEMPLATE.format(
                cell_pct=20, length="full", line="outside_off", overall_pct=60, n=15
            )
        }

    def test_pure_noise_yields_no_findings(self) -> None:
        records = [record(i, control=True) for i in range(20)]
        records += [record(100 + i, line="middle", length="good", control=True) for i in range(20)]
        assert run_analysis(records) == []

    def test_min_sample_gate_blocks_small_cells(self) -> None:
        # A 9-ball weak cell at 0% control would be a huge negative effect, but
        # it sits below the 10-ball min-n gate. The well-sampled cell that
        # remains tracks the overall rate closely (90/99 overall), so its own
        # effect stays under the floor and nothing fires — only the gate is
        # in play here. (99 balls stays below the fatigue probe's 100 floor.)
        records = [record(i, control=False) for i in range(9)]
        records += [record(100 + i, line="middle", length="good", control=True) for i in range(90)]
        assert run_analysis(records) == []

    def test_overall_below_min_sample_yields_nothing(self) -> None:
        assert run_analysis([record(i, control=False) for i in range(5)]) == []

    def test_effect_floor_blocks_small_contrasts(self) -> None:
        # Weak cell at 50% vs 75% overall: effect -0.25 under a 0.3 floor.
        records = [record(i, control=(i % 2 == 0)) for i in range(20)]
        records += [record(100 + i, line="middle", length="good", control=True) for i in range(20)]
        config = AnalysisConfig(effect_floor=0.3)
        assert run_analysis(records, config=config) == []

    def test_records_missing_zone_context_are_skipped(self) -> None:
        records = planted_zone_records()
        records.append({**record(300), "line": None})
        records.append({**record(301), "length": None})
        findings = run_analysis(records)
        assert all(f["condition"] != {"line": None, "length": None} for f in findings)

    def test_confidence_averages_per_ball_control_confidence(self) -> None:
        records = [record(i, control=(i < 3), confidence={"control": 0.5}) for i in range(15)]
        records += [record(100 + i, line="middle", length="good", control=True) for i in range(15)]
        weak = run_analysis(records)[0]
        assert weak["confidence"] == 0.5

    def test_balls_without_clips_stay_in_ball_ids_but_not_evidence(self) -> None:
        records = [record(i, control=(i < 3), clips={}) for i in range(15)]
        records += [record(100 + i, line="middle", length="good", control=True) for i in range(15)]
        weak = run_analysis(records)[0]
        assert weak["ball_ids"] == list(range(15))
        assert weak["evidence"] == {}


class TestFatigueContrastProbe:
    @staticmethod
    def fatigue_records(first_controls: int, last_controls: int) -> list[dict[str, Any]]:
        """100 same-zone balls: N controlled in the first 50, M in the last 50."""
        return [record(i, control=(i < first_controls)) for i in range(50)] + [
            record(50 + i, control=(i < last_controls)) for i in range(50)
        ]

    def test_control_drop_fires_major_fatigue_finding(self) -> None:
        findings = run_analysis(self.fatigue_records(45, 25))
        assert len(findings) == 1
        fatigue = findings[0]
        assert fatigue["kind"] == "fatigue_contrast"
        assert fatigue["severity"] == "major"  # -0.4 vs 2*0.15 floor
        assert fatigue["effect_size"] == pytest.approx(-0.4)
        assert fatigue["n"] == 100
        assert fatigue["condition"] == {"segment": "last_50_vs_first_50"}
        assert fatigue["ball_ids"] == list(range(100))
        assert fatigue["text_data"] == {
            "summary": FATIGUE_TEXT_TEMPLATE.format(last_pct=50, first_pct=90, segment=50)
        }

    def test_improvement_is_an_info_strength(self) -> None:
        findings = run_analysis(self.fatigue_records(25, 45))
        assert [f["severity"] for f in findings] == ["info"]
        assert findings[0]["effect_size"] == pytest.approx(0.4)

    def test_below_two_segments_of_balls_no_comparison(self) -> None:
        records = self.fatigue_records(45, 25)[:99]
        assert run_analysis(records) == []

    def test_small_drop_below_floor_is_not_a_finding(self) -> None:
        assert run_analysis(self.fatigue_records(45, 40)) == []

    def test_sparse_control_data_fails_the_per_segment_min_n_gate(self) -> None:
        records = self.fatigue_records(45, 25)
        for rec in records[:45]:  # only 5 scored balls remain in the first segment
            rec["control"] = None
        assert run_analysis(records) == []

    def test_all_null_first_segment_yields_nothing(self) -> None:
        records = self.fatigue_records(45, 25)
        for rec in records[:50]:
            rec["control"] = None
        assert run_analysis(records) == []


class TestContracts:
    def test_unknown_schema_major_rejected(self) -> None:
        with pytest.raises(AnalysisError, match="schema major"):
            run_analysis([record(1, schema_version="2.0")])

    def test_missing_schema_version_treated_as_v1(self) -> None:
        rec = record(1)
        del rec["schema_version"]
        assert run_analysis([rec]) == []

    def test_non_integer_ball_id_rejected(self) -> None:
        with pytest.raises(AnalysisError, match="ball_id"):
            run_analysis([record(1) | {"ball_id": "1"}])

    def test_non_boolean_control_rejected(self) -> None:
        with pytest.raises(AnalysisError, match="control"):
            run_analysis([record(1) | {"control": "yes"} for _ in range(12)])

    def test_valid_rule_finding_passes_through_unchanged(self) -> None:
        injected = rule_finding()
        findings = run_analysis([], rule_findings=[injected])
        assert findings == [injected]
        assert findings[0] is not injected  # a copy: caller data never aliased

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda f: f.pop("metric"),
            lambda f: f.pop("text_data"),
            lambda f: f.update(probe="also_probe"),  # both rule_key and probe
            lambda f: f.pop("rule_key"),  # neither rule_key nor probe
            lambda f: f.update(severity="catastrophic"),
        ],
    )
    def test_contract_invalid_rule_findings_raise(self, mutation: Any) -> None:
        bad = rule_finding()
        mutation(bad)
        with pytest.raises(AnalysisError):
            run_analysis([], rule_findings=[bad])

    def test_validate_finding_accepts_probe_findings(self) -> None:
        probe = run_analysis(planted_zone_records())[0]
        validate_finding(probe)  # must not raise


class TestRankingDiscipline:
    def test_top_k_caps_combined_rule_and_probe_findings(self) -> None:
        rules = [rule_finding(f"rf-{i}", severity="minor") for i in range(5)]
        findings = run_analysis(planted_zone_records(), rule_findings=rules)
        assert len(findings) == DEFAULT_ANALYSIS_CONFIG.top_k
        # The planted major zone weakness outranks every minor rule hit.
        assert findings[0]["kind"] == "zone_contrast"
        assert findings[0]["severity"] == "major"

    def test_ranking_orders_severity_then_effect_then_n(self) -> None:
        small = rule_finding("rf-small", severity="minor") | {"effect_size": -0.2, "n": 10}
        large = rule_finding("rf-large", severity="minor") | {"effect_size": -0.5, "n": 10}
        many = rule_finding("rf-many", severity="minor") | {"effect_size": -0.5, "n": 99}
        ranked = rank_findings([small, large, many], top_k=5)
        assert [f["finding_id"] for f in ranked] == ["rf-many", "rf-large", "rf-small"]

    def test_null_effect_ranks_below_any_measured_effect(self) -> None:
        measured = rule_finding("rf-measured") | {"effect_size": -0.01}
        unmeasured = rule_finding("rf-unmeasured")  # effect_size None
        ranked = rank_findings([unmeasured, measured], top_k=5)
        assert [f["finding_id"] for f in ranked] == ["rf-measured", "rf-unmeasured"]

    def test_ties_break_deterministically_by_finding_id(self) -> None:
        a = rule_finding("rf-a")
        b = rule_finding("rf-b")
        assert rank_findings([b, a], top_k=5) == rank_findings([a, b], top_k=5)


class TestDeterminism:
    def test_shuffled_input_produces_identical_output(self) -> None:
        records = planted_zone_records() + TestFatigueContrastProbe.fatigue_records(45, 25)
        # Re-id the fatigue records so ball ids do not collide.
        for offset, rec in enumerate(records[30:]):
            rec["ball_id"] = 1000 + offset
        assert run_analysis(records) == run_analysis(list(reversed(records)))

    def test_repeated_runs_are_byte_identical(self) -> None:
        records = planted_zone_records()
        assert run_analysis(records, seed=7) == run_analysis(records, seed=7)

    def test_seed_changes_ids_but_not_measurements(self) -> None:
        records = planted_zone_records()
        base, reseeded = run_analysis(records, seed=0), run_analysis(records, seed=1)
        assert [f["finding_id"] for f in base] != [f["finding_id"] for f in reseeded]
        assert [f["effect_size"] for f in base] == [f["effect_size"] for f in reseeded]

    def test_finding_id_is_pure_function_of_inputs(self) -> None:
        condition = {"line": "outside_off", "length": "full"}
        assert finding_id(0, "zone_contrast", CONTROL_METRIC, condition) == finding_id(
            0, "zone_contrast", CONTROL_METRIC, dict(condition)
        )
