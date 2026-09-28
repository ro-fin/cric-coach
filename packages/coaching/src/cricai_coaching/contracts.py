"""Typed I/O contracts between pipeline agents (US-J1, plan "Pinned cross-group contracts").

Each ``validate_*`` function is the runtime guard the orchestrator runs between
stages: it raises :class:`ContractViolation` naming the precise path of the
first offending field, so a contract-invalid agent output is rejected loudly
(US-J1 AC "orchestrator rejects contract-invalid outputs") instead of flowing
downstream. Validators check structure and the pinned invariants only — domain
judgement (ranking, workload math, wording) belongs to the owning agents.

Covered contracts (numbers from the Phase-5 plan):
#1 BallRecord envelope (consumers reject unknown schema_version MAJOR),
#2 Finding, #3 SafetyVerdict (verbatim-text SHA-256 check, US-H5),
#4 Report body v1, #5 DrillPlan blocks, #6 QualityScore, #7 ProgressSnapshot.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from cricai_data.enums import BlockIntent, FindingSeverity, ReportKind, SafetyCode

#: The BallRecord MAJOR this codebase understands (contract #1); ``"1.0"`` today.
BALL_RECORD_MAJOR = 1

_SCHEMA_VERSION_RE = re.compile(r"^(\d+)(?:\.\d+)?$")

#: QualityScore components (contract #6) — exactly these, no more, no fewer.
QUALITY_COMPONENT_KEYS: frozenset[str] = frozenset(
    {"sync", "exposure", "pose_coverage", "track_coverage", "calibration_freshness"}
)

#: ProgressSnapshot trend vocabulary (contract #7).
PROGRESS_DIRECTIONS: frozenset[str] = frozenset({"improving", "flat", "regressing"})

#: Qualification guardrails (contract #7): a snapshot may claim ``qualified``
#: only with at least this many points, each backed by at least this many balls.
PROGRESS_MIN_SESSIONS = 3
PROGRESS_MIN_BALLS_PER_POINT = 30

#: Report body v1 secondary findings cap (contract #4).
REPORT_MAX_SECONDARY = 3

_SEVERITIES = frozenset(member.value for member in FindingSeverity)
_SAFETY_CODES = frozenset(member.value for member in SafetyCode)
_REPORT_KINDS = frozenset(member.value for member in ReportKind)
_BLOCK_INTENTS = frozenset(member.value for member in BlockIntent)

#: Bowling blocks carry this intent marker instead of a ``BlockIntent`` value
#: (pinned contract #5 seam; mirrors ``planner_agent.BOWLING_INTENT``). A drill
#: plan legitimately contains a bowling block, so the block validator accepts it
#: alongside the batting intents — rejecting it broke every real plan (finding 43).
BOWLING_BLOCK_INTENT = "bowling"

#: The full block-intent vocabulary contract #5 accepts (batting + bowling).
_PLAN_INTENTS: frozenset[str] = _BLOCK_INTENTS | {BOWLING_BLOCK_INTENT}


class ContractViolation(ValueError):
    """An agent payload broke a pinned cross-group contract.

    ``path`` locates the offending field (e.g. ``finding.ball_ids[2]``);
    ``problem`` says what is wrong with it. The orchestrator records the
    message verbatim on the failing stage row (US-J1 debuggability).
    """

    def __init__(self, path: str, problem: str) -> None:
        self.path = path
        self.problem = problem
        super().__init__(f"{path}: {problem}")


def _require(condition: bool, path: str, problem: str) -> None:
    if not condition:
        raise ContractViolation(path, problem)


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractViolation(path, "must be an object")
    return value


def _sequence(value: object, path: str, problem: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractViolation(path, problem)
    return value


def _field(data: dict[str, Any], key: str, path: str) -> Any:
    _require(key in data, f"{path}.{key}", "missing required key")
    return data[key]


def _string(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractViolation(path, "must be a string")
    _require(allow_empty or bool(value.strip()), path, "must be a non-empty string")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractViolation(path, "must be a boolean")
    return value


def _integer(value: object, path: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractViolation(path, "must be an integer")
    _require(value >= minimum, path, f"must be >= {minimum}")
    return value


def _finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ContractViolation(path, "must be a finite number")
    return float(value)


def _fraction(value: object, path: str) -> float:
    number = _finite_number(value, path)
    _require(0.0 <= number <= 1.0, path, "must be within [0, 1]")
    return number


def _string_mapping(value: object, path: str) -> dict[str, Any]:
    data = _mapping(value, path)
    for key, item in data.items():
        _string(item, f"{path}.{key}", allow_empty=True)
    return data


def validate_ball_record(record: object) -> None:
    """Contract #1 envelope check: version-gated per-ball record (US-G1).

    Consumers reject any record whose ``schema_version`` MAJOR they do not
    understand; the full field-level JSON Schema lives with the producer
    (``cricai_data.ballrecord``, story g1) — this guard is the cheap
    between-stage envelope check.
    """
    data = _mapping(record, "ball_record")
    version = _string(_field(data, "schema_version", "ball_record"), "ball_record.schema_version")
    match = _SCHEMA_VERSION_RE.match(version)
    if match is None:
        raise ContractViolation(
            "ball_record.schema_version", f"must look like 'MAJOR.MINOR', got {version!r}"
        )
    major = int(match.group(1))
    _require(
        major == BALL_RECORD_MAJOR,
        "ball_record.schema_version",
        f"unknown MAJOR {major} (this consumer understands MAJOR {BALL_RECORD_MAJOR})",
    )
    _integer(_field(data, "ball_id", "ball_record"), "ball_record.ball_id", minimum=1)
    _string(_field(data, "session_id", "ball_record"), "ball_record.session_id")
    confidence = _mapping(_field(data, "confidence", "ball_record"), "ball_record.confidence")
    for key, value in confidence.items():
        _fraction(value, f"ball_record.confidence.{key}")
    _string_mapping(_field(data, "source", "ball_record"), "ball_record.source")
    _string_mapping(_field(data, "clips", "ball_record"), "ball_record.clips")


def validate_finding(finding: object) -> None:
    """Contract #2: machine findings — JSON only, no free prose (US-J2 AC)."""
    data = _mapping(finding, "finding")
    _string(_field(data, "finding_id", "finding"), "finding.finding_id")
    _string(_field(data, "agent", "finding"), "finding.agent")
    origin = data.get("rule_key") or data.get("probe")
    _require(
        isinstance(origin, str) and bool(origin.strip()),
        "finding.rule_key",
        "one of 'rule_key' or 'probe' must name the finding's origin",
    )
    _string(_field(data, "kind", "finding"), "finding.kind")
    severity = _string(_field(data, "severity", "finding"), "finding.severity")
    _require(
        severity in _SEVERITIES,
        "finding.severity",
        f"must be one of {sorted(_SEVERITIES)}, got {severity!r}",
    )
    _string(_field(data, "metric", "finding"), "finding.metric")
    _mapping(_field(data, "condition", "finding"), "finding.condition")
    _integer(_field(data, "n", "finding"), "finding.n", minimum=0)
    effect_size = _field(data, "effect_size", "finding")
    if effect_size is not None:
        _finite_number(effect_size, "finding.effect_size")
    _fraction(_field(data, "confidence", "finding"), "finding.confidence")
    ball_ids = _sequence(
        _field(data, "ball_ids", "finding"), "finding.ball_ids", "must be a list of ball numbers"
    )
    for index, ball_id in enumerate(ball_ids):
        _integer(ball_id, f"finding.ball_ids[{index}]", minimum=1)
    evidence = _mapping(_field(data, "evidence", "finding"), "finding.evidence")
    for ball_no, clips in evidence.items():
        _string_mapping(clips, f"finding.evidence.{ball_no}")
    text_data = _mapping(_field(data, "text_data", "finding"), "finding.text_data")
    for key, value in text_data.items():
        # Rule-authored strings only: free prose never originates in findings.
        _string(value, f"finding.text_data.{key}", allow_empty=True)


def validate_safety_verdict(verdict: object) -> None:
    """Contract #3: SafetyVerdict with the US-H5 verbatim-text hash check.

    ``sha256`` must be the SHA-256 hex of ``text`` — the publish validator
    recomputes it, so any post-hoc edit of the safety wording is rejected.
    """
    data = _mapping(verdict, "safety_verdict")
    _boolean(_field(data, "active", "safety_verdict"), "safety_verdict.active")
    codes = _sequence(
        _field(data, "codes", "safety_verdict"),
        "safety_verdict.codes",
        "must be a list of safety codes",
    )
    for index, code in enumerate(codes):
        path = f"safety_verdict.codes[{index}]"
        value = _string(code, path)
        _require(
            value in _SAFETY_CODES, path, f"must be one of {sorted(_SAFETY_CODES)}, got {value!r}"
        )
    text = _string(_field(data, "text", "safety_verdict"), "safety_verdict.text", allow_empty=True)
    sha = _string(_field(data, "sha256", "safety_verdict"), "safety_verdict.sha256")
    expected = hashlib.sha256(text.encode()).hexdigest()
    _require(
        sha == expected,
        "safety_verdict.sha256",
        "does not match SHA-256 of 'text' (verbatim-insertion check, US-H5)",
    )


def _validate_main_correction(value: object, path: str) -> None:
    data = _mapping(value, path)
    _string(_field(data, "finding_id", path), f"{path}.finding_id")
    _string(_field(data, "text", path), f"{path}.text", allow_empty=True)
    _mapping(_field(data, "evidence", path), f"{path}.evidence")


def _validate_drill_section(value: object, path: str) -> None:
    data = _mapping(value, path)
    drill_id = _field(data, "drill_id", path)
    # A fallback drill (no library drill mapped) carries drill_id=null and
    # rule-authored text — the production shape when no DrillResolver is injected
    # (finding 46); the key must still exist, but null is legal.
    if drill_id is not None:
        _string(drill_id, f"{path}.drill_id")
    _string(_field(data, "text", path), f"{path}.text", allow_empty=True)
    _mapping(_field(data, "machine_settings", path), f"{path}.machine_settings")
    _string(_field(data, "success_metric", path), f"{path}.success_metric")


def _validate_fatigue_note(value: object, path: str) -> None:
    """Report body v1 fatigue note (US-H3, finding [53]): number-free text plus
    the structured components the scorer emits."""
    data = _mapping(value, path)
    _string(_field(data, "text", path), f"{path}.text", allow_empty=True)
    _integer(_field(data, "window", path), f"{path}.window", minimum=0)
    _finite_number(_field(data, "control_drop_points", path), f"{path}.control_drop_points")
    metrics = _sequence(
        _field(data, "degrading_metrics", path),
        f"{path}.degrading_metrics",
        "must be a list of metric names",
    )
    for index, metric in enumerate(metrics):
        _string(metric, f"{path}.degrading_metrics[{index}]")


def _validate_goal(value: object, path: str) -> None:
    data = _mapping(value, path)
    _string(_field(data, "metric", path), f"{path}.metric")
    _field(data, "target", path)
    _mapping(_field(data, "condition", path), f"{path}.condition")


def _validate_claims(value: object, path: str) -> None:
    claims = _sequence(value, path, "must be a list of claims")
    for index, claim in enumerate(claims):
        claim_path = f"{path}[{index}]"
        data = _mapping(claim, claim_path)
        _finite_number(_field(data, "value", claim_path), f"{claim_path}.value")
        _string(_field(data, "metric", claim_path), f"{claim_path}.metric")
        _string(_field(data, "recompute_key", claim_path), f"{claim_path}.recompute_key")


def validate_report_body(body: object) -> None:
    """Contract #4: Report body v1 (g2 owns content; j1 publishes).

    ``main_correction``/``drill``/``goal`` may be null on the honesty path
    (nothing credible to say beats invention, US-G3) but the keys must exist;
    every number quoted in text must reappear in ``claims`` for recomputation.
    ``drill.drill_id`` may be null (the fallback-drill shape, finding 46) and the
    always-present ``fatigue_note`` key is a dict or null (US-H3, finding [53]).
    """
    data = _mapping(body, "report")
    kind = _string(_field(data, "kind", "report"), "report.kind")
    _require(
        kind in _REPORT_KINDS,
        "report.kind",
        f"must be one of {sorted(_REPORT_KINDS)}, got {kind!r}",
    )
    _mapping(_field(data, "period", "report"), "report.period")
    main_correction = _field(data, "main_correction", "report")
    if main_correction is not None:
        _validate_main_correction(main_correction, "report.main_correction")
    drill = _field(data, "drill", "report")
    if drill is not None:
        _validate_drill_section(drill, "report.drill")
    goal = _field(data, "goal", "report")
    if goal is not None:
        _validate_goal(goal, "report.goal")
    secondary = _sequence(_field(data, "secondary", "report"), "report.secondary", "must be a list")
    _require(
        len(secondary) <= REPORT_MAX_SECONDARY,
        "report.secondary",
        f"at most {REPORT_MAX_SECONDARY} secondary findings, got {len(secondary)}",
    )
    for index, item in enumerate(secondary):
        _mapping(item, f"report.secondary[{index}]")
    positive = _field(data, "positive", "report")
    if positive is not None:
        _string(positive, "report.positive", allow_empty=True)
    safety = _field(data, "safety", "report")
    if safety is not None:
        validate_safety_verdict(safety)
    honesty_banner = _field(data, "honesty_banner", "report")
    if honesty_banner is not None:
        _string(honesty_banner, "report.honesty_banner", allow_empty=True)
    coverage_note = _field(data, "coverage_note", "report")
    if coverage_note is not None:
        _string(coverage_note, "report.coverage_note", allow_empty=True)
    fatigue_note = _field(data, "fatigue_note", "report")
    if fatigue_note is not None:
        _validate_fatigue_note(fatigue_note, "report.fatigue_note")
    _validate_claims(_field(data, "claims", "report"), "report.claims")


def validate_drill_plan_blocks(blocks: object) -> None:
    """Contract #5: DrillPlan blocks — structural shape only.

    Ball-count totals, the H2 quality split, fun-block protection and H1
    bowling allowances are the h1/h2 constraint validators' judgement calls;
    this guard pins the block shape every producer and consumer agrees on.
    """
    items = _sequence(blocks, "blocks", "must be a list of plan blocks")
    for index, block in enumerate(items):
        path = f"blocks[{index}]"
        data = _mapping(block, path)
        intent = _string(_field(data, "intent", path), f"{path}.intent")
        _require(
            intent in _PLAN_INTENTS,
            f"{path}.intent",
            f"must be one of {sorted(_PLAN_INTENTS)}, got {intent!r}",
        )
        _integer(_field(data, "balls", path), f"{path}.balls", minimum=1)
        drill_id = _field(data, "drill_id", path)
        if drill_id is not None:
            _string(drill_id, f"{path}.drill_id")
        _mapping(_field(data, "machine_settings", path), f"{path}.machine_settings")
        _string(_field(data, "success_metric", path), f"{path}.success_metric")
        finding_id = _field(data, "finding_id", path)
        if finding_id is not None:
            _string(finding_id, f"{path}.finding_id")


def validate_quality_score(score: object) -> None:
    """Contract #6: QualityScore — exactly the five pinned components, all 0..1."""
    data = _mapping(score, "quality")
    components = _mapping(_field(data, "components", "quality"), "quality.components")
    missing = sorted(QUALITY_COMPONENT_KEYS - set(components))
    _require(not missing, "quality.components", f"missing components {missing}")
    unknown = sorted(set(components) - QUALITY_COMPONENT_KEYS)
    _require(not unknown, "quality.components", f"unknown components {unknown}")
    for key in sorted(QUALITY_COMPONENT_KEYS):
        _fraction(components[key], f"quality.components.{key}")
    _fraction(_field(data, "composite", "quality"), "quality.composite")
    banner = _field(data, "banner", "quality")
    if banner is not None:
        _string(banner, "quality.banner", allow_empty=True)


def validate_progress_snapshot(snapshot: object) -> None:
    """Contract #7: ProgressSnapshot with the qualification guardrail.

    ``qualified`` may only be true with >= 3 session points and >= 30 balls
    behind each point — a snapshot claiming qualification without the sample
    to back it is rejected (US-J4 statistical guardrails).
    """
    data = _mapping(snapshot, "progress")
    _string(_field(data, "metric", "progress"), "progress.metric")
    _string(_field(data, "zone_key", "progress"), "progress.zone_key")
    _string(_field(data, "window", "progress"), "progress.window")
    points = _sequence(
        _field(data, "points", "progress"), "progress.points", "must be a list of session points"
    )
    ns: list[int] = []
    for index, point in enumerate(points):
        path = f"progress.points[{index}]"
        entry = _mapping(point, path)
        session_id = _field(entry, "session_id", path)
        # A date whose slice merged several sessions has no single session id, so
        # the producer emits null (progress_agent._point); the key must exist.
        if session_id is not None:
            _string(session_id, f"{path}.session_id")
        _string(_field(entry, "date", path), f"{path}.date")
        _finite_number(_field(entry, "value", path), f"{path}.value")
        ns.append(_integer(_field(entry, "n", path), f"{path}.n", minimum=0))
    baseline = _field(data, "baseline", "progress")
    if baseline is not None:
        _finite_number(baseline, "progress.baseline")
    direction = _string(_field(data, "direction", "progress"), "progress.direction")
    _require(
        direction in PROGRESS_DIRECTIONS,
        "progress.direction",
        f"must be one of {sorted(PROGRESS_DIRECTIONS)}, got {direction!r}",
    )
    qualified = _boolean(_field(data, "qualified", "progress"), "progress.qualified")
    if qualified:
        _require(
            len(ns) >= PROGRESS_MIN_SESSIONS and all(n >= PROGRESS_MIN_BALLS_PER_POINT for n in ns),
            "progress.qualified",
            f"requires >= {PROGRESS_MIN_SESSIONS} session points with"
            f" >= {PROGRESS_MIN_BALLS_PER_POINT} balls each",
        )
