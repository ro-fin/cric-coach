"""US-I7 leg-spin report body: Report-body-v1 + additive bowling blocks —
accuracy scorecard (US-I4 denominators), release scatter (US-I3), honest
variation agreement (US-I6), Warne/Saqlain learning modules (honestly marked
pending coach review) and the always-present workload view; SAF content lints
over every string."""

from datetime import date
from typing import Any

import pytest
from cricai_coaching import bowling_report
from cricai_coaching.bowling_analysis import BOWLING_FINDING_KINDS
from cricai_coaching.bowling_report import (
    ACCURACY_DENOMINATOR_NOTE,
    BOWLING_BODY_KEY,
    CHECKPOINT_STRENGTH_POSITIVE,
    LEARNING_MODULES,
    RELEASE_SCATTER_NOTE,
    TARGET_STRENGTH_POSITIVE,
    UNCLEAR_DETECTION,
    UNLABELED_VARIATION,
    VARIATION_HONESTY_NOTE,
    accuracy_scorecard,
    assemble_bowling_report_body,
    learning_modules,
    release_scatter,
    variation_agreement,
)
from cricai_coaching.content_lint import (
    BANNED_COACHING_PHRASES,
    BANNED_MEDICAL_PHRASES,
    BANNED_SPIN_CLAIMS,
    assert_kid_safe,
    find_banned_phrases,
)
from cricai_coaching.contracts import validate_report_body
from cricai_coaching.report import (
    HONESTY_CLEAN,
    STRENGTH_DEFAULT_POSITIVE,
    ReportSources,
    validate_claims_coverage,
)

PERIOD_START = date(2026, 7, 10)
PERIOD_END = date(2026, 7, 10)


def ball(ball_no: int, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "1.1",
        "ball_id": ball_no,
        "mode": "bowling",
        "confidence": {},
        "clips": {},
    }
    record.update(fields)
    return record


def probe_finding(
    kind: str,
    *,
    finding_id: str = "an-1",
    severity: str = "major",
    effect: float = -0.5,
    n: int = 12,
    condition: dict[str, Any] | None = None,
    evidence: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "agent": "bowling_analysis",
        "probe": kind,
        "kind": kind,
        "severity": severity,
        "metric": "release_height_sigma_cm",
        "condition": condition if condition is not None else {"mode": "bowling"},
        "n": n,
        "effect_size": effect,
        "confidence": 0.9,
        "ball_ids": list(range(1, n + 1)),
        "evidence": evidence
        if evidence is not None
        else {str(i): {"C5": f"clip-{i}"} for i in range(1, 4)},
        "text_data": {"summary": "s", "correction": "Fix the slot.", "drill": "Groove it."},
    }


class TestAccuracyScorecard:
    def test_scorecard_counts_only_confident_targets(self) -> None:
        records = [
            ball(1, target_hit=True, variation_intent="leg_break"),
            ball(2, target_hit=False, variation_intent="leg_break"),
            ball(3, target_hit=True, variation_intent="googly"),
            ball(4, target_hit=None, variation_intent="googly"),  # excluded, shown
            ball(5, target_hit=True),  # unlabeled variation
        ]
        card = accuracy_scorecard(records)
        assert card["overall"] == {"hits": 3, "n": 4, "pct": 75.0}
        assert card["counted"] == 4
        assert card["total"] == 5
        assert card["by_variation"] == [
            {"variation": "googly", "hits": 1, "n": 1, "pct": 100.0},
            {"variation": "leg_break", "hits": 1, "n": 2, "pct": 50.0},
            {"variation": UNLABELED_VARIATION, "hits": 1, "n": 1, "pct": 100.0},
        ]
        assert card["note"] == ACCURACY_DENOMINATOR_NOTE

    def test_empty_session_is_honest_nulls(self) -> None:
        card = accuracy_scorecard([ball(1, mode="batting")])
        assert card["overall"] == {"hits": 0, "n": 0, "pct": None}
        assert card["by_variation"] == []
        assert card["total"] == 0


class TestReleaseScatter:
    def test_stats_points_and_per_variation_clusters(self) -> None:
        records = [
            ball(1, release_height_cm=180.0, variation_intent="leg_break"),
            ball(2, release_height_cm=190.0, variation_intent="leg_break"),
            ball(3, release_height_cm=170.0, variation_intent="googly"),
            ball(4, release_height_cm=None),  # null never enters the scatter
        ]
        block = release_scatter(records)
        assert block["n"] == 3
        assert block["mean_cm"] == 180.0
        assert block["min_cm"] == 170.0
        assert block["max_cm"] == 190.0
        assert block["points"] == [
            {"ball_id": 1, "release_height_cm": 180.0, "variation_intent": "leg_break"},
            {"ball_id": 2, "release_height_cm": 190.0, "variation_intent": "leg_break"},
            {"ball_id": 3, "release_height_cm": 170.0, "variation_intent": "googly"},
        ]
        by_variation = {row["variation"]: row for row in block["by_variation"]}
        assert by_variation["leg_break"]["sigma_cm"] == 5.0
        # A 1-ball cluster reports a null sigma, never a fake perfect 0.0.
        assert by_variation["googly"]["sigma_cm"] is None
        assert block["note"] == RELEASE_SCATTER_NOTE

    def test_empty_scatter_is_all_nulls(self) -> None:
        block = release_scatter([])
        assert block["n"] == 0
        assert block["mean_cm"] is None
        assert block["sigma_cm"] is None
        assert block["min_cm"] is None
        assert block["max_cm"] is None
        assert block["points"] == []


class TestVariationAgreement:
    def test_matrix_and_agreement_are_honest(self) -> None:
        records = [
            ball(1, variation_intent="leg_break", variation_detected="leg_break"),
            ball(2, variation_intent="leg_break", variation_detected="googly"),
            ball(3, variation_intent="googly", variation_detected=None),  # unclear
            ball(4, variation_intent=None, variation_detected=None),  # unlabeled
        ]
        block = variation_agreement(records)
        assert block["labeled"] == 3
        assert block["compared"] == 2
        assert block["unclear"] == 1
        assert block["agreement_pct"] == 50.0
        assert block["matrix"] == [
            {"intent": "googly", "detected": UNCLEAR_DETECTION, "count": 1},
            {"intent": "leg_break", "detected": "googly", "count": 1},
            {"intent": "leg_break", "detected": "leg_break", "count": 1},
        ]
        assert block["note"] == VARIATION_HONESTY_NOTE

    def test_unknown_labels_show_in_matrix_but_never_compare(self) -> None:
        """US-I6: ``unknown`` is honest "unclear" — shown, never scored."""
        records = [
            ball(1, variation_intent="unknown", variation_detected="leg_break"),
            ball(2, variation_intent="leg_break", variation_detected="unknown"),
            ball(3, variation_intent="leg_break", variation_detected="leg_break"),
        ]
        block = variation_agreement(records)
        assert block["labeled"] == 3
        assert block["compared"] == 1  # same denominator rule as the probe
        assert block["unclear"] == 2
        assert block["agreement_pct"] == 100.0
        assert {(cell["intent"], cell["detected"]) for cell in block["matrix"]} == {
            ("unknown", "leg_break"),
            ("leg_break", "unknown"),
            ("leg_break", "leg_break"),
        }

    def test_no_detections_means_null_agreement_never_zero(self) -> None:
        records = [ball(1, variation_intent="leg_break", variation_detected=None)]
        block = variation_agreement(records)
        assert block["agreement_pct"] is None  # below-gate predictions never asserted
        assert block["compared"] == 0
        assert block["unclear"] == 1


class TestLearningModules:
    def test_modules_key_off_finding_kinds_with_player_examples(self) -> None:
        findings = [
            probe_finding("release_consistency", finding_id="an-1"),
            probe_finding("variation_agreement", finding_id="an-2", severity="minor"),
        ]
        modules = learning_modules(findings)
        assert [m["kind"] for m in modules] == ["release_consistency", "variation_agreement"]
        first = modules[0]
        assert first["legend"] == "Shane Warne"
        assert first["approved_by"] == "pending_coach_review"
        assert first["finding_id"] == "an-1"
        assert first["example_ball_ids"] == [1, 2, 3]
        assert first["examples"]["1"] == {"C5": "clip-1"}

    def test_one_module_per_kind_highest_ranked_wins(self) -> None:
        findings = [
            probe_finding("release_consistency", finding_id="an-minor", severity="minor", n=5),
            probe_finding("release_consistency", finding_id="an-major", severity="major", n=20),
        ]
        modules = learning_modules(findings)
        assert len(modules) == 1
        assert modules[0]["finding_id"] == "an-major"

    def test_non_bowling_kinds_get_no_module(self) -> None:
        rule_finding = probe_finding("release_consistency") | {"kind": "rule"}
        assert learning_modules([rule_finding]) == []

    def test_examples_limited_to_balls_with_evidence(self) -> None:
        finding = probe_finding("target_accuracy", evidence={"7": {"C5": "clip-7"}}, n=12)
        modules = learning_modules([finding])
        assert modules[0]["example_ball_ids"] == [7]
        assert modules[0]["examples"] == {"7": {"C5": "clip-7"}}

    def test_every_pinned_kind_has_a_module_and_vice_versa(self) -> None:
        assert set(LEARNING_MODULES) == set(BOWLING_FINDING_KINDS)


class TestAssembleBowlingReportBody:
    def test_body_is_v1_plus_bowling_section_and_validates(self) -> None:
        records = [ball(1, target_hit=True, release_height_cm=180.0)]
        workload = {"weighted_overs": 10.5, "ceiling_overs": 16.0, "violations": []}
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[probe_finding("release_consistency")],
            records=records,
            workload=workload,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        validate_report_body(body)  # contract #4 holds with the additive key
        assert validate_claims_coverage(body) == []  # US-G3 gate still green
        section = body[BOWLING_BODY_KEY]
        assert section["workload"] == workload
        assert section["accuracy_scorecard"]["overall"]["hits"] == 1
        assert section["release_scatter"]["n"] == 1
        assert section["learning_modules"][0]["kind"] == "release_consistency"
        assert body["main_correction"]["finding_id"] == "an-1"
        assert body["drill"]["text"] == "Groove it."

    def test_honesty_path_still_carries_bowling_section(self) -> None:
        """US-I7 AC: workload always shows — even a clean session ships the block."""
        workload = {
            "weighted_overs": 18.0,
            "ceiling_overs": 16.0,
            "violations": ["workload_ceiling"],
        }
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[],
            records=[],
            workload=workload,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        assert body["honesty_banner"] == HONESTY_CLEAN
        assert body["main_correction"] is None
        assert body[BOWLING_BODY_KEY]["workload"] == workload
        validate_report_body(body)

    def test_quality_banner_path_carries_bowling_section_too(self) -> None:
        sources = ReportSources(quality={"banner": "Too little usable data today."})
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[],
            sources=sources,
            records=[],
            workload=None,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        assert body["honesty_banner"] == "Too little usable data today."
        assert body[BOWLING_BODY_KEY]["workload"] is None  # honest null, never fabricated

    def test_bowling_strength_rewords_the_generic_positive(self) -> None:
        strength = probe_finding(
            "target_accuracy",
            severity="info",
            effect=0.4,
            condition={"mode": "bowling", "variation_intent": "leg_break"},
        )
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[strength],
            records=[],
            workload=None,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        assert body["positive"] == TARGET_STRENGTH_POSITIVE.format(variation="leg break")

    def test_checkpoint_strength_gets_its_own_wording(self) -> None:
        strength = probe_finding("action_checkpoint", severity="info", effect=0.3)
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[strength],
            records=[],
            workload=None,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        assert body["positive"] == CHECKPOINT_STRENGTH_POSITIVE

    def test_release_kind_strength_keeps_generic_wording(self) -> None:
        # Defensive: a positive-effect finding of a kind without special wording.
        strength = probe_finding("release_consistency", severity="info", effect=0.2)
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[strength],
            records=[],
            workload=None,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        assert body["positive"] == STRENGTH_DEFAULT_POSITIVE

    def test_authored_positive_matching_the_constant_is_left_alone(self) -> None:
        # Defensive: an authored "what went well" that happens to equal the
        # generic constant must not be rewritten when no strength exists.
        authored = probe_finding("positive", effect=-0.1)
        authored["kind"] = "positive"
        authored["text_data"] = {"positive": STRENGTH_DEFAULT_POSITIVE}
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[authored],
            records=[],
            workload=None,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        assert body["positive"] == STRENGTH_DEFAULT_POSITIVE

    def test_no_strengths_keeps_the_default_positive(self) -> None:
        body = assemble_bowling_report_body(
            kind="daily",
            findings=[probe_finding("release_consistency")],
            records=[],
            workload=None,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        assert "keep that going" not in body["positive"]


class TestContentSafety:
    def _module_strings(self) -> list[str]:
        texts = [
            value
            for name, value in vars(bowling_report).items()
            if isinstance(value, str) and name.isupper()
        ]
        for module in LEARNING_MODULES.values():
            texts.extend(module.values())
        return texts

    @pytest.mark.safety
    def test_lesson_and_block_text_passes_banned_claim_lint(self) -> None:
        """SAF (US-I5): no bowling-facing string claims rpm / revs / spin rate."""
        banned = BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES + BANNED_SPIN_CLAIMS
        texts = self._module_strings()
        assert texts, "no strings to lint"
        offenders = {
            text: [v.phrase for v in find_banned_phrases(text, banned)]
            for text in texts
            if find_banned_phrases(text, banned)
        }
        assert offenders == {}

    @pytest.mark.safety
    def test_lesson_text_is_kid_safe_and_digit_free(self) -> None:
        """SAF: age-appropriate, and digit-free so wording never needs claims."""
        for text in self._module_strings():
            assert_kid_safe(text)
            assert not any(char.isdigit() for char in text), text

    @pytest.mark.safety
    def test_no_module_claims_an_unrecorded_coach_signoff(self) -> None:
        """US-I7 honesty: ``approved_by`` never asserts a sign-off that never
        happened.

        The coach sign-off ledger (docs/coaching_metrics.md header) is blank,
        so every module must carry the honest ``pending_coach_review`` marker
        (pinned here as a literal, independent of the source constant). The
        ``"coach"`` value may only ever ship together with a recorded sign-off
        in that ledger — flipping it is a human act, not a code default.
        """
        for kind, module in LEARNING_MODULES.items():
            assert module["approved_by"] == "pending_coach_review", kind
            for key in ("legend", "title", "principle", "lesson", "drill"):
                assert module[key].strip(), (kind, key)
