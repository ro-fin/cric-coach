"""US-G3 acceptance: ranking, one-correction template, honesty path, claims,
the ReportWriter seam and the printable HTML renderer."""

from typing import Any

import pytest
from cricai_coaching.content_lint import assert_kid_safe
from cricai_coaching.fatigue import FATIGUE_SUGGESTION
from cricai_coaching.report import (
    BATTING_SPLIT_NOTE,
    DEFAULT_DRILL_TEXT,
    DEFAULT_POSITIVE,
    HONESTY_CLEAN,
    MAX_SECONDARY,
    STRENGTH_DEFAULT_POSITIVE,
    ReportSources,
    ReportWriter,
    RuleBasedWriter,
    _cell_text,
    assemble_report_body,
    batting_split_block,
    extract_numbers,
    finding_score,
    format_number,
    is_strength,
    rank_findings,
    render_report_html,
    validate_claims_coverage,
    wording_texts,
)
from cricai_coaching.workload import IntentReconciliation, SplitReconciliation


def _strength(
    fid: str, *, effect: float = 0.3, condition: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    """A probe strength finding: no rule_key, positive effect, severity info."""
    finding = _finding(
        fid,
        severity="info",
        kind="zone_contrast",
        condition=condition if condition is not None else {},
        **kw,
    )
    del finding["rule_key"]  # probes carry a probe name, never a rule_key (contract #2)
    finding["effect_size"] = effect
    return finding


def _evidence(fid: str, clips: int) -> dict[str, dict[str, str]]:
    """`clips` distinct clip links spread over balls, two cameras per ball."""
    evidence: dict[str, dict[str, str]] = {}
    for i in range(clips):
        ball = str(i // 2 + 1)
        camera = f"C{i % 2 + 1}"
        evidence.setdefault(ball, {})[camera] = f"clip-{fid}-{i}"
    return evidence


def _finding(
    fid: str,
    *,
    severity: str = "major",
    n: int = 20,
    metric: str = "control_pct",
    kind: str = "technique",
    clips: int = 2,
    text_data: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    condition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": fid,
        "agent": "rules",
        "rule_key": "front_foot_stride",
        "kind": kind,
        "severity": severity,
        "metric": metric,
        "condition": condition if condition is not None else {"line": "outside_off"},
        "n": n,
        "effect_size": None,
        "confidence": 0.9,
        "ball_ids": list(range(1, n + 1)),
        "evidence": _evidence(fid, clips),
        "payload": payload if payload is not None else {},
        "text_data": text_data
        if text_data is not None
        else {"correction": "Move your front foot to the ball."},
    }


def _assemble(findings: list[dict[str, Any]], **sources: Any) -> dict[str, Any]:
    return assemble_report_body(
        kind="daily",
        period_start="2026-07-09",
        period_end="2026-07-09",
        findings=findings,
        sources=ReportSources(**sources) if sources else None,
    )


# --- ranking: severity x frequency x trend (US-G3 UT) -----------------------


def test_severity_dominates_at_equal_frequency() -> None:
    ranked = rank_findings(
        [
            _finding("info", severity="info"),
            _finding("major", severity="major"),
            _finding("minor", severity="minor"),
        ]
    )
    assert [f["finding_id"] for f in ranked] == ["major", "minor", "info"]


def test_frequency_breaks_equal_severity() -> None:
    ranked = rank_findings([_finding("small", n=5), _finding("big", n=40)])
    assert [f["finding_id"] for f in ranked] == ["big", "small"]


def test_regressing_trend_outranks_an_equal_score() -> None:
    a = _finding("a", severity="minor", n=20, metric="m1")  # 2 * 20 = 40
    b = _finding("b", severity="major", n=10, metric="m2")  # 4 * 10 = 40
    assert [f["finding_id"] for f in rank_findings([a, b])] == ["a", "b"]  # id tie-break
    ranked = rank_findings([a, b], trends={"m2": "regressing"})
    assert [f["finding_id"] for f in ranked] == ["b", "a"]


def test_improving_trend_deprioritizes() -> None:
    a = _finding("a", severity="minor", n=20, metric="m1")
    b = _finding("b", severity="minor", n=18, metric="m2")
    ranked = rank_findings([a, b], trends={"m1": "improving"})
    assert [f["finding_id"] for f in ranked] == ["b", "a"]


def test_score_floors_frequency_at_one_ball() -> None:
    assert finding_score(_finding("f", severity="info", n=0)) == 1.0


# --- report body assembly (contract #4) -------------------------------------


def test_body_has_exactly_the_pinned_keys() -> None:
    body = _assemble([_finding("f1")])
    assert set(body) == {
        "kind",
        "period",
        "main_correction",
        "drill",
        "goal",
        "secondary",
        "positive",
        "safety",
        "honesty_banner",
        "coverage_note",
        "claims",
        "fatigue_note",
    }
    assert body["kind"] == "daily"
    assert body["period"] == {"start": "2026-07-09", "end": "2026-07-09"}


def test_exactly_one_correction_one_drill_one_goal() -> None:
    body = _assemble([_finding("f1"), _finding("f2", severity="minor")])
    assert body["main_correction"]["finding_id"] == "f1"
    assert "Move your front foot to the ball." in body["main_correction"]["text"]
    assert "Seen on 20 balls." in body["main_correction"]["text"]
    assert body["main_correction"]["evidence"] == _evidence("f1", 2)
    assert body["drill"] is not None
    assert body["goal"] is not None
    assert body["honesty_banner"] is None


def test_secondary_capped_at_three_collapsible_observations() -> None:
    findings = [_finding(f"f{i}", n=30 - i) for i in range(6)]
    body = _assemble(findings)
    assert len(body["secondary"]) == MAX_SECONDARY
    assert [item["finding_id"] for item in body["secondary"]] == ["f1", "f2", "f3"]
    assert all("balls" in item["text"] for item in body["secondary"])


def test_measured_value_lands_in_text_and_claims() -> None:
    body = _assemble([_finding("f1", payload={"value": 57.5})])
    assert "measured 57.5" in body["main_correction"]["text"]
    assert {"value": 57.5, "metric": "control_pct", "recompute_key": "finding:f1:value"} in body[
        "claims"
    ]


def test_goal_prefers_target_then_threshold_then_none() -> None:
    with_target = _assemble([_finding("f1", payload={"target": 70, "threshold": 60})])
    assert with_target["goal"] == {
        "metric": "control_pct",
        "target": 70.0,
        "condition": {"line": "outside_off"},
    }
    assert any(c["recompute_key"] == "finding:f1:target" for c in with_target["claims"])

    with_threshold = _assemble([_finding("f1", payload={"threshold": 60})])
    assert with_threshold["goal"]["target"] == 60.0
    assert any(c["recompute_key"] == "finding:f1:threshold" for c in with_threshold["claims"])

    without = _assemble([_finding("f1")])
    assert without["goal"]["target"] is None


def test_every_wording_number_is_claimed() -> None:
    body = _assemble(
        [
            _finding("f1", payload={"value": 57.0, "target": 70}),
            _finding("f2", severity="minor", n=12),
        ]
    )
    assert validate_claims_coverage(body) == []
    ball_claims = [c for c in body["claims"] if c["metric"] == "ball_count"]
    assert {c["recompute_key"] for c in ball_claims} == {"finding:f1:n", "finding:f2:n"}


# --- drill resolution --------------------------------------------------------


def test_injected_drill_resolver_wins_and_claims_its_ball_count() -> None:
    def drill_for(finding: Any) -> dict[str, Any]:
        return {
            "drill_id": "d-1",
            "text": "Cover-drive and leave block.",
            "machine_settings": {"speed_kph": 95},
            "success_metric": "control_pct",
            "ball_count": 80,
        }

    body = _assemble([_finding("f1")], drill_for=drill_for)
    assert body["drill"]["drill_id"] == "d-1"
    assert "Do 80 balls." in body["drill"]["text"]
    assert body["drill"]["machine_settings"] == {"speed_kph": 95}
    assert {"value": 80, "metric": "drill_balls", "recompute_key": "drill:d-1:ball_count"} in body[
        "claims"
    ]
    assert validate_claims_coverage(body) == []


def test_resolver_without_ball_count_or_metric_uses_finding_metric() -> None:
    body = _assemble(
        [_finding("f1")],
        drill_for=lambda _f: {"drill_id": "d-2", "text": "Shadow the stride."},
    )
    assert body["drill"]["success_metric"] == "control_pct"
    assert body["drill"]["machine_settings"] == {}
    assert "Do " not in body["drill"]["text"]


def test_resolver_returning_none_falls_back_to_rule_text() -> None:
    finding = _finding(
        "f1", text_data={"correction": "Step to the ball.", "drill": "Front-foot ladder drill."}
    )
    body = _assemble([finding], drill_for=lambda _f: None)
    assert body["drill"] == {
        "drill_id": None,
        "text": "Front-foot ladder drill.",
        "machine_settings": {},
        "success_metric": "control_pct",
    }


def test_no_drill_source_uses_deterministic_default() -> None:
    body = _assemble([_finding("f1")])
    assert body["drill"]["text"] == DEFAULT_DRILL_TEXT


# --- positive ("what went well") ---------------------------------------------


def test_positive_finding_supplies_the_positive_line() -> None:
    positive = _finding(
        "p1",
        kind="positive",
        severity="info",
        text_data={"positive": "Your leaves outside off were spot on."},
    )
    body = _assemble([_finding("f1"), positive])
    assert body["positive"] == "Your leaves outside off were spot on."
    assert body["main_correction"]["finding_id"] == "f1"  # positives never head corrections


def test_positive_finding_without_authored_text_uses_its_correction_text() -> None:
    positive = _finding("p1", kind="positive", text_data={"correction": "Clean hands all day."})
    assert _assemble([positive, _finding("f1")])["positive"] == "Clean hands all day."


def test_default_positive_when_no_positive_finding() -> None:
    assert _assemble([_finding("f1")])["positive"] == DEFAULT_POSITIVE


def test_unauthored_correction_text_names_the_metric() -> None:
    body = _assemble([_finding("f1", text_data={})])
    assert "control_pct" in body["main_correction"]["text"]


# --- honesty path (US-G3 SAF: never invents an issue) ------------------------


@pytest.mark.safety
def test_no_findings_ships_the_clean_session_banner() -> None:
    body = _assemble([])
    assert body["honesty_banner"] == HONESTY_CLEAN
    assert body["main_correction"] is None
    assert body["drill"] is None
    assert body["goal"] is None
    assert body["secondary"] == []
    assert body["claims"] == []
    assert body["positive"] == DEFAULT_POSITIVE


@pytest.mark.safety
def test_findings_below_the_evidence_bar_take_the_honesty_path() -> None:
    body = _assemble([_finding("f1", clips=1)])
    assert body["honesty_banner"] == HONESTY_CLEAN
    assert body["main_correction"] is None


@pytest.mark.safety
def test_only_positive_findings_is_still_a_clean_session() -> None:
    body = _assemble([_finding("p1", kind="positive")])
    assert body["honesty_banner"] == HONESTY_CLEAN
    assert body["main_correction"] is None


@pytest.mark.safety
def test_thin_data_quality_banner_replaces_findings() -> None:
    quality = {"components": {"pose_coverage": 0.2}, "composite": 0.3, "banner": "Data was thin."}
    body = _assemble([_finding("f1")], quality=quality)
    assert body["honesty_banner"] == "Data was thin."
    assert body["main_correction"] is None
    assert body["claims"] == []


def test_quality_without_banner_reports_normally() -> None:
    quality = {"components": {}, "composite": 0.9, "banner": None}
    body = _assemble([_finding("f1")], quality=quality)
    assert body["honesty_banner"] is None
    assert body["main_correction"] is not None


@pytest.mark.safety
def test_default_copy_passes_the_content_lint() -> None:
    for text in (DEFAULT_POSITIVE, DEFAULT_DRILL_TEXT, HONESTY_CLEAN):
        assert_kid_safe(text)


# --- safety verdict passthrough (contract #3) --------------------------------


def test_safety_verdict_is_copied_verbatim() -> None:
    verdict = {
        "active": True,
        "codes": ["workload_ceiling"],
        "text": "Stop bowling.",
        "sha256": "x",
    }
    body = _assemble([_finding("f1")], safety=verdict)
    assert body["safety"] == verdict
    verdict["text"] = "mutated later"
    assert body["safety"]["text"] == "Stop bowling."  # a copy, not a reference


def test_safety_travels_on_the_honesty_path_too() -> None:
    verdict = {"active": True, "codes": ["pain_flag"], "text": "No bowling today.", "sha256": "y"}
    assert _assemble([], safety=verdict)["safety"] == verdict


# --- numbers, claims coverage -------------------------------------------------


def test_format_number_is_canonical() -> None:
    assert format_number(57.0) == "57"
    assert format_number(0.5) == "0.5"
    assert format_number(81.25) == "81.25"


def test_extract_numbers_normalizes_literals() -> None:
    assert extract_numbers("control 57% on 42.0 balls at -3.5 cm") == ["57", "42", "-3.5"]
    assert extract_numbers("no numbers here") == []


def test_uncovered_numbers_are_reported() -> None:
    body = _assemble([_finding("f1")])
    body["positive"] = "You hit 9 sixes!"
    assert validate_claims_coverage(body) == ["9"]


def test_wording_texts_skips_safety_and_banner() -> None:
    verdict = {"active": True, "codes": [], "text": "Limit is 16 overs.", "sha256": "z"}
    body = _assemble([], safety=verdict)
    body["honesty_banner"] = "Only 3 balls tracked."
    assert validate_claims_coverage(body) == []  # verbatim inserts are exempt
    texts = wording_texts(body)
    assert "Limit is 16 overs." not in texts
    assert "Only 3 balls tracked." not in texts


def test_wording_texts_covers_every_wording_field() -> None:
    body = _assemble([_finding("f1"), _finding("f2", severity="minor")])
    texts = wording_texts(body)
    assert body["main_correction"]["text"] in texts
    assert body["drill"]["text"] in texts
    assert body["secondary"][0]["text"] in texts
    assert body["positive"] in texts


def test_wording_texts_handles_a_positive_free_body() -> None:
    assert wording_texts({"secondary": []}) == []


# --- ReportWriter seam ---------------------------------------------------------


def test_rule_based_writer_satisfies_the_protocol_and_is_pure() -> None:
    writer: ReportWriter = RuleBasedWriter()
    body = _assemble([_finding("f1")])
    context: dict[str, Any] = {
        "findings": [],
        "history": {},
        "rules": [],
        "tone": "encouraging",
        "safety": None,
    }
    worded = writer.write(body, context)
    assert worded == body
    worded["positive"] = "changed"
    assert body["positive"] != "changed"  # deep copy: original untouched


def test_rule_based_writer_is_deterministic() -> None:
    body = _assemble([_finding("f1")])
    writer = RuleBasedWriter()
    assert writer.write(body, {}) == writer.write(body, {})


# --- printable HTML renderer ----------------------------------------------------


def test_html_renders_all_sections_and_escapes() -> None:
    verdict = {"active": True, "codes": ["workload_ceiling"], "text": "Stop & rest.", "sha256": "x"}
    body = _assemble(
        [
            _finding("f1", text_data={"correction": "Keep your head <over> the ball."}),
            _finding("f2", severity="minor"),
        ],
        safety=verdict,
        drill_for=lambda _f: {"drill_id": "d-1", "text": "Leave block.", "ball_count": 80},
    )
    html = render_report_html(body)
    assert "Keep your head &lt;over&gt; the ball." in html
    assert "<over>" not in html
    assert "Stop &amp; rest." in html
    assert "Safety first" in html
    assert "<details" in html
    assert "Move your front foot to the ball. (20 balls.)" in html
    assert "clip-f1-0" in html  # evidence links rendered
    assert "Do 80 balls." in html
    assert "What went well" in html


def test_html_goal_shows_target_or_coach_set() -> None:
    with_target = _assemble([_finding("f1", payload={"target": 70})])
    assert "reach 70 next session" in render_report_html(with_target)
    without = _assemble([_finding("f1")])
    assert "coach-set" in render_report_html(without)


def test_html_honesty_body_has_banner_and_no_correction_sections() -> None:
    html = render_report_html(_assemble([]))
    assert HONESTY_CLEAN in html
    assert "Main correction" not in html
    assert "Tomorrow&#x27;s drill" not in html and "Tomorrow's drill" not in html
    assert "<details" not in html


def test_html_skips_inactive_safety_and_empty_evidence() -> None:
    verdict = {"active": False, "codes": [], "text": "", "sha256": ""}
    body = _assemble([_finding("f1")], safety=verdict)
    body["main_correction"]["evidence"] = {}
    html = render_report_html(body)
    assert "Safety first" not in html
    assert '<ul class="evidence">' not in html


def test_html_is_a_pure_function_of_the_body() -> None:
    body = _assemble([_finding("f1")])
    assert render_report_html(body) == render_report_html(body)


# --- strength probes never head corrections (US-G3 SAF, finding [1]) ---------


def test_is_strength_only_for_positive_effect_probes() -> None:
    assert is_strength(_strength("s1", effect=0.3)) is True
    assert is_strength(_strength("s1", effect=-0.3)) is False  # a weakness, not a strength
    assert is_strength(_finding("f1")) is False  # rule finding: effect is a hit share


@pytest.mark.safety
def test_strength_only_session_takes_the_honesty_path_and_names_the_strength() -> None:
    body = _assemble([_strength("s1", condition={"line": "outside_off", "length": "good"})])
    assert body["main_correction"] is None  # a strength never invents a correction (US-G3)
    assert body["honesty_banner"] == HONESTY_CLEAN
    assert body["positive"] == "Great control on good/outside_off balls - keep that going."


@pytest.mark.safety
def test_strength_never_outranks_a_real_correction_and_routes_to_positive() -> None:
    correction = _finding("f1", severity="major")
    strength = _strength("s1", effect=0.4)  # no line/length -> generic strength positive
    body = _assemble([strength, correction])
    assert body["main_correction"]["finding_id"] == "f1"
    assert all(item["finding_id"] != "s1" for item in body["secondary"])
    assert body["positive"] == STRENGTH_DEFAULT_POSITIVE


def test_positive_kind_still_wins_over_a_strength_probe() -> None:
    positive = _finding(
        "p1", kind="positive", severity="info", text_data={"positive": "Loved the leaves."}
    )
    body = _assemble([positive, _strength("s1")])
    assert body["positive"] == "Loved the leaves."


# --- safety banner renders first (US-H5, finding [31]) -----------------------


@pytest.mark.safety
def test_active_safety_renders_before_the_honesty_banner() -> None:
    """A safety warning must precede a reassuring 'clean session' banner."""
    verdict = {"active": True, "codes": ["pain_flag"], "text": "No bowling today.", "sha256": "x"}
    html = render_report_html(_assemble([], safety=verdict))  # clean session AND active safety
    assert "Safety first" in html and "honesty-banner" in html
    assert html.index("Safety first") < html.index("honesty-banner")


@pytest.mark.safety
def test_active_safety_renders_before_the_main_correction() -> None:
    verdict = {"active": True, "codes": ["pain_flag"], "text": "No bowling today.", "sha256": "x"}
    html = render_report_html(_assemble([_finding("f1")], safety=verdict))
    assert html.index("Safety first") < html.index("Main correction")


# --- US-H3 fatigue note in the report body (finding [53]) --------------------


def _fatigue_note() -> dict[str, Any]:
    return {
        "kind": "fatigue",
        "window": 10,
        "baseline_n": 10,
        "window_n": 10,
        "control_baseline_pct": 90.0,
        "control_window_pct": 60.0,
        "control_drop_points": 30.0,
        "degrading_signals": [
            {"metric": "head_stability_score", "baseline_mean": 0.8, "window_mean": 0.65},
            {"metric": "front_foot_direction_cm", "baseline_mean": 20.0, "window_mean": 14.0},
        ],
        "suggestion": FATIGUE_SUGGESTION,
    }


def test_fatigue_note_is_rendered_and_stays_claims_clean() -> None:
    body = _assemble([_finding("f1")], fatigue=_fatigue_note())
    note = body["fatigue_note"]
    assert note["text"] == FATIGUE_SUGGESTION  # number-free suggestion: no claims needed
    assert note["window"] == 10
    assert note["control_drop_points"] == 30.0
    assert set(note["degrading_metrics"]) == {"head_stability_score", "front_foot_direction_cm"}
    assert validate_claims_coverage(body) == []
    assert note["text"] in wording_texts(body)


def test_fatigue_note_survives_the_clean_session_path() -> None:
    body = _assemble([], fatigue=_fatigue_note())
    assert body["honesty_banner"] == HONESTY_CLEAN
    assert body["fatigue_note"] is not None  # fatigue is honest info even with no correction


@pytest.mark.safety
def test_thin_data_suppresses_the_fatigue_note() -> None:
    quality = {"components": {}, "composite": 0.3, "banner": "Data was thin."}
    body = _assemble([_finding("f1")], fatigue=_fatigue_note(), quality=quality)
    assert body["honesty_banner"] == "Data was thin."
    assert body["fatigue_note"] is None  # untrustworthy data: no fatigue claim either


def test_no_fatigue_source_leaves_the_note_none() -> None:
    assert _assemble([_finding("f1")])["fatigue_note"] is None


def test_fatigue_note_renders_its_own_html_section() -> None:
    html = render_report_html(_assemble([_finding("f1")], fatigue=_fatigue_note()))
    assert "Fatigue check" in html
    assert FATIGUE_SUGGESTION in html


# --- day-scoped coverage note (multi-session degraded-day fix) ---------------

COVERAGE_NOTE = "One of today's sessions could not be fully analysed; corrections follow."


def test_coverage_note_ships_alongside_the_corrections() -> None:
    """A coverage note never suppresses findings: the day's corrections still ship."""
    body = _assemble([_finding("f1")], coverage_note=COVERAGE_NOTE)
    assert body["coverage_note"] == COVERAGE_NOTE
    assert body["main_correction"] is not None  # findings are NOT dropped
    assert body["honesty_banner"] is None


def test_coverage_note_defaults_to_none() -> None:
    assert _assemble([_finding("f1")])["coverage_note"] is None


def test_coverage_note_joins_wording_texts_and_stays_claims_clean() -> None:
    body = _assemble([_finding("f1")], coverage_note=COVERAGE_NOTE)
    assert COVERAGE_NOTE in wording_texts(body)
    assert validate_claims_coverage(body) == []  # the shipped note is number-free


def test_coverage_note_renders_after_safety_and_above_the_main_correction() -> None:
    verdict = {"active": True, "codes": ["pain_flag"], "text": "No bowling today.", "sha256": "x"}
    html = render_report_html(
        _assemble([_finding("f1")], safety=verdict, coverage_note=COVERAGE_NOTE)
    )
    assert "coverage-note" in html
    assert html.index("Safety first") < html.index("coverage-note") < html.index("Main correction")


# --- US-I7/K5 bowling section on the printable surface (finding [46/55]) -----


def _bowling_block(*, workload: dict[str, Any] | None = None) -> dict[str, Any]:
    """A representative body['bowling'] section (the bowling_report shape)."""
    return {
        "accuracy_scorecard": {
            "overall": {"hits": 7, "n": 10, "pct": 70.0},
            "by_variation": [
                {"variation": "googly", "hits": 2, "n": 4, "pct": 50.0},
                {"variation": "leg_break", "hits": 5, "n": 6, "pct": 83.3},
            ],
            "counted": 10,
            "total": 12,
            "note": "Accuracy is counted only over confident target calls.",
        },
        "release_scatter": {
            "n": 10,
            "mean_cm": 201.5,
            "sigma_cm": 3.2,
            "min_cm": 196.0,
            "max_cm": 207.0,
            "points": [{"ball_id": 1, "release_height_cm": 200.0, "variation_intent": "leg_break"}],
            "by_variation": [{"variation": "leg_break", "n": 6, "mean_cm": 200.0, "sigma_cm": 2.0}],
            "note": "Scatter uses release height only for now.",
        },
        "variation_agreement": {
            "labeled": 10,
            "compared": 8,
            "unclear": 2,
            "agreement_pct": 75.0,
            "matrix": [
                {"intent": "leg_break", "detected": "leg_break", "count": 5},
                {"intent": "leg_break", "detected": "unclear", "count": 1},
            ],
            "note": "Declared intent is the ground truth.",
        },
        "learning_modules": [
            {
                "kind": "target_accuracy",
                "legend": "Shane Warne",
                "title": "Land it on the coin",
                "principle": "Target discipline first, magic second.",
                "lesson": "Look at where the ball pitched.",
                "drill": "Bowl a full block at a marker.",
                "approved_by": "pending_coach_review",
                "finding_id": "f-1",
                "example_ball_ids": [1],
                "examples": {},
            }
        ],
        "workload": workload
        if workload is not None
        else {
            "window": {"start": "2026-07-06", "end": "2026-07-10"},
            "weighted_overs": 12.5,
            "ceiling_overs": 20.0,
            "remaining_balls": 45,
            "violations": [],
        },
    }


def _bowling_body(**kw: Any) -> dict[str, Any]:
    body = _assemble([])
    body["bowling"] = _bowling_block(**kw)
    return body


def test_bowling_section_reaches_the_printable_surface() -> None:
    """US-I7 finding [46/55]: every bowling block ships on the HTML/PDF path."""
    html = render_report_html(_bowling_body())
    for token in (
        "Bowling workload",
        "Accuracy scorecard",
        "Release scatter",
        "Variation agreement",
        "Learning modules",
    ):
        assert token in html, token


def test_workload_vs_ceiling_numbers_render() -> None:
    """US-I7 AC: the report ALWAYS shows week-to-date overs vs the ceiling."""
    html = render_report_html(_bowling_body())
    assert "12.5" in html
    assert "20" in html
    assert "45" in html
    assert "2026-07-06" in html


def test_null_workload_renders_an_honest_absence() -> None:
    body = _assemble([])
    bowling = _bowling_block()
    bowling["workload"] = None
    body["bowling"] = bowling
    html = render_report_html(body)
    assert "Bowling workload" in html  # the block never silently disappears
    assert "not available" in html


def test_workload_violations_are_listed() -> None:
    html = render_report_html(
        _bowling_body(
            workload={
                "window": {"start": "2026-07-06", "end": "2026-07-10"},
                "weighted_overs": 22.0,
                "ceiling_overs": 20.0,
                "remaining_balls": 0,
                "violations": ["weekly_ceiling_exceeded"],
            }
        )
    )
    assert "weekly_ceiling_exceeded" in html


def test_scorecard_rows_render_counts_and_shares() -> None:
    html = render_report_html(_bowling_body())
    for token in ("overall", "leg_break", "googly", "83.3", "70"):
        assert token in html, token
    assert "Counted 10 of 12" in html


def test_release_scatter_summary_renders_stats() -> None:
    html = render_report_html(_bowling_body())
    for token in ("201.5", "3.2", "196", "207"):
        assert token in html, token


def test_agreement_matrix_and_totals_render() -> None:
    html = render_report_html(_bowling_body())
    assert "75" in html
    assert "unclear" in html
    assert "Declared intent is the ground truth." in html


def test_learning_modules_render_title_legend_and_texts() -> None:
    html = render_report_html(_bowling_body())
    for token in (
        "Land it on the coin",
        "Shane Warne",
        "Target discipline first, magic second.",
        "Look at where the ball pitched.",
        "Bowl a full block at a marker.",
    ):
        assert token in html, token


def test_empty_learning_modules_render_no_module_section() -> None:
    body = _assemble([])
    bowling = _bowling_block()
    bowling["learning_modules"] = []
    body["bowling"] = bowling
    assert "Learning modules" not in render_report_html(body)


def test_null_stats_render_an_honest_dash() -> None:
    body = _assemble([])
    bowling = _bowling_block()
    bowling["release_scatter"]["sigma_cm"] = None  # 1-ball sigma is honestly null
    bowling["accuracy_scorecard"]["overall"]["pct"] = None
    body["bowling"] = bowling
    assert "-" in render_report_html(body)


def test_bodies_without_a_bowling_key_render_unchanged() -> None:
    html = render_report_html(_assemble([]))
    assert "Bowling" not in html
    assert "scorecard" not in html.lower()


def test_bowling_strings_are_escaped() -> None:
    body = _assemble([])
    bowling = _bowling_block()
    bowling["learning_modules"][0]["title"] = "<script>alert(1)</script>"
    body["bowling"] = bowling
    html = render_report_html(body)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_cell_text_formats_every_structured_value_shape() -> None:
    assert _cell_text(None) == "-"
    assert _cell_text(True) == "yes"
    assert _cell_text(False) == "no"
    assert _cell_text(70.0) == "70"
    assert _cell_text("leg_break") == "leg_break"


def test_module_without_a_drill_skips_that_paragraph() -> None:
    body = _assemble([])
    bowling = _bowling_block()
    bowling["learning_modules"][0]["drill"] = None
    body["bowling"] = bowling
    html = render_report_html(body)
    assert "Bowl a full block at a marker." not in html
    assert "Target discipline first, magic second." in html


# --- US-H2 plan-vs-actual batting split (finding [12/19/31]) -----------------


def _reconciliation() -> SplitReconciliation:
    """The T5 #2 golden's hand-computed split: one on-plan block, one over
    threshold, and a skipped protected fun block."""
    return SplitReconciliation(
        intents=(
            IntentReconciliation("technical", 4, 3, -25.0, flagged=False),
            IntentReconciliation("decision", 2, 3, 50.0, flagged=True),
            IntentReconciliation("spin_specific", 3, 3, 0.0, flagged=False),
            IntentReconciliation("fun", 1, 0, -100.0, flagged=True),
        ),
        planned_total=10,
        actual_total=9,
        flagged_intents=("decision", "fun"),
        fun_block_intact=False,
    )


def _split_body() -> dict[str, Any]:
    body = _assemble([])
    body["batting_split"] = batting_split_block(_reconciliation())
    return body


def test_batting_split_block_serializes_every_field() -> None:
    """The additive block mirrors the reconciliation dataclass field for field,
    under the fixed number-free producer note."""
    block = batting_split_block(_reconciliation())
    assert block == {
        "intents": [
            {
                "intent": "technical",
                "planned_balls": 4,
                "actual_balls": 3,
                "deviation_pct": -25.0,
                "flagged": False,
            },
            {
                "intent": "decision",
                "planned_balls": 2,
                "actual_balls": 3,
                "deviation_pct": 50.0,
                "flagged": True,
            },
            {
                "intent": "spin_specific",
                "planned_balls": 3,
                "actual_balls": 3,
                "deviation_pct": 0.0,
                "flagged": False,
            },
            {
                "intent": "fun",
                "planned_balls": 1,
                "actual_balls": 0,
                "deviation_pct": -100.0,
                "flagged": True,
            },
        ],
        "planned_total": 10,
        "actual_total": 9,
        "flagged_intents": ["decision", "fun"],
        "fun_block_intact": False,
        "note": BATTING_SPLIT_NOTE,
    }
    assert not any(ch.isdigit() for ch in block["note"])  # numbers ride as data


def test_batting_split_renders_the_plan_vs_actual_table() -> None:
    """US-H2 AC: the split table ships on the printable surface."""
    html = render_report_html(_split_body())
    assert "Plan vs actual" in html
    for token in ("technical", "decision", "spin_specific", "fun", "-25", "50", "-100"):
        assert token in html, token
    assert "yes" in html and "no" in html  # the flagged column
    assert BATTING_SPLIT_NOTE in html


def test_batting_split_numbers_are_claim_exempt() -> None:
    """The split's numbers are structured data, not wording — the claims gate
    never flags them (the k4 precedent, like the bowling blocks)."""
    assert validate_claims_coverage(_split_body()) == []


def test_batting_split_absent_by_default() -> None:
    """A body without the additive key never renders the section."""
    assert "Plan vs actual" not in render_report_html(_assemble([]))
