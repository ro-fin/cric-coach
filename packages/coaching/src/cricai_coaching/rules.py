"""Coach-approved coaching-rule DSL and runner (US-G2): rules are data, not code.

A rule ``definition`` is JSON with the pinned keys::

    {
      "metric":  <BallRecord field>,          # what is measured
      "op":      "lt"|"le"|"gt"|"ge"|"eq",    # per-ball comparison
      "value":   <threshold / expected value>,
      "condition": {field: [allowed values]}, # zone/context filters
      "min_n":   int >= 1,                    # minimum-sample gate (default 10)
      "severity": "info"|"minor"|"major",
      "text_data": {name: rule-authored string},
      "trigger_share": float in (0, 1]        # optional, default 0.5
    }

Semantics: over the session's canonical BallRecords (US-G1), a ball *matches*
when it passes every ``condition`` filter and its ``metric`` is non-null; a
matched ball is a *hit* when ``metric OP value`` holds. The rule fires only
when matched balls >= ``min_n`` (no conclusions from 3 balls) AND the hit
share >= ``trigger_share``. Fired rules emit Finding-shaped dicts carrying the
matched count, aggregate, threshold and the exact hit ball IDs as evidence —
free prose comes ONLY from rule-authored ``text_data`` (pinned contract #2),
and every text passes the age-appropriateness content lint at parse time.

Per-player :class:`~cricai_data.models.RuleOverride` rows either ``disable`` a
rule or ``adjust`` its parameters; adjusted definitions are re-validated so an
override can never smuggle in a malformed rule.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cricai_data.enums import (
    BowlerSource,
    FindingSeverity,
    Footwork,
    Length,
    Line,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import CoachingRule, RuleOverride

from cricai_coaching.content_lint import assert_kid_safe
from cricai_coaching.llm import canonical_payload

#: Comparison operators the DSL speaks. Ordering ops require numeric metrics;
#: ``eq`` requires categorical/boolean metrics (float equality is a footgun).
RULE_OPS: tuple[str, ...] = ("lt", "le", "gt", "ge", "eq")

#: Numeric BallRecord metrics (aggregate reported as the mean over matches).
NUMERIC_METRICS: frozenset[str] = frozenset(
    {"speed_kph", "front_foot_direction_cm", "head_stability_score"}
)

#: Categorical metrics and their vocabularies; ``None`` = open string
#: vocabulary (producer versions may extend the classes, e.g. bat_path).
CATEGORICAL_METRICS: dict[str, frozenset[str] | None] = {
    "line": frozenset(m.value for m in Line),
    "length": frozenset(m.value for m in Length),
    "shot": frozenset(m.value for m in Shot),
    "footwork": frozenset(m.value for m in Footwork),
    "outcome": frozenset(m.value for m in Outcome),
    "bowler": frozenset(m.value for m in BowlerSource),
    "mode": frozenset({SessionType.BATTING.value, SessionType.BOWLING.value}),
    "bat_path": None,
    "contact_quality": None,
}

#: Boolean metrics (``eq`` against true/false).
BOOLEAN_METRICS: frozenset[str] = frozenset({"control"})

#: Every metric the DSL may reference.
KNOWN_METRICS: frozenset[str] = NUMERIC_METRICS | frozenset(CATEGORICAL_METRICS) | BOOLEAN_METRICS

#: Fields allowed inside ``condition`` zone/context filters.
CONDITION_FIELDS: frozenset[str] = frozenset(CATEGORICAL_METRICS) | BOOLEAN_METRICS

#: US-G2 AC: a finding fires only on >= this many balls unless the rule says more.
DEFAULT_MIN_N = 10

#: Default share of matched balls that must hit before the rule fires.
DEFAULT_TRIGGER_SHARE = 0.5

_REQUIRED_KEYS = frozenset({"metric", "op", "value", "condition", "min_n", "severity", "text_data"})
_ALLOWED_KEYS = _REQUIRED_KEYS | {"trigger_share"}
_OPTIONAL_DEFAULTS: dict[str, Any] = {"min_n": DEFAULT_MIN_N, "condition": {}}


class RuleError(ValueError):
    """A rule definition (or an override adjustment of one) is invalid."""


@dataclass(frozen=True)
class ParsedRule:
    """One validated rule definition, ready to run."""

    metric: str
    op: str
    value: float | str | bool
    condition: dict[str, tuple[Any, ...]]
    min_n: int
    severity: str
    text_data: dict[str, str]
    trigger_share: float


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _validate_condition_value(field: str, value: Any) -> None:
    if field in BOOLEAN_METRICS:
        if not isinstance(value, bool):
            raise RuleError(f"condition {field!r} values must be booleans, got {value!r}")
        return
    vocabulary = CATEGORICAL_METRICS[field]
    if not isinstance(value, str) or not value:
        raise RuleError(f"condition {field!r} values must be non-empty strings, got {value!r}")
    if vocabulary is not None and value not in vocabulary:
        raise RuleError(f"unknown {field!r} condition value {value!r}")


def _parse_condition(condition: Any) -> dict[str, tuple[Any, ...]]:
    if not isinstance(condition, Mapping):
        raise RuleError(f"condition must be a mapping of field -> allowed values: {condition!r}")
    parsed: dict[str, tuple[Any, ...]] = {}
    for field, values in condition.items():
        if field not in CONDITION_FIELDS:
            raise RuleError(f"unknown condition field {field!r} (allowed: BallRecord zones)")
        if not isinstance(values, list) or not values:
            raise RuleError(f"condition {field!r} must list at least one allowed value")
        for value in values:
            _validate_condition_value(field, value)
        parsed[field] = tuple(values)
    return parsed


def _validate_op_and_value(metric: str, op: str, value: Any) -> float | str | bool:
    if op not in RULE_OPS:
        raise RuleError(f"unknown op {op!r} (allowed: {', '.join(RULE_OPS)})")
    if metric in NUMERIC_METRICS:
        if op == "eq":
            raise RuleError(f"op 'eq' is not allowed on numeric metric {metric!r}")
        if not _is_number(value):
            raise RuleError(f"metric {metric!r} needs a numeric threshold, got {value!r}")
        return float(value)
    if op != "eq":
        raise RuleError(f"op {op!r} needs a numeric metric; {metric!r} only supports 'eq'")
    if metric in BOOLEAN_METRICS:
        if not isinstance(value, bool):
            raise RuleError(f"metric {metric!r} needs a boolean value, got {value!r}")
        return value
    _validate_condition_value(metric, value)
    return str(value)


def _parse_text_data(text_data: Any) -> dict[str, str]:
    if not isinstance(text_data, Mapping) or not text_data:
        raise RuleError("text_data must be a non-empty mapping of name -> string")
    parsed: dict[str, str] = {}
    for name, text in text_data.items():
        if not isinstance(name, str) or not isinstance(text, str) or not text.strip():
            raise RuleError(f"text_data entry {name!r} must be a non-empty string")
        try:
            assert_kid_safe(text)
        except ValueError as exc:  # content lint is release-gating (US-G2 SAF)
            raise RuleError(f"text_data[{name!r}] failed the content lint: {exc}") from exc
        parsed[name] = text
    return parsed


def _parse_gates(definition: Mapping[str, Any]) -> tuple[int, float]:
    min_n = definition["min_n"]
    if not isinstance(min_n, int) or isinstance(min_n, bool) or min_n < 1:
        raise RuleError(f"min_n must be an integer >= 1, got {min_n!r}")
    trigger_share = definition.get("trigger_share", DEFAULT_TRIGGER_SHARE)
    if not _is_number(trigger_share) or not 0.0 < float(trigger_share) <= 1.0:
        raise RuleError(f"trigger_share must be a number in (0, 1], got {trigger_share!r}")
    return min_n, float(trigger_share)


def parse_rule_definition(definition: Mapping[str, Any]) -> ParsedRule:
    """Validate one rule definition; loud :class:`RuleError` on anything off.

    Rejects unknown metrics, unknown ops, op/metric type mismatches, malformed
    conditions and gates, and any ``text_data`` string that fails the
    age-appropriateness content lint (US-G2 AC).
    """
    if not isinstance(definition, Mapping):
        raise RuleError(f"definition must be a mapping, got {type(definition).__name__}")
    filled = {**_OPTIONAL_DEFAULTS, **definition}
    unknown = set(filled) - _ALLOWED_KEYS
    if unknown:
        raise RuleError(f"unknown definition keys: {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(filled)
    if missing:
        raise RuleError(f"missing definition keys: {sorted(missing)}")
    metric = filled["metric"]
    if metric not in KNOWN_METRICS:
        raise RuleError(f"unknown metric {metric!r} (allowed: {sorted(KNOWN_METRICS)})")
    value = _validate_op_and_value(metric, filled["op"], filled["value"])
    severity = filled["severity"]
    if severity not in {s.value for s in FindingSeverity}:
        raise RuleError(f"unknown severity {severity!r}")
    min_n, trigger_share = _parse_gates(filled)
    return ParsedRule(
        metric=metric,
        op=filled["op"],
        value=value,
        condition=_parse_condition(filled["condition"]),
        min_n=min_n,
        severity=severity,
        text_data=_parse_text_data(filled["text_data"]),
        trigger_share=trigger_share,
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def finding_id(
    rule_key: str, version: int, metric: str, condition: Mapping[str, Any], ball_ids: Sequence[int]
) -> str:
    """Deterministic rule finding id (mirrors ``analysis_agent.finding_id``).

    A pure function of the rule identity (key + version), the metric, the
    condition and the exact hit ball ids — so the same session and data always
    produce the same id (US-G2 regression snapshots), while different hit balls
    or a bumped rule version produce a different one.
    """
    body = canonical_payload(
        {
            "rule_key": rule_key,
            "version": version,
            "metric": metric,
            "condition": {field: list(values) for field, values in sorted(condition.items())},
            "ball_ids": sorted(ball_ids),
        }
    )
    return f"ru-{hashlib.sha256(body.encode()).hexdigest()[:16]}"


def _matches(rule: ParsedRule, record: Mapping[str, Any]) -> bool:
    return all(record.get(field) in allowed for field, allowed in rule.condition.items())


def _hit(rule: ParsedRule, observed: Any) -> bool:
    if rule.op == "eq":
        return bool(observed == rule.value)
    threshold = float(rule.value)
    number = float(observed)
    if rule.op == "lt":
        return number < threshold
    if rule.op == "le":
        return number <= threshold
    if rule.op == "gt":
        return number > threshold
    return number >= threshold  # ge


def _aggregate(rule: ParsedRule, matched: Sequence[Mapping[str, Any]], share: float) -> float:
    """The reported metric aggregate: mean for numeric metrics, hit share else."""
    if rule.metric in NUMERIC_METRICS:
        return statistics.fmean(float(r[rule.metric]) for r in matched)
    return share


def _confidence(rule: ParsedRule, matched: Sequence[Mapping[str, Any]]) -> float:
    """Mean stored confidence of the metric across matched balls (manual = 1.0)."""
    values = [float(r.get("confidence", {}).get(rule.metric, 1.0)) for r in matched]
    return statistics.fmean(values)


def evaluate_rule(
    rule_key: str,
    rule: ParsedRule,
    records: Sequence[Mapping[str, Any]],
    *,
    version: int = 1,
    agent: str = "rules",
) -> dict[str, Any] | None:
    """Run one parsed rule over a session's BallRecords.

    Returns a Finding-shaped dict (pinned contract #2) when the rule fires,
    else ``None``. ``n`` is the matched-ball denominator, ``ball_ids`` are the
    exact hit balls, ``evidence`` maps each hit ball to its per-camera clips,
    and ``payload`` carries the aggregate/threshold/share the report layer
    recomputes claims from (US-G3).
    """
    matched = [r for r in records if _matches(rule, r) and r.get(rule.metric) is not None]
    n = len(matched)
    if n < rule.min_n:  # US-G2: no conclusions from 3 balls
        return None
    hits = [r for r in matched if _hit(rule, r[rule.metric])]
    share = len(hits) / n
    if share < rule.trigger_share:
        return None
    hit_ball_ids = [int(r["ball_id"]) for r in hits]
    return {
        "finding_id": finding_id(rule_key, version, rule.metric, rule.condition, hit_ball_ids),
        "agent": agent,
        "rule_key": rule_key,
        "kind": "rule",
        "severity": rule.severity,
        "metric": rule.metric,
        "condition": {field: list(values) for field, values in rule.condition.items()},
        "n": n,
        "effect_size": share,
        "confidence": _confidence(rule, matched),
        "ball_ids": hit_ball_ids,
        "evidence": {str(r["ball_id"]): dict(r.get("clips", {})) for r in hits},
        "text_data": dict(rule.text_data),
        "payload": {
            "op": rule.op,
            "threshold": rule.value,
            "aggregate": _aggregate(rule, matched, share),
            "hits": len(hits),
            "share": share,
            "min_n": rule.min_n,
            "trigger_share": rule.trigger_share,
            "rule_version": version,
        },
    }


def select_active_rules(rules: Iterable[CoachingRule]) -> dict[str, CoachingRule]:
    """Latest version per rule_key, kept only when enabled AND approved.

    ``approved_by`` non-null is required for a rule to run (US-G2: the AI's
    opinions are the coach's opinions); an unapproved or disabled latest
    version retires the whole rule_key without rewriting history.
    """
    latest: dict[str, CoachingRule] = {}
    for row in rules:
        current = latest.get(row.rule_key)
        if current is None or row.version > current.version:
            latest[row.rule_key] = row
    return {key: row for key, row in latest.items() if row.enabled and row.approved_by is not None}


def apply_overrides(
    definition: Mapping[str, Any], overrides: Sequence[RuleOverride]
) -> ParsedRule | None:
    """Apply one player's overrides to one rule's definition (US-G2).

    ``disable`` wins outright (returns ``None``); ``adjust`` rows merge their
    ``params`` over the definition in creation order, and the merged result is
    re-validated — a malformed adjustment raises :class:`RuleError` instead of
    silently running something the coach never approved.
    """
    merged = dict(definition)
    for override in sorted(overrides, key=lambda o: o.created_at):
        if override.action == "disable":
            return None
        merged.update(override.params)
    return parse_rule_definition(merged)


def run_rules(
    rules: Iterable[CoachingRule],
    records: Sequence[Mapping[str, Any]],
    overrides: Iterable[RuleOverride] = (),
    *,
    agent: str = "rules",
) -> list[dict[str, Any]]:
    """Evaluate every active rule over one session's BallRecords (US-G2).

    ``overrides`` must already be scoped to the session's player. Findings
    come back ordered by rule_key; a rule that does not fire emits nothing.
    """
    by_key: dict[str, list[RuleOverride]] = {}
    for override in overrides:
        by_key.setdefault(override.rule_key, []).append(override)
    findings: list[dict[str, Any]] = []
    for rule_key, row in sorted(select_active_rules(rules).items()):
        parsed = apply_overrides(row.definition, by_key.get(rule_key, []))
        if parsed is None:  # disabled for this player
            continue
        finding = evaluate_rule(rule_key, parsed, records, version=row.version, agent=agent)
        if finding is not None:
            findings.append(finding)
    return findings
