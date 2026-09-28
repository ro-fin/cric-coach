"""US-J1 contract validators: every pinned cross-group shape, exact violation paths."""

import hashlib
from typing import Any

import pytest
from cricai_coaching import planner_agent
from cricai_coaching.contracts import (
    BALL_RECORD_MAJOR,
    PROGRESS_MIN_BALLS_PER_POINT,
    PROGRESS_MIN_SESSIONS,
    QUALITY_COMPONENT_KEYS,
    REPORT_MAX_SECONDARY,
    ContractViolation,
    validate_ball_record,
    validate_drill_plan_blocks,
    validate_finding,
    validate_progress_snapshot,
    validate_quality_score,
    validate_report_body,
    validate_safety_verdict,
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def ball_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "ball_id": 7,
        "session_id": "8b6d2f2e-0000-0000-0000-000000000001",
        "identity": {"mode": "batting"},
        "confidence": {"line": 0.9, "shot": 1.0},
        "source": {"line": "auto", "shot": "manual"},
        "clips": {"C1": "clip-1", "C3": "clip-3"},
    }
    record.update(overrides)
    return record


def finding(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "finding_id": "f-1",
        "agent": "batting_analysis",
        "rule_key": "reaching_outside_off",
        "kind": "rule_hit",
        "severity": "major",
        "metric": "control_pct",
        "condition": {"line": "outside_off"},
        "n": 42,
        "effect_size": 0.31,
        "confidence": 0.9,
        "ball_ids": [3, 17, 29],
        "evidence": {"3": {"C1": "clip-3-c1"}},
        "text_data": {"drill_hint": "drive-or-leave", "note": ""},
    }
    data.update(overrides)
    return data


def verdict(text: str = "Bowling is paused: weekly overs ceiling reached.") -> dict[str, Any]:
    return {"active": True, "codes": ["workload_ceiling"], "text": text, "sha256": sha(text)}


def report_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "daily",
        "period": {"start": "2026-07-09", "end": "2026-07-09"},
        "main_correction": {"finding_id": "f-1", "text": "Reaching at wide balls.", "evidence": {}},
        "drill": {
            "drill_id": "d-9",
            "text": "80 balls outside off, drive or leave.",
            "machine_settings": {"speed_kph": 85},
            "success_metric": "control_pct",
        },
        "goal": {"metric": "control_pct", "target": 70, "condition": {"line": "outside_off"}},
        "secondary": [{"finding_id": "f-2", "text": "Late footwork vs short."}],
        "positive": "Front-foot drives were crisp today.",
        "safety": verdict(),
        "honesty_banner": None,
        "coverage_note": None,
        "fatigue_note": None,
        "claims": [
            {"value": 62.0, "metric": "control_pct", "recompute_key": "control:outside_off"}
        ],
    }
    body.update(overrides)
    return body


def block(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "intent": "technical",
        "balls": 80,
        "drill_id": "d-9",
        "machine_settings": {"speed_kph": 85},
        "success_metric": "control_pct",
        "finding_id": "f-1",
    }
    data.update(overrides)
    return data


def quality(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "components": {
            "sync": 0.9,
            "exposure": 0.8,
            "pose_coverage": 0.7,
            "track_coverage": 0.95,
            "calibration_freshness": 1.0,
        },
        "composite": 0.87,
        "banner": None,
    }
    data.update(overrides)
    return data


def snapshot(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "metric": "control_pct",
        "zone_key": "outside_off/good",
        "window": "last_10_sessions",
        "points": [
            {"session_id": f"s-{i}", "date": f"2026-07-0{i}", "value": 60.0 + i, "n": 45}
            for i in range(1, 4)
        ],
        "baseline": 58.5,
        "direction": "improving",
        "qualified": True,
    }
    data.update(overrides)
    return data


def path_of(excinfo: pytest.ExceptionInfo[ContractViolation]) -> str:
    return excinfo.value.path


# ---------------------------------------------------------------- BallRecord


def test_ball_record_valid_passes() -> None:
    validate_ball_record(ball_record())


def test_ball_record_major_only_version_passes() -> None:
    validate_ball_record(ball_record(schema_version=str(BALL_RECORD_MAJOR)))


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        ({"schema_version": "v1"}, "ball_record.schema_version"),
        ({"schema_version": "1.2.3"}, "ball_record.schema_version"),
        ({"schema_version": 1}, "ball_record.schema_version"),
        ({"schema_version": "2.0"}, "ball_record.schema_version"),
        ({"ball_id": 0}, "ball_record.ball_id"),
        ({"ball_id": True}, "ball_record.ball_id"),
        ({"session_id": ""}, "ball_record.session_id"),
        ({"confidence": [0.9]}, "ball_record.confidence"),
        ({"confidence": {"line": 1.5}}, "ball_record.confidence.line"),
        ({"confidence": {"line": "high"}}, "ball_record.confidence.line"),
        ({"source": {"line": 3}}, "ball_record.source.line"),
        ({"clips": "C1"}, "ball_record.clips"),
    ],
)
def test_ball_record_violations(mutation: dict[str, Any], path: str) -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_ball_record(ball_record(**mutation))
    assert path_of(excinfo) == path


def test_ball_record_rejects_non_object_and_missing_version() -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_ball_record(["not", "a", "record"])
    assert path_of(excinfo) == "ball_record"
    record = ball_record()
    del record["schema_version"]
    with pytest.raises(ContractViolation) as excinfo:
        validate_ball_record(record)
    assert path_of(excinfo) == "ball_record.schema_version"


def test_ball_record_unknown_major_message_names_supported_major() -> None:
    with pytest.raises(ContractViolation, match=f"understands MAJOR {BALL_RECORD_MAJOR}"):
        validate_ball_record(ball_record(schema_version="9.1"))


# ------------------------------------------------------------------ Finding


def test_finding_valid_passes() -> None:
    validate_finding(finding())


def test_finding_probe_origin_passes_without_rule_key() -> None:
    validate_finding(finding(rule_key=None, probe="first50_last50"))


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        ({"finding_id": ""}, "finding.finding_id"),
        ({"agent": 4}, "finding.agent"),
        ({"rule_key": None}, "finding.rule_key"),
        ({"rule_key": None, "probe": "  "}, "finding.rule_key"),
        ({"kind": ""}, "finding.kind"),
        ({"severity": "catastrophic"}, "finding.severity"),
        ({"metric": ""}, "finding.metric"),
        ({"condition": []}, "finding.condition"),
        ({"n": -1}, "finding.n"),
        ({"n": True}, "finding.n"),
        ({"effect_size": float("nan")}, "finding.effect_size"),
        ({"confidence": 1.2}, "finding.confidence"),
        ({"ball_ids": 3}, "finding.ball_ids"),
        ({"ball_ids": [3, 0]}, "finding.ball_ids[1]"),
        ({"evidence": "clip"}, "finding.evidence"),
        ({"evidence": {"3": "clip"}}, "finding.evidence.3"),
        ({"evidence": {"3": {"C1": 9}}}, "finding.evidence.3.C1"),
        ({"text_data": None}, "finding.text_data"),
        ({"text_data": {"note": 5}}, "finding.text_data.note"),
    ],
)
def test_finding_violations(mutation: dict[str, Any], path: str) -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_finding(finding(**mutation))
    assert path_of(excinfo) == path


def test_finding_missing_key_reports_key_path() -> None:
    data = finding()
    del data["ball_ids"]
    with pytest.raises(ContractViolation, match="missing required key") as excinfo:
        validate_finding(data)
    assert path_of(excinfo) == "finding.ball_ids"


def test_finding_effect_size_null_is_allowed() -> None:
    validate_finding(finding(effect_size=None))


# ------------------------------------------------------------ SafetyVerdict


@pytest.mark.safety
def test_safety_verdict_valid_passes() -> None:
    validate_safety_verdict(verdict())


@pytest.mark.safety
def test_safety_verdict_empty_text_with_matching_hash_passes() -> None:
    validate_safety_verdict({"active": False, "codes": [], "text": "", "sha256": sha("")})


@pytest.mark.safety
def test_safety_verdict_rejects_tampered_text() -> None:
    """US-H5: any edit to the verbatim safety text breaks the hash and is rejected."""
    tampered = verdict()
    tampered["text"] = tampered["text"].replace("paused", "encouraged")
    with pytest.raises(ContractViolation, match="does not match SHA-256") as excinfo:
        validate_safety_verdict(tampered)
    assert path_of(excinfo) == "safety_verdict.sha256"


@pytest.mark.safety
@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        ({"active": "yes"}, "safety_verdict.active"),
        ({"codes": "workload_ceiling"}, "safety_verdict.codes"),
        ({"codes": [1]}, "safety_verdict.codes[0]"),
        ({"codes": ["overtraining"]}, "safety_verdict.codes[0]"),
        ({"text": None}, "safety_verdict.text"),
        ({"sha256": 42}, "safety_verdict.sha256"),
    ],
)
def test_safety_verdict_violations(mutation: dict[str, Any], path: str) -> None:
    data = verdict()
    data.update(mutation)
    with pytest.raises(ContractViolation) as excinfo:
        validate_safety_verdict(data)
    assert path_of(excinfo) == path


@pytest.mark.safety
def test_safety_verdict_must_be_object() -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_safety_verdict("all good")
    assert path_of(excinfo) == "safety_verdict"


# -------------------------------------------------------------- Report body


def test_report_body_valid_passes() -> None:
    validate_report_body(report_body())


def test_report_body_honesty_path_passes() -> None:
    """Degraded-but-honest (US-G3/J1): null sections with a banner are legal."""
    validate_report_body(
        report_body(
            main_correction=None,
            drill=None,
            goal=None,
            positive=None,
            safety=None,
            honesty_banner="Analysis unavailable today: pose stage failed.",
            secondary=[],
            claims=[],
        )
    )


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        ({"kind": "hourly"}, "report.kind"),
        ({"period": "today"}, "report.period"),
        (
            {"main_correction": {"finding_id": "f-1", "text": "x"}},
            "report.main_correction.evidence",
        ),
        (
            {"main_correction": {"finding_id": 1, "text": "x", "evidence": {}}},
            "report.main_correction.finding_id",
        ),
        (
            {"drill": {"drill_id": "d", "text": "t", "machine_settings": {}}},
            "report.drill.success_metric",
        ),
        ({"goal": {"metric": "control_pct", "condition": {}}}, "report.goal.target"),
        (
            {"goal": {"metric": "control_pct", "target": 70, "condition": 3}},
            "report.goal.condition",
        ),
        ({"secondary": "none"}, "report.secondary"),
        ({"secondary": [{}, {}, {}, {}]}, "report.secondary"),
        ({"secondary": ["finding"]}, "report.secondary[0]"),
        ({"positive": 12}, "report.positive"),
        ({"safety": {"active": True}}, "safety_verdict.codes"),
        ({"honesty_banner": 5}, "report.honesty_banner"),
        ({"claims": {}}, "report.claims"),
        ({"claims": ["62"]}, "report.claims[0]"),
        (
            {"claims": [{"value": float("inf"), "metric": "m", "recompute_key": "k"}]},
            "report.claims[0].value",
        ),
        (
            {"claims": [{"value": 62, "metric": "m", "recompute_key": ""}]},
            "report.claims[0].recompute_key",
        ),
    ],
)
def test_report_body_violations(mutation: dict[str, Any], path: str) -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_report_body(report_body(**mutation))
    assert path_of(excinfo) == path


def test_report_secondary_cap_is_pinned() -> None:
    assert REPORT_MAX_SECONDARY == 3
    validate_report_body(report_body(secondary=[{}, {}, {}]))


def test_report_body_accepts_null_drill_id() -> None:
    """A fallback drill (no library drill mapped) carries drill_id=null — the
    production shape when no resolver is injected (finding 46)."""
    validate_report_body(
        report_body(
            drill={
                "drill_id": None,
                "text": "Repeat the focus drill on this correction.",
                "machine_settings": {},
                "success_metric": "control_pct",
            }
        )
    )


def test_report_body_accepts_a_fatigue_note() -> None:
    """The always-present fatigue_note key is a dict|null (US-H3, finding [53]/46)."""
    validate_report_body(
        report_body(
            fatigue_note={
                "text": "You tired late in the session - shorter blocks tomorrow.",
                "window": 10,
                "control_drop_points": 22.5,
                "degrading_metrics": ["head_stability_score"],
            }
        )
    )


@pytest.mark.parametrize(
    ("note", "path"),
    [
        ("not-a-dict", "report.fatigue_note"),
        (
            {"window": 10, "control_drop_points": 1.0, "degrading_metrics": []},
            "report.fatigue_note.text",
        ),
        (
            {"text": "t", "control_drop_points": 1.0, "degrading_metrics": []},
            "report.fatigue_note.window",
        ),
        (
            {"text": "t", "window": 10, "degrading_metrics": []},
            "report.fatigue_note.control_drop_points",
        ),
        (
            {"text": "t", "window": 10, "control_drop_points": 1.0},
            "report.fatigue_note.degrading_metrics",
        ),
        (
            {"text": "t", "window": 10, "control_drop_points": 1.0, "degrading_metrics": [7]},
            "report.fatigue_note.degrading_metrics[0]",
        ),
    ],
)
def test_report_fatigue_note_violations(note: Any, path: str) -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_report_body(report_body(fatigue_note=note))
    assert path_of(excinfo) == path


def test_report_body_missing_fatigue_note_key_is_rejected() -> None:
    body = report_body()
    del body["fatigue_note"]
    with pytest.raises(ContractViolation, match="missing required key") as excinfo:
        validate_report_body(body)
    assert path_of(excinfo) == "report.fatigue_note"


def test_report_body_accepts_a_coverage_note() -> None:
    """The always-present coverage_note key is a str|null (day-scoped honesty)."""
    validate_report_body(report_body(coverage_note="One session could not be analysed."))


def test_report_body_coverage_note_must_be_a_string() -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_report_body(report_body(coverage_note=5))
    assert path_of(excinfo) == "report.coverage_note"


def test_report_body_missing_coverage_note_key_is_rejected() -> None:
    body = report_body()
    del body["coverage_note"]
    with pytest.raises(ContractViolation, match="missing required key") as excinfo:
        validate_report_body(body)
    assert path_of(excinfo) == "report.coverage_note"


# --------------------------------------------------------- DrillPlan blocks


def test_drill_plan_blocks_valid_passes() -> None:
    validate_drill_plan_blocks([block(), block(intent="fun", drill_id=None, finding_id=None)])


def test_drill_plan_blocks_accept_a_bowling_block() -> None:
    """Contract #5 legitimately carries a bowling block: intent='bowling', no
    drill_id/finding_id required (finding 43)."""
    validate_drill_plan_blocks(
        [
            block(
                intent="bowling",
                drill_id=None,
                finding_id="maintenance",
                success_metric="landing_accuracy_pct",
            )
        ]
    )


def test_drill_plan_blocks_accept_real_build_plan_output() -> None:
    """The pinned producer (planner_agent.build_plan) and this validator agree on
    the block vocabulary, bowling block included (finding 43)."""
    split = {"blocks": {"technical": 30, "decision": 20, "fun": 10}, "daily_balls": 60}
    result = planner_agent.build_plan(
        [], [], planner_agent.PlanContext(split=split, bowling_allowance_balls=30)
    )
    assert any(b["intent"] == planner_agent.BOWLING_INTENT for b in result.blocks)
    validate_drill_plan_blocks(result.blocks)


def test_drill_plan_blocks_empty_list_passes() -> None:
    validate_drill_plan_blocks([])


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        ({"intent": "grind"}, "blocks[0].intent"),
        ({"balls": 0}, "blocks[0].balls"),
        ({"balls": 12.5}, "blocks[0].balls"),
        ({"drill_id": 9}, "blocks[0].drill_id"),
        ({"machine_settings": None}, "blocks[0].machine_settings"),
        ({"success_metric": ""}, "blocks[0].success_metric"),
        ({"finding_id": 1}, "blocks[0].finding_id"),
    ],
)
def test_drill_plan_block_violations(mutation: dict[str, Any], path: str) -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_drill_plan_blocks([block(**mutation)])
    assert path_of(excinfo) == path


def test_drill_plan_blocks_rejects_non_list_and_non_object_block() -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_drill_plan_blocks({"intent": "fun"})
    assert path_of(excinfo) == "blocks"
    with pytest.raises(ContractViolation) as excinfo:
        validate_drill_plan_blocks(["fun"])
    assert path_of(excinfo) == "blocks[0]"


# ------------------------------------------------------------- QualityScore


def test_quality_score_valid_passes() -> None:
    validate_quality_score(quality())
    validate_quality_score(quality(banner="Data quality is low today; treat trends carefully."))


@pytest.mark.parametrize(
    ("mutation", "path", "match"),
    [
        ({"components": {"sync": 1.0}}, "quality.components", "missing components"),
        (
            {
                "components": {
                    **dict.fromkeys(QUALITY_COMPONENT_KEYS, 0.5),
                    "vibes": 1.0,
                }
            },
            "quality.components",
            "unknown components",
        ),
        (
            {"components": {**dict.fromkeys(QUALITY_COMPONENT_KEYS, 0.5), "sync": -0.1}},
            "quality.components.sync",
            "within",
        ),
        ({"composite": 1.7}, "quality.composite", "within"),
        ({"banner": 0}, "quality.banner", "string"),
    ],
)
def test_quality_score_violations(mutation: dict[str, Any], path: str, match: str) -> None:
    with pytest.raises(ContractViolation, match=match) as excinfo:
        validate_quality_score(quality(**mutation))
    assert path_of(excinfo) == path


# --------------------------------------------------------- ProgressSnapshot


def test_progress_snapshot_valid_qualified_passes() -> None:
    validate_progress_snapshot(snapshot())


def test_progress_snapshot_unqualified_needs_no_points() -> None:
    validate_progress_snapshot(snapshot(points=[], baseline=None, qualified=False))


def test_progress_snapshot_allows_null_session_id_point() -> None:
    """A date slice that merged several sessions has no single session id, so the
    producer emits null and the validator accepts it (progress_agent._point)."""
    merged = {"session_id": None, "date": "2026-07-01", "value": 60.0, "n": 45}
    validate_progress_snapshot(snapshot(points=[merged], qualified=False))


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        ({"metric": ""}, "progress.metric"),
        ({"zone_key": None}, "progress.zone_key"),
        ({"window": 7}, "progress.window"),
        ({"points": "many"}, "progress.points"),
        ({"points": ["p1"]}, "progress.points[0]"),
        ({"baseline": float("-inf")}, "progress.baseline"),
        ({"direction": "sideways"}, "progress.direction"),
        ({"qualified": "yes"}, "progress.qualified"),
    ],
)
def test_progress_snapshot_violations(mutation: dict[str, Any], path: str) -> None:
    with pytest.raises(ContractViolation) as excinfo:
        validate_progress_snapshot(snapshot(**mutation))
    assert path_of(excinfo) == path


def test_progress_point_field_violations() -> None:
    bad_point = {"session_id": "s-1", "date": "2026-07-01", "value": 60.0, "n": -3}
    with pytest.raises(ContractViolation) as excinfo:
        validate_progress_snapshot(snapshot(points=[bad_point], qualified=False))
    assert path_of(excinfo) == "progress.points[0].n"
    no_value = {"session_id": "s-1", "date": "2026-07-01", "n": 40}
    with pytest.raises(ContractViolation) as excinfo:
        validate_progress_snapshot(snapshot(points=[no_value], qualified=False))
    assert path_of(excinfo) == "progress.points[0].value"


@pytest.mark.safety
def test_progress_qualification_guardrail() -> None:
    """US-J4: 'qualified' claims need >=3 sessions and >=30 balls per point."""
    too_few = snapshot()
    too_few["points"] = too_few["points"][: PROGRESS_MIN_SESSIONS - 1]
    with pytest.raises(ContractViolation) as excinfo:
        validate_progress_snapshot(too_few)
    assert path_of(excinfo) == "progress.qualified"

    thin = snapshot()
    thin["points"][1]["n"] = PROGRESS_MIN_BALLS_PER_POINT - 1
    with pytest.raises(ContractViolation) as excinfo:
        validate_progress_snapshot(thin)
    assert path_of(excinfo) == "progress.qualified"


def test_contract_violation_carries_path_and_problem() -> None:
    error = ContractViolation("finding.n", "must be >= 0")
    assert error.path == "finding.n"
    assert error.problem == "must be >= 0"
    assert str(error) == "finding.n: must be >= 0"
