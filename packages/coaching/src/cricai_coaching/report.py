"""US-G3: daily report — one correction, one drill, one measurable goal.

This module owns the pinned Report-body-v1 artifact (cross-group contract #4):

    {kind, period, main_correction: {finding_id, text, evidence},
     drill: {drill_id, text, machine_settings, success_metric},
     goal: {metric, target, condition}, secondary: [<=3], positive,
     safety: SafetyVerdict|null, honesty_banner: str|null,
     coverage_note: str|null, fatigue_note: {...}|null,
     claims: [{value, metric, recompute_key}]}

Everything here is pure: findings arrive as contract-#2 dicts, quality as the
contract-#6 dict, safety as the contract-#3 dict — all injected by the caller
(``cricai_worker.generate_report``). Wording comes only from rule-authored
``text_data`` strings plus deterministic templates; free prose never
originates here (US-J2 AC). Every number placed in wording is also listed in
``claims`` with a ``recompute_key`` so the publish-time validator and the IT
claim-recomputation test can recompute it from the database:

- ``finding:<finding_id>:n``        -> ``findings.n``
- ``finding:<finding_id>:<field>``  -> ``findings.payload[<field>]``
- ``drill:<drill_id>:ball_count``   -> ``drills.ball_count``

Honesty path (US-G3): when no finding meets the evidence bar — or the US-L4
quality score raises its thin-data banner — the report says so via
``honesty_banner`` and ships no correction/drill/goal. It never invents an
issue.
"""

from __future__ import annotations

import copy
import html
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from cricai_data.enums import FindingSeverity

from cricai_coaching.evidence import check_min_clips
from cricai_coaching.fatigue import FATIGUE_SUGGESTION
from cricai_coaching.workload import SplitReconciliation

#: US-G3 ranking: severity x frequency x trend. Majors dominate; a regressing
#: metric outranks an improving one at equal severity and frequency.
SEVERITY_WEIGHTS: dict[str, float] = {
    FindingSeverity.INFO.value: 1.0,
    FindingSeverity.MINOR.value: 2.0,
    FindingSeverity.MAJOR.value: 4.0,
}

#: Trend directions use the ProgressSnapshot vocabulary (contract #7).
TREND_WEIGHTS: dict[str, float] = {"regressing": 1.5, "flat": 1.0, "improving": 0.75}

#: Report lists at most 3 secondary observations (US-G3, collapsed by default).
MAX_SECONDARY = 3

#: Honesty path copy (US-G3: "clean session — keep the same plan").
HONESTY_CLEAN = "Clean session - no issue met the evidence bar today. Keep the same plan."

#: Deterministic fallbacks when rules author no explicit string. Lint-safe,
#: number-free (numbers would need claims), encouragement-toned.
DEFAULT_POSITIVE = "Great effort today - the work you put in shows."
DEFAULT_DRILL_TEXT = "Repeat the focus drill from your last plan on this correction."

#: US-H2 batting-split note: fixed, number-free producer copy. The plan-vs-actual
#: numbers ride as structured per-intent rows (claim-exempt structured data, like
#: the fatigue note's numeric components), so no number belongs in this wording.
BATTING_SPLIT_NOTE = (
    "Planned vs actual balls per block for the day. A block whose share drifts "
    "past the configured alert threshold is flagged; the fun block is protected."
)

#: Findings of this ``kind`` are "what went well" positives, never corrections.
POSITIVE_KIND = "positive"

#: Numberless "what went well" templates for probe strength findings (US-J2
#: encodes strengths as positive-effect INFO probes). Numberless by design:
#: positives carry no claims, so a number here would fail claims coverage.
STRENGTH_ZONE_POSITIVE = "Great control on {length}/{line} balls - keep that going."
STRENGTH_DEFAULT_POSITIVE = "Your control stood out as a strength today - keep that going."

#: Optional per-finding drill source injected by the caller (j2's planner maps
#: findings to library drills; the honest default derives from ``text_data``).
DrillResolver = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


class ReportWriter(Protocol):
    """Wording seam between g2 (assembly) and g3 (LLM writer) — contract #9.

    ``write`` takes the deterministic rule-based report body plus context
    ``{"findings": [...], "history": {...}, "rules": [...], "tone": str,
    "safety": SafetyVerdict-dict|None}`` and returns the same body shape with
    wording fields rewritten. A writer must never change numbers, keys, or
    the safety text, and raises on its own failure so the caller falls back
    to the rule-based writer (US-G4 deterministic fallback).
    """

    def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...


class RuleBasedWriter:
    """The default writer: pure deterministic wording, no LLM (US-G3 V1).

    Assembly already words every field from rule-authored ``text_data``
    strings and deterministic templates, so the rule-based pass is the
    identity on a defensive copy. It is always available, which is exactly
    what makes it the safe fallback (US-G4: LLM unavailable => report ships).
    """

    def write(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context  # protocol parity: rule-based wording is a function of the body alone
        return copy.deepcopy(body)


def format_number(value: float) -> str:
    """Canonical wording form of a claim number (``57``, ``0.57``, ``81.5``)."""
    return f"{value:g}"


def extract_numbers(text: str) -> list[str]:
    """Every number literal in a wording string, canonically formatted."""
    return [format_number(float(match)) for match in _NUMBER_RE.findall(text)]


def finding_score(finding: Mapping[str, Any], *, trends: Mapping[str, str] | None = None) -> float:
    """US-G3 impact score: severity weight x matched-ball count x trend weight."""
    severity = SEVERITY_WEIGHTS.get(str(finding.get("severity")), 1.0)
    frequency = max(int(finding.get("n", 0)), 1)
    direction = (trends or {}).get(str(finding.get("metric")), "flat")
    return severity * frequency * TREND_WEIGHTS.get(direction, 1.0)


def rank_findings(
    findings: Sequence[Mapping[str, Any]], *, trends: Mapping[str, str] | None = None
) -> list[Mapping[str, Any]]:
    """Highest-impact first; ties broken by ``finding_id`` for determinism."""
    return sorted(
        findings,
        key=lambda f: (-finding_score(f, trends=trends), str(f["finding_id"])),
    )


def is_strength(finding: Mapping[str, Any]) -> bool:
    """A probe finding with a positive effect is a strength, never a correction.

    Probes (contract #2: no ``rule_key``) encode strengths as positive control
    deltas (severity INFO); rule findings are excluded because their
    ``effect_size`` is a hit *share* — always positive, never a strength
    signal. Headlining a strength would invent an issue (US-G3 SAF).
    """
    rule_key = finding.get("rule_key")
    if isinstance(rule_key, str) and rule_key.strip():
        return False
    effect = finding.get("effect_size")
    return isinstance(effect, int | float) and not isinstance(effect, bool) and effect > 0


def _text_data(finding: Mapping[str, Any]) -> Mapping[str, Any]:
    data: Mapping[str, Any] = finding.get("text_data", {})
    return data


def _correction_text(finding: Mapping[str, Any]) -> str:
    """Rule-authored correction string; deterministic fallback names the metric."""
    authored = _text_data(finding).get("correction")
    if authored is not None:
        return str(authored)
    return f"Watch {finding['metric']} in the marked zone."


def _claim(value: float, metric: str, recompute_key: str) -> dict[str, Any]:
    return {"value": value, "metric": metric, "recompute_key": recompute_key}


def _main_correction(finding: Mapping[str, Any], claims: list[dict[str, Any]]) -> dict[str, Any]:
    finding_id = str(finding["finding_id"])
    n = int(finding["n"])
    text = f"{_correction_text(finding)} Seen on {n} balls."
    claims.append(_claim(n, "ball_count", f"finding:{finding_id}:n"))
    payload: Mapping[str, Any] = finding.get("payload", {})
    value = payload.get("value")
    if isinstance(value, int | float):
        text += f" Your {finding['metric']} measured {format_number(value)}."
        claims.append(_claim(float(value), str(finding["metric"]), f"finding:{finding_id}:value"))
    return {"finding_id": finding_id, "text": text, "evidence": finding.get("evidence", {})}


def _resolve_drill(
    finding: Mapping[str, Any],
    drill_for: DrillResolver | None,
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    """One drill for tomorrow: injected library drill, else rule-authored text."""
    if drill_for is not None:
        drill = drill_for(finding)
        if drill is not None:
            drill_id = str(drill["drill_id"])
            text = str(drill["text"])
            ball_count = drill.get("ball_count")
            if isinstance(ball_count, int):
                text += f" Do {ball_count} balls."
                claims.append(_claim(ball_count, "drill_balls", f"drill:{drill_id}:ball_count"))
            return {
                "drill_id": drill_id,
                "text": text,
                "machine_settings": dict(drill.get("machine_settings", {})),
                "success_metric": str(drill.get("success_metric", finding["metric"])),
            }
    authored = _text_data(finding).get("drill")
    return {
        "drill_id": None,
        "text": str(authored) if authored is not None else DEFAULT_DRILL_TEXT,
        "machine_settings": {},
        "success_metric": str(finding["metric"]),
    }


def _goal(finding: Mapping[str, Any], claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Measurable goal: metric + target + condition, testable next session.

    The target comes from the rule's payload (``target``, else ``threshold``)
    so the claim recomputes from ``findings.payload`` — never invented here.
    """
    finding_id = str(finding["finding_id"])
    payload: Mapping[str, Any] = finding.get("payload", {})
    target: float | None = None
    for field in ("target", "threshold"):
        candidate = payload.get(field)
        if isinstance(candidate, int | float):
            target = float(candidate)
            claims.append(_claim(target, str(finding["metric"]), f"finding:{finding_id}:{field}"))
            break
    return {
        "metric": str(finding["metric"]),
        "target": target,
        "condition": dict(finding.get("condition", {})),
    }


def _secondary(
    findings: Sequence[Mapping[str, Any]], claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for finding in findings[:MAX_SECONDARY]:
        finding_id = str(finding["finding_id"])
        n = int(finding["n"])
        claims.append(_claim(n, "ball_count", f"finding:{finding_id}:n"))
        items.append(
            {
                "finding_id": finding_id,
                "text": f"{_correction_text(finding)} ({n} balls.)",
                "evidence": finding.get("evidence", {}),
            }
        )
    return items


def _strength_positive(finding: Mapping[str, Any]) -> str:
    """Numberless positive line from a strength probe's zone context.

    Numbers are deliberately absent: the probe's ``text_data`` summary carries
    percentages no claim covers, so wording built from it would fail the
    claims-coverage gate. Zone strengths name their cell; anything else (e.g.
    the fatigue contrast, whose condition embeds segment sizes) uses the
    generic template.
    """
    condition: Mapping[str, Any] = finding.get("condition", {})
    line, length = condition.get("line"), condition.get("length")
    if isinstance(line, str) and isinstance(length, str):
        return STRENGTH_ZONE_POSITIVE.format(length=length, line=line)
    return STRENGTH_DEFAULT_POSITIVE


def _positive(findings: Sequence[Mapping[str, Any]]) -> str:
    """One "what went well": the top-ranked positive-kind finding, else the
    strongest positive-effect probe strength, else the default."""
    for finding in findings:
        if str(finding.get("kind")) == POSITIVE_KIND:
            authored = _text_data(finding).get("positive")
            return str(authored) if authored is not None else _correction_text(finding)
    strengths = sorted(
        (f for f in findings if is_strength(f)),
        key=lambda f: (-float(f["effect_size"]), str(f["finding_id"])),
    )
    if strengths:
        return _strength_positive(strengths[0])
    return DEFAULT_POSITIVE


def _fatigue_note(note: Mapping[str, Any]) -> dict[str, Any]:
    """US-H3 report note from the fatigue scorer's output (finding [53]).

    The wording is the number-free :data:`FATIGUE_SUGGESTION`; the numeric
    components (window, control drop, degrading metrics) ride as structured,
    inspectable fields so no claim is needed and nothing recomputable is
    invented in prose.
    """
    return {
        "text": FATIGUE_SUGGESTION,
        "window": int(note["window"]),
        "control_drop_points": float(note["control_drop_points"]),
        "degrading_metrics": [str(signal["metric"]) for signal in note["degrading_signals"]],
    }


def batting_split_block(reconciliation: SplitReconciliation) -> dict[str, Any]:
    """Serialize a US-H2 :class:`SplitReconciliation` into the additive report block.

    Pure structured data — per-intent planned/actual/deviation/flag rows, the
    day totals, the flagged intents and the kid-first fun-block predicate —
    under the fixed number-free :data:`BATTING_SPLIT_NOTE`. Like the bowling
    section and the fatigue note's numeric components, every number here rides
    as inspectable structured data, so it carries no ``claims`` entry and is
    never reworded by the writer (US-G4 guard treats it as structural).
    """
    return {
        "intents": [
            {
                "intent": row.intent,
                "planned_balls": row.planned_balls,
                "actual_balls": row.actual_balls,
                "deviation_pct": row.deviation_pct,
                "flagged": row.flagged,
            }
            for row in reconciliation.intents
        ],
        "planned_total": reconciliation.planned_total,
        "actual_total": reconciliation.actual_total,
        "flagged_intents": list(reconciliation.flagged_intents),
        "fun_block_intact": reconciliation.fun_block_intact,
        "note": BATTING_SPLIT_NOTE,
    }


def _period(period_start: Any, period_end: Any) -> dict[str, str]:
    return {"start": str(period_start), "end": str(period_end)}


@dataclass(frozen=True)
class ReportSources:
    """Cross-story inputs to assembly, each with an honest empty default.

    ``trends`` — metric -> direction from the progress agent (contract #7
    vocabulary); ``quality`` — the US-L4 QualityScore (contract #6);
    ``safety`` — the SafetyVerdict (contract #3), inserted verbatim;
    ``drill_for`` — the planner's finding->drill mapping (US-J3 seam);
    ``fatigue`` — the US-H3 fatigue scorer's ``note`` (or None when the
    session was not fatigued / not evaluated); ``coverage_note`` — a short
    day-scoped honesty note (finding: multi-session day where one session
    degraded) that ships ABOVE the corrections when sibling sessions still
    produced findings, so the day's real corrections are never suppressed by
    one session's failure. Absent collaborators simply mean "no data", never
    fabricated values.
    """

    trends: Mapping[str, str] | None = None
    quality: Mapping[str, Any] | None = None
    safety: Mapping[str, Any] | None = None
    drill_for: DrillResolver | None = None
    fatigue: Mapping[str, Any] | None = None
    coverage_note: str | None = None


def assemble_report_body(
    *,
    kind: str,
    period_start: Any,
    period_end: Any,
    findings: Sequence[Mapping[str, Any]],
    sources: ReportSources | None = None,
) -> dict[str, Any]:
    """Assemble Report-body-v1 (contract #4) from contract-#2 findings.

    A non-null quality banner means the session's data is too thin to trust,
    so the honesty path runs instead of forcing findings. The safety verdict
    is inserted verbatim — its integrity is hash-verified downstream, so its
    text is exempt from the claims-coverage rule (it is not ours to reword).
    """
    src = sources if sources is not None else ReportSources()
    ranked = rank_findings(findings, trends=src.trends)
    positive = _positive(ranked)
    # Strengths (positive-effect probes) route to ``positive`` above; they are
    # never corrections, so headlining one would invent an issue (US-G3 SAF).
    corrections = [
        f
        for f in ranked
        if str(f.get("kind")) != POSITIVE_KIND
        and not is_strength(f)
        and check_min_clips(f).sufficient
    ]
    quality_banner = src.quality.get("banner") if src.quality is not None else None

    claims: list[dict[str, Any]] = []
    body: dict[str, Any] = {
        "kind": kind,
        "period": _period(period_start, period_end),
        "main_correction": None,
        "drill": None,
        "goal": None,
        "secondary": [],
        "positive": positive,
        "safety": copy.deepcopy(dict(src.safety)) if src.safety is not None else None,
        "honesty_banner": None,
        "coverage_note": src.coverage_note,
        "claims": claims,
        "fatigue_note": None,
    }

    if quality_banner is not None:
        # Thin/untrustworthy data: no findings AND no fatigue claim ship.
        body["honesty_banner"] = str(quality_banner)
        return body
    # Fatigue is honest, correction-independent info: it rides on the clean
    # session path too (a session can tire a player without any rule firing).
    if src.fatigue is not None:
        body["fatigue_note"] = _fatigue_note(src.fatigue)
    if not corrections:
        body["honesty_banner"] = HONESTY_CLEAN
        return body

    main, *rest = corrections
    body["main_correction"] = _main_correction(main, claims)
    body["drill"] = _resolve_drill(main, src.drill_for, claims)
    body["goal"] = _goal(main, claims)
    body["secondary"] = _secondary(rest, claims)
    return body


def wording_texts(body: Mapping[str, Any]) -> list[str]:
    """Every wording field the claims-coverage rule applies to.

    The safety text (hash-verified verbatim, US-H5) and the honesty banner
    (verbatim from US-L4 quality) are third-party inserts, not our wording.
    """
    texts: list[str] = []
    main = body.get("main_correction")
    if main is not None:
        texts.append(str(main["text"]))
    drill = body.get("drill")
    if drill is not None:
        texts.append(str(drill["text"]))
    texts.extend(str(item["text"]) for item in body.get("secondary", []))
    fatigue = body.get("fatigue_note")
    if fatigue is not None:
        texts.append(str(fatigue["text"]))
    coverage_note = body.get("coverage_note")
    if coverage_note is not None:
        texts.append(str(coverage_note))
    positive = body.get("positive")
    if positive is not None:
        texts.append(str(positive))
    return texts


def _claimed_number(claim: Any) -> str | None:
    """Canonical form of a well-formed claim's value, else None.

    Malformed claim entries never *widen* coverage: a non-mapping, a missing
    or non-numeric ``value`` (or a bool) contributes nothing to the claimed
    set, so a number in wording it might have "covered" is still flagged.
    """
    if isinstance(claim, Mapping):
        value = claim.get("value")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return format_number(float(value))
    return None


def validate_claims_coverage(body: Mapping[str, Any]) -> list[str]:
    """US-G3 gate: every number in wording appears in ``claims``.

    Returns the offending number literals (empty = compliant). The publish
    validator rejects any body where wording carries a number the claims
    ledger cannot recompute; the LLM writer runs this exact function as its
    final acceptance check, so a wording it accepts is one this gate accepts.
    """
    claimed = {n for n in map(_claimed_number, body.get("claims", [])) if n is not None}
    missing: list[str] = []
    for text in wording_texts(body):
        missing.extend(n for n in extract_numbers(text) if n not in claimed)
    return missing


def _html_section(title: str, inner: str) -> str:
    return f"<section><h2>{html.escape(title)}</h2>{inner}</section>"


def _html_paragraph(text: str) -> str:
    return f"<p>{html.escape(text)}</p>"


def _html_evidence(evidence: Mapping[str, Mapping[str, str]]) -> str:
    items = "".join(
        f"<li>ball {html.escape(str(ball))} - {html.escape(str(cam))}: "
        f"<code>{html.escape(str(evidence[ball][cam]))}</code></li>"
        for ball in sorted(evidence, key=str)
        for cam in sorted(evidence[ball])
    )
    return f'<ul class="evidence">{items}</ul>' if items else ""


def _cell_text(value: Any) -> str:
    """Table-cell text for a structured data value: numbers canonical, None an
    honest dash (a 1-ball sigma or an uncalled percentage is null, never 0)."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        return format_number(float(value))
    return str(value)


def _html_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(header))}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_cell_text(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _html_note(note: Any) -> str:
    """The producer's fixed digit-free honesty note under a data block."""
    return f'<p class="note">{html.escape(str(note))}</p>' if note is not None else ""


def _html_bowling_workload(workload: Mapping[str, Any] | None) -> str:
    """US-I7 AC: the week-to-date workload vs the US-H1 ceiling ALWAYS shows —
    a null block renders an honest absence, never disappears silently."""
    if workload is None:
        return _html_section(
            "Bowling workload",
            _html_paragraph("Workload data is not available for this session."),
        )
    window: Mapping[str, Any] = workload.get("window") or {}
    inner = _html_table(
        ["window start", "window end", "weighted overs", "ceiling overs", "remaining balls"],
        [
            [
                window.get("start"),
                window.get("end"),
                workload.get("weighted_overs"),
                workload.get("ceiling_overs"),
                workload.get("remaining_balls"),
            ]
        ],
    )
    violations = [str(code) for code in workload.get("violations") or []]
    if violations:
        items = "".join(f"<li>{html.escape(code)}</li>" for code in violations)
        inner += f'<ul class="workload-violations">{items}</ul>'
    return _html_section("Bowling workload", inner)


def _html_bowling_scorecard(block: Mapping[str, Any]) -> str:
    """US-I4 scorecard: hit counts/shares with the denominator shown honestly."""
    overall: Mapping[str, Any] = block["overall"]
    rows: list[Sequence[Any]] = [
        ["overall", overall.get("hits"), overall.get("n"), overall.get("pct")]
    ]
    rows += [
        [cell.get("variation"), cell.get("hits"), cell.get("n"), cell.get("pct")]
        for cell in block.get("by_variation", [])
    ]
    inner = _html_table(["variation", "hits", "balls", "hit %"], rows)
    inner += _html_paragraph(
        f"Counted {_cell_text(block.get('counted'))} of {_cell_text(block.get('total'))} "
        "deliveries."
    )
    inner += _html_note(block.get("note"))
    return _html_section("Accuracy scorecard", inner)


def _html_bowling_scatter(block: Mapping[str, Any]) -> str:
    """US-I3 release-scatter summary: mean/1-sigma/min/max, overall + per variation."""
    rows: list[Sequence[Any]] = [
        [
            "overall",
            block.get("n"),
            block.get("mean_cm"),
            block.get("sigma_cm"),
            block.get("min_cm"),
            block.get("max_cm"),
        ]
    ]
    rows += [
        [
            cell.get("variation"),
            cell.get("n"),
            cell.get("mean_cm"),
            cell.get("sigma_cm"),
            None,
            None,
        ]
        for cell in block.get("by_variation", [])
    ]
    inner = _html_table(["variation", "balls", "mean cm", "sigma cm", "min cm", "max cm"], rows)
    inner += _html_note(block.get("note"))
    return _html_section("Release scatter", inner)


def _html_bowling_agreement(block: Mapping[str, Any]) -> str:
    """US-I6 intent-vs-detected matrix with its honest denominators."""
    inner = _html_table(
        ["labeled", "compared", "unclear", "agreement %"],
        [
            [
                block.get("labeled"),
                block.get("compared"),
                block.get("unclear"),
                block.get("agreement_pct"),
            ]
        ],
    )
    inner += _html_table(
        ["intent", "detected", "count"],
        [
            [cell.get("intent"), cell.get("detected"), cell.get("count")]
            for cell in block.get("matrix", [])
        ],
    )
    inner += _html_note(block.get("note"))
    return _html_section("Variation agreement", inner)


def _html_bowling_modules(modules: Sequence[Mapping[str, Any]]) -> str:
    """US-I7 Warne/Saqlain modules: coach-approved principle/lesson/drill text."""
    if not modules:
        return ""
    parts: list[str] = []
    for module in modules:
        parts.append(
            f"<h3>{html.escape(str(module.get('title')))} - "
            f"{html.escape(str(module.get('legend')))}</h3>"
        )
        for text_key in ("principle", "lesson", "drill"):
            value = module.get(text_key)
            if value is not None:
                parts.append(_html_paragraph(str(value)))
    return _html_section("Learning modules", "".join(parts))


def _html_batting_split(block: Mapping[str, Any]) -> str:
    """US-H2 plan-vs-actual: the per-intent planned/actual/deviation/flag table
    plus day totals and the kid-first fun-block predicate.

    Pure data render like the bowling blocks: numbers come from the structured
    ``batting_split`` block (claim-exempt), prose is the producer's fixed note.
    """
    rows: list[Sequence[Any]] = [
        [
            row.get("intent"),
            row.get("planned_balls"),
            row.get("actual_balls"),
            row.get("deviation_pct"),
            row.get("flagged"),
        ]
        for row in block.get("intents", [])
    ]
    inner = _html_table(["block", "planned balls", "actual balls", "deviation %", "flagged"], rows)
    inner += _html_paragraph(
        f"Planned {_cell_text(block.get('planned_total'))} balls; "
        f"{_cell_text(block.get('actual_total'))} logged; "
        f"fun block intact: {_cell_text(block.get('fun_block_intact'))}."
    )
    inner += _html_note(block.get("note"))
    return _html_section("Plan vs actual", inner)


def _html_bowling_section(bowling: Mapping[str, Any]) -> str:
    """US-I7/K5 (finding [46/55]): the leg-spin data blocks on the printable
    surface — scorecard, release scatter summary, agreement matrix, learning
    modules and the always-present workload-vs-ceiling view.

    Pure data render: every number comes from the structured ``bowling``
    blocks (claim-exempt structured data, like ``fatigue_note``'s numeric
    components); all prose is the producer's fixed digit-free notes.
    """
    return (
        _html_bowling_workload(bowling.get("workload"))
        + _html_bowling_scorecard(bowling["accuracy_scorecard"])
        + _html_bowling_scatter(bowling["release_scatter"])
        + _html_bowling_agreement(bowling["variation_agreement"])
        + _html_bowling_modules(bowling.get("learning_modules", []))
    )


def render_report_html(body: Mapping[str, Any]) -> str:
    """Printable HTML for the net wall — a pure function over the body JSON.

    No I/O, no clock, no styling dependencies: identical bodies render
    identical markup (the Phase-6 dashboard/PDF path reuses the same body).
    """
    parts: list[str] = [f'<article class="report" data-kind="{html.escape(str(body["kind"]))}">']
    period = body["period"]
    parts.append(
        f"<header><h1>{html.escape(str(body['kind']).title())} report</h1>"
        f"<p>{html.escape(str(period['start']))} to {html.escape(str(period['end']))}</p></header>"
    )
    # Safety first, always: an active warning precedes even the "clean session"
    # honesty banner so a child never reads reassurance above a safety block.
    safety = body.get("safety")
    if safety is not None and safety.get("active"):
        parts.append(_html_section("Safety first", _html_paragraph(str(safety["text"]))))
    banner = body.get("honesty_banner")
    if banner is not None:
        parts.append(f'<p class="honesty-banner">{html.escape(str(banner))}</p>')
    # Day-scoped coverage note: sits with the banner, above the corrections, so a
    # partly-degraded multi-session day still ships its real findings under a
    # short honest caveat rather than suppressing everything.
    coverage_note = body.get("coverage_note")
    if coverage_note is not None:
        parts.append(f'<p class="coverage-note">{html.escape(str(coverage_note))}</p>')
    main = body.get("main_correction")
    if main is not None:
        parts.append(
            _html_section(
                "Main correction",
                _html_paragraph(str(main["text"])) + _html_evidence(main["evidence"]),
            )
        )
    drill = body.get("drill")
    if drill is not None:
        parts.append(_html_section("Tomorrow's drill", _html_paragraph(str(drill["text"]))))
    goal = body.get("goal")
    if goal is not None:
        target = goal["target"]
        target_text = format_number(float(target)) if target is not None else "coach-set"
        parts.append(
            _html_section(
                "Goal",
                _html_paragraph(f"{goal['metric']}: reach {target_text} next session."),
            )
        )
    fatigue = body.get("fatigue_note")
    if fatigue is not None:
        parts.append(_html_section("Fatigue check", _html_paragraph(str(fatigue["text"]))))
    # US-H2 plan-vs-actual: a batting/MIXED day with tagged blocks ships the
    # additive split table; absent on bowling days and block-less days.
    batting_split = body.get("batting_split")
    if batting_split is not None:
        parts.append(_html_batting_split(batting_split))
    # US-I7 leg-spin section (finding [46/55]): a bowling body's data blocks
    # ship on every human-visible surface — this renderer feeds /reports/{id}/html
    # and the PDF/PNG export, so the net-wall print carries them too (US-K5).
    bowling = body.get("bowling")
    if bowling is not None:
        parts.append(_html_bowling_section(bowling))
    secondary = body.get("secondary", [])
    if secondary:
        items = "".join(
            f"<li>{html.escape(str(item['text']))}{_html_evidence(item['evidence'])}</li>"
            for item in secondary
        )
        parts.append(
            f'<details class="secondary"><summary>Also worth a look</summary>'
            f"<ul>{items}</ul></details>"
        )
    parts.append(_html_section("What went well", _html_paragraph(str(body["positive"]))))
    parts.append("</article>")
    return "".join(parts)
