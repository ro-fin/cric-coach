"""Batting analysis agent (US-J2): ranked findings, never prose.

Combines injected rule-runner results (story g1's runner output, accepted as
plain Finding dicts per pinned contract #2) with this module's own pattern
probes over BallRecord dicts (pinned contract #1, the flat US-G1 shape):

- ``zone_contrast``: control%% of each (line, length) cell vs the session
  overall control%%.
- ``fatigue_contrast``: control%% of the first ``segment_size`` balls vs the
  last ``segment_size`` (US-J2's first-50/last-50 comparison).

Multiple-comparison discipline (US-J2 AC): every probe candidate must pass the
min-sample gate (US-G2's >= 10-balls default) AND the effect-size floor, and
the combined rule+probe list is cut to the top ``top_k`` (default 5) by an
explicit deterministic ranking — severity, then |effect size|, then n, then
``finding_id``. Determinism (US-J2 AC): records are processed in ball_id
order, cells in sorted zone order, and ``finding_id`` is a pure function of
``(seed, probe, metric, condition)`` — identical inputs always produce
byte-identical output, enabling regression snapshots.

Free prose NEVER originates here (US-J2 AC): every ``text_data`` string is
formatted from the module's fixed templates with enum values and recomputable
numbers; injected rule findings carry only their rule-authored ``text_data``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cricai_data.enums import FindingSeverity

from cricai_coaching.llm import canonical_payload

#: The agent name stamped on every finding this layer emits (contract #2).
AGENT_NAME = "analysis"

#: Keys every Finding dict must carry (pinned contract #2), plus exactly one
#: of ``rule_key`` (rule findings) or ``probe`` (pattern-probe findings).
FINDING_KEYS: frozenset[str] = frozenset(
    {
        "finding_id",
        "agent",
        "kind",
        "severity",
        "metric",
        "condition",
        "n",
        "effect_size",
        "confidence",
        "ball_ids",
        "evidence",
        "text_data",
    }
)

#: The one metric both probes contrast: control fraction in percent.
CONTROL_METRIC = "control_pct"

#: Fixed text templates — the ONLY source of probe finding prose (US-J2 AC).
ZONE_TEXT_TEMPLATE = "control {cell_pct}% on {length}/{line} vs {overall_pct}% overall ({n} balls)"
FATIGUE_TEXT_TEMPLATE = (
    "control {last_pct}% over the last {segment} balls vs {first_pct}% over the first {segment}"
)

_SEVERITY_RANK: dict[str, int] = {
    FindingSeverity.MAJOR.value: 2,
    FindingSeverity.MINOR.value: 1,
    FindingSeverity.INFO.value: 0,
}


class AnalysisError(ValueError):
    """Contract violation: malformed BallRecord input or injected finding."""


@dataclass(frozen=True)
class AnalysisConfig:
    """Probe discipline knobs (US-J2 AC defaults, documented here).

    ``min_sample`` is US-G2's no-conclusions-from-3-balls gate (default 10);
    ``effect_floor`` is the multiple-comparison effect-size floor on the
    control-fraction delta (0.15 = 15 percentage points); ``top_k`` caps the
    ranked output (max 5 findings); ``segment_size`` sizes the first/last
    fatigue segments (50 balls each, disjoint segments required).
    """

    min_sample: int = 10
    effect_floor: float = 0.15
    top_k: int = 5
    segment_size: int = 50


DEFAULT_ANALYSIS_CONFIG = AnalysisConfig()


def _check_schema(record: Mapping[str, Any]) -> None:
    """Reject unknown MAJOR schema versions (contract #1); absent = v1 era."""
    version = str(record.get("schema_version", "1.0"))
    major = version.split(".", maxsplit=1)[0]
    if major != "1":
        raise AnalysisError(f"unsupported BallRecord schema major version: {version!r}")


def _ordered_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Validate and order records by ball_id — explicit ordering, never input luck."""
    for record in records:
        _check_schema(record)
        if not isinstance(record.get("ball_id"), int):
            raise AnalysisError("BallRecord requires an integer ball_id")
    return sorted(records, key=lambda record: int(record["ball_id"]))


def _control(record: Mapping[str, Any]) -> bool | None:
    value = record.get("control")
    if value is None or isinstance(value, bool):
        return value
    raise AnalysisError(f"BallRecord control must be a boolean or null, got {value!r}")


def _control_rate(records: Sequence[Mapping[str, Any]]) -> tuple[float, list[Mapping[str, Any]]]:
    """(control fraction, contributing records) over non-null control values."""
    scored = [record for record in records if _control(record) is not None]
    if not scored:
        return 0.0, []
    return sum(1 for record in scored if record["control"]) / len(scored), scored


def _confidence(records: Sequence[Mapping[str, Any]]) -> float:
    """Mean per-ball confidence of the control field; absent fields count 1.0.

    Callers only build findings from non-empty scored samples (the min-n gate
    ran first), so the mean is always defined.
    """
    values = [float(record.get("confidence", {}).get("control", 1.0)) for record in records]
    return round(sum(values) / len(values), 4)


def _evidence(records: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, str]]:
    """ball_no -> {camera_id -> clip_id} for every contributing ball with clips."""
    return {
        int(record["ball_id"]): dict(record["clips"])
        for record in records
        if isinstance(record.get("clips"), Mapping) and record["clips"]
    }


def finding_id(seed: int, probe: str, metric: str, condition: Mapping[str, Any]) -> str:
    """Deterministic probe finding id: pure function of (seed, probe, condition)."""
    body = canonical_payload({"seed": seed, "probe": probe, "metric": metric, **dict(condition)})
    return f"an-{hashlib.sha256(body.encode()).hexdigest()[:16]}"


def _severity(effect: float, floor: float) -> str:
    """Negative control deltas are weaknesses; positive ones are strengths."""
    if effect > 0:
        return FindingSeverity.INFO.value
    if abs(effect) >= 2 * floor:
        return FindingSeverity.MAJOR.value
    return FindingSeverity.MINOR.value


@dataclass(frozen=True)
class _Candidate:
    """One probe hit that passed the gates, before it becomes a Finding dict."""

    probe: str
    condition: dict[str, Any]
    effect: float
    text: str
    scored: tuple[Mapping[str, Any], ...]
    n: int


def _finding_from(candidate: _Candidate, *, seed: int, floor: float) -> dict[str, Any]:
    return {
        "finding_id": finding_id(seed, candidate.probe, CONTROL_METRIC, candidate.condition),
        "agent": AGENT_NAME,
        "probe": candidate.probe,
        "kind": candidate.probe,
        "severity": _severity(candidate.effect, floor),
        "metric": CONTROL_METRIC,
        "condition": candidate.condition,
        "n": candidate.n,
        "effect_size": round(candidate.effect, 4),
        "confidence": _confidence(candidate.scored),
        "ball_ids": sorted(int(record["ball_id"]) for record in candidate.scored),
        "evidence": _evidence(candidate.scored),
        "text_data": {"summary": candidate.text},
    }


def _pct(rate: float) -> int:
    return round(rate * 100)


def _zone_contrast_probe(
    records: Sequence[Mapping[str, Any]], config: AnalysisConfig, seed: int
) -> list[dict[str, Any]]:
    """Per (line, length) cell control%% vs overall control%% (US-J2)."""
    overall_rate, overall_scored = _control_rate(records)
    if len(overall_scored) < config.min_sample:
        return []
    cells: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in overall_scored:
        line, length = record.get("line"), record.get("length")
        if isinstance(line, str) and isinstance(length, str):
            cells.setdefault((line, length), []).append(record)
    findings: list[dict[str, Any]] = []
    for line, length in sorted(cells):  # sorted cell order: no set/dict iteration luck
        scored = cells[(line, length)]
        if len(scored) < config.min_sample:
            continue  # min-n gate (US-G2)
        cell_rate, _ = _control_rate(scored)
        effect = cell_rate - overall_rate
        if abs(effect) < config.effect_floor:
            continue  # effect-size floor (multiple-comparison discipline)
        text = ZONE_TEXT_TEMPLATE.format(
            cell_pct=_pct(cell_rate),
            length=length,
            line=line,
            overall_pct=_pct(overall_rate),
            n=len(scored),
        )
        candidate = _Candidate(
            probe="zone_contrast",
            condition={"line": line, "length": length},
            effect=effect,
            text=text,
            scored=tuple(scored),
            n=len(scored),
        )
        findings.append(_finding_from(candidate, seed=seed, floor=config.effect_floor))
    return findings


def _fatigue_contrast_probe(
    records: Sequence[Mapping[str, Any]], config: AnalysisConfig, seed: int
) -> list[dict[str, Any]]:
    """First-``segment_size`` vs last-``segment_size`` control contrast (US-J2)."""
    if len(records) < 2 * config.segment_size:
        return []  # segments must be disjoint: no double-counted balls
    first_rate, first_scored = _control_rate(records[: config.segment_size])
    last_rate, last_scored = _control_rate(records[-config.segment_size :])
    if len(first_scored) < config.min_sample or len(last_scored) < config.min_sample:
        return []  # min-n gate applies per segment
    effect = last_rate - first_rate
    if abs(effect) < config.effect_floor:
        return []
    candidate = _Candidate(
        probe="fatigue_contrast",
        condition={"segment": f"last_{config.segment_size}_vs_first_{config.segment_size}"},
        effect=effect,
        text=FATIGUE_TEXT_TEMPLATE.format(
            last_pct=_pct(last_rate),
            first_pct=_pct(first_rate),
            segment=config.segment_size,
        ),
        scored=(*first_scored, *last_scored),
        n=len(first_scored) + len(last_scored),
    )
    return [_finding_from(candidate, seed=seed, floor=config.effect_floor)]


def validate_finding(finding: Mapping[str, Any]) -> None:
    """Reject contract-invalid Finding dicts (US-J1: contracts are enforced)."""
    missing = sorted(FINDING_KEYS - set(finding))
    if missing:
        raise AnalysisError(f"finding missing required keys: {missing}")
    has_rule = isinstance(finding.get("rule_key"), str)
    has_probe = isinstance(finding.get("probe"), str)
    if has_rule == has_probe:
        raise AnalysisError("finding must carry exactly one of rule_key or probe")
    if finding["severity"] not in _SEVERITY_RANK:
        raise AnalysisError(f"unknown finding severity: {finding['severity']!r}")


def rank_findings(findings: Sequence[Mapping[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
    """Deterministic top-k ranking: severity, |effect|, n, then finding_id.

    The ``finding_id`` tiebreak makes the order a pure function of content —
    no arrival-order or hash-order luck (US-J2 determinism AC).
    """

    def sort_key(finding: Mapping[str, Any]) -> tuple[int, float, int, str]:
        effect = finding["effect_size"]
        return (
            -_SEVERITY_RANK[str(finding["severity"])],
            -abs(float(effect)) if effect is not None else 0.0,
            -int(finding["n"]),
            str(finding["finding_id"]),
        )

    return [dict(finding) for finding in sorted(findings, key=sort_key)[:top_k]]


def run_analysis(
    records: Sequence[Mapping[str, Any]],
    *,
    rule_findings: Sequence[Mapping[str, Any]] = (),
    config: AnalysisConfig = DEFAULT_ANALYSIS_CONFIG,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Run the pattern probes and merge with injected rule-runner findings.

    ``rule_findings`` is story g1's runner output, injected as plain dicts
    (cross-story seam) and validated against contract #2 before merging —
    contract-invalid findings raise :class:`AnalysisError`, they are never
    silently dropped or repaired. The merged list is ranked and cut to
    ``config.top_k``; pure noise below the gates yields no probe findings.
    """
    for finding in rule_findings:
        validate_finding(finding)
    ordered = _ordered_records(records)
    probe_findings = [
        *_zone_contrast_probe(ordered, config, seed),
        *_fatigue_contrast_probe(ordered, config, seed),
    ]
    for finding in probe_findings:
        validate_finding(finding)  # this layer honors the same contract it enforces
    return rank_findings([*rule_findings, *probe_findings], top_k=config.top_k)
