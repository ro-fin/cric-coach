"""Canonical per-ball BallRecord assembler (US-G1): one language for everyone.

Builds one validated, versioned BallRecord dict per ball from stored rows
(:class:`~cricai_data.models.BallTag`, :class:`~cricai_data.models.BallEvent`,
:class:`~cricai_data.models.BallMetrics`, :class:`~cricai_data.models.Clip`,
:class:`~cricai_data.models.SessionBlock`). The pinned v1 shape lives in
``schemas/ball_record-v1.json`` and mirrors the US-G1 example: flat identity /
context / delivery / technique / outcome fields plus ``confidence`` /
``source`` / ``reasons`` / ``clips`` maps.

Contract rules (US-G1 acceptance criteria):

- **Producers validate before write**: :func:`validate_ball_record` checks the
  assembled dict against the published JSON Schema (a self-contained subset
  interpreter — no third-party validator) plus the cross-field invariant that
  every metric field is either non-null with provenance in ``source``, or
  null with a reason code in ``reasons`` — never a fake default.
- **Consumers reject unknown MAJOR versions**: :func:`ensure_readable` parses
  ``schema_version`` and refuses anything whose major is not ``1``; unknown
  MINOR versions (additive fields) are accepted. v1.1 (US-I2/I3/I4/I6) adds ten
  optional leg-spin bowling fields, populated only for ``mode == "bowling"``
  records; :func:`migrate_v1_to_v1_1` upgrades an older v1.0 record end-to-end.
- **Provenance everywhere**: manual tag values carry ``source="manual"`` and
  confidence 1.0; metric-payload values keep their stored source (or ``proxy``
  when flagged, else ``auto``) and stored confidence.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_data.enums import ClipStatus, SessionType
from cricai_data.models import (
    BallEvent,
    BallMetrics,
    BallTag,
    Clip,
    DeliveryLabel,
    Session,
    SessionBlock,
)

#: The MAJOR version this codebase reads; consumers reject any other major.
SCHEMA_MAJOR = 1

#: The version producers stamp on newly assembled records (v1.1 adds the
#: optional bowling fields; still MAJOR 1, so v1.0 consumers keep working).
SCHEMA_VERSION = "1.1"

#: Published v1 JSON Schema (US-G1: "JSON Schema published and versioned").
SCHEMA_PATH = Path(__file__).parent / "schemas" / "ball_record-v1.json"

#: Metric-family fields: each is non-null with ``source``/``confidence`` entries
#: or null with a ``reasons`` entry (nullable-with-reason, US-G1/E3).
METRIC_FIELDS: tuple[str, ...] = (
    "mode",
    "bowler",
    "speed_kph",
    "line",
    "length",
    "bounce_xy",
    "shot",
    "footwork",
    "front_foot_direction_cm",
    "head_stability_score",
    "bat_path",
    "contact_quality",
    "outcome",
    "control",
)

#: Provenance for values copied from manual rows (tags, machine settings).
MANUAL_SOURCE = "manual"

#: Technique/delivery metric-payload keys consulted per BallRecord field, in
#: precedence order. ``speed_kph`` is handled by :func:`_fill_speed` (payload
#: first, then machine settings) so its null reason names the missing source.
_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "line": ("line",),
    "length": ("length",),
    "bounce_xy": ("bounce_xy",),
    "shot": ("shot",),
    "footwork": ("footwork",),
    "front_foot_direction_cm": ("front_foot_direction_cm",),
    "head_stability_score": ("head_stability_score",),
    "bat_path": ("bat_path", "bat_path_class"),
    "contact_quality": ("contact_quality",),
}

#: v1.1 bowling metric-payload keys consulted per BallRecord field (US-I2/I3),
#: in precedence order. ``variation_intent``/``variation_detected`` come from the
#: delivery label row (US-I6), not ``ball_metrics``, so they are filled apart.
_BOWLING_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "release_height_cm": ("release_height_cm",),
    "release_frame_offset": ("release_frame_offset",),
    "brace_state": ("brace_state",),
    "falling_away_deg": ("falling_away_deg",),
    "turn_cm": ("turn_cm",),
    "apex_m": ("apex_m",),
    "dip_flag": ("dip_flag",),
    "target_hit": ("target_hit",),
}

#: The ten optional bowling fields BallRecord v1.1 adds (US-I2/I3/I4/I6). Present
#: only on ``mode == "bowling"`` records; when present they follow the same
#: nullable-with-reason invariant as :data:`METRIC_FIELDS`. ``variation_intent``
#: is human ground truth and ``variation_detected`` the classifier's honest
#: prediction — never conflated (contract #5).
BOWLING_FIELDS: tuple[str, ...] = (
    *_BOWLING_PAYLOAD_KEYS,
    "variation_intent",
    "variation_detected",
)


class BallRecordError(ValueError):
    """A record violated the BallRecord contract (assembly, schema, version)."""


@dataclass(frozen=True)
class BallSources:
    """The stored rows describing one ball (US-G1 assembly inputs).

    Bundling the per-ball rows keeps :func:`assemble_ball_record` to a small
    signature; ``blocks`` stays separate because it is session-level context
    shared across every ball.
    """

    tag: BallTag | None = None
    event: BallEvent | None = None
    metrics: Sequence[BallMetrics] = ()
    clips: Sequence[Clip] = ()
    label: DeliveryLabel | None = None  # US-I6 variation ground truth (bowling)


#: The empty sources default (a frozen instance shared by all bare calls).
_NO_SOURCES = BallSources()


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """The published v1 JSON Schema, parsed once."""
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
    return schema


# --------------------------------------------------------------------------
# JSON Schema subset interpreter (producer-side validation, US-G1)
# --------------------------------------------------------------------------


#: JSON Schema type name -> predicate (kept as a table so the checker stays a
#: single dispatch rather than a long return ladder).
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _type_ok(value: Any, name: str) -> bool:
    return _TYPE_CHECKS[name](value)


def _check_types(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> bool:
    declared = schema["type"]
    names = declared if isinstance(declared, list) else [declared]
    if not any(_type_ok(value, name) for name in names):
        errors.append(f"{path}: expected type {'|'.join(names)}, got {type(value).__name__}")
        return False
    return True


def _check_scalar_bounds(value: Any, schema: Mapping[str, Any], path: str, err: list[str]) -> None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            err.append(f"{path}: {value} below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            err.append(f"{path}: {value} above maximum {schema['maximum']}")
    if isinstance(value, str):
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            err.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            err.append(f"{path}: shorter than minLength {schema['minLength']}")


def _check_array(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        return
    if "minItems" in schema and len(value) < schema["minItems"]:
        errors.append(f"{path}: fewer than {schema['minItems']} items")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        errors.append(f"{path}: more than {schema['maxItems']} items")
    if "items" in schema:
        for i, item in enumerate(value):
            _check_node(item, schema["items"], f"{path}[{i}]", errors)


def _check_object(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        return
    for key in schema.get("required", ()):
        if key not in value:
            errors.append(f"{path}: missing required property {key!r}")
    properties: Mapping[str, Any] = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        if key in properties:
            _check_node(item, properties[key], f"{path}.{key}", errors)
        elif isinstance(additional, dict):
            _check_node(item, additional, f"{path}.{key}", errors)
        elif additional is False:
            errors.append(f"{path}: unexpected property {key!r}")


def _check_node(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    """Validate one value against the schema subset the v1 schema uses."""
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path}: {value!r} not one of {schema['enum']}")
        return
    if "type" in schema and not _check_types(value, schema, path, errors):
        return
    _check_scalar_bounds(value, schema, path, errors)
    _check_array(value, schema, path, errors)
    _check_object(value, schema, path, errors)


def _check_null_reason_invariant(record: Mapping[str, Any], errors: list[str]) -> None:
    """Nullable-with-reason (US-G1): null => reason code, non-null => provenance.

    Bowling fields (v1.1) join the invariant only on ``mode == "bowling"``
    records, where the assembler always emits them; batting records omit them
    entirely (optional, additive MINOR).
    """
    reasons = record.get("reasons")
    source = record.get("source")
    if not isinstance(reasons, dict) or not isinstance(source, dict):
        return  # shape errors already reported by the schema walk
    fields = METRIC_FIELDS
    if record.get("mode") == SessionType.BOWLING.value:
        fields = (*METRIC_FIELDS, *BOWLING_FIELDS)
    for field in fields:
        if record.get(field) is None:
            if field not in reasons:
                errors.append(f"$.{field}: null without a reason code in 'reasons'")
        elif field not in source:
            errors.append(f"$.{field}: non-null without provenance in 'source'")


def validate_ball_record(record: Mapping[str, Any]) -> None:
    """Producer-side validation against the published v1 schema (US-G1).

    Raises :class:`BallRecordError` listing every violation: schema-subset
    checks first, then the cross-field nullable-with-reason invariant the
    schema language cannot express.
    """
    errors: list[str] = []
    _check_node(dict(record), load_schema(), "$", errors)
    _check_null_reason_invariant(record, errors)
    if errors:
        raise BallRecordError("invalid BallRecord: " + "; ".join(errors))


# --------------------------------------------------------------------------
# Consumer-side version gate (US-G1: reject unknown MAJOR)
# --------------------------------------------------------------------------


def parse_schema_version(version: Any) -> tuple[int, int]:
    """``"MAJOR.MINOR"`` -> (major, minor); loud on anything malformed."""
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+", version) is None:
        raise BallRecordError(f"malformed schema_version: {version!r}")
    major, minor = version.split(".")
    return int(major), int(minor)


def ensure_readable(record: Mapping[str, Any]) -> dict[str, Any]:
    """Consumer entry point: reject unknown-MAJOR records, tolerate MINOR.

    A v1 reader accepts any ``1.x`` record (additive minor fields ride along
    untouched — see :func:`migrate_v1_to_v1_1`) and refuses everything else
    loudly instead of misreading it.
    """
    major, _minor = parse_schema_version(record.get("schema_version"))
    if major != SCHEMA_MAJOR:
        raise BallRecordError(
            f"unknown BallRecord MAJOR version {major} (this reader speaks {SCHEMA_MAJOR}.x)"
        )
    return dict(record)


def migrate_v1_to_v1_1(record: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade an older v1.0 record to v1.1 in place (US-G1 migration-path AC).

    v1.1 adds ten optional leg-spin bowling fields. A v1.0 record predates
    bowling support, so a ``mode == "bowling"`` record gains them all as
    null-with-reason (never fabricated) while a batting record is untouched but
    for the version stamp. Either way the result stays additive: a v1 reader
    that calls :func:`ensure_readable` keeps working on it unchanged.
    """
    migrated = ensure_readable(record)
    if migrated.get("mode") == SessionType.BOWLING.value:
        reasons = dict(migrated.get("reasons", {}))
        for field in BOWLING_FIELDS:
            if field not in migrated:
                migrated[field] = None
                reasons.setdefault(field, "bowling field absent in a v1.0 record")
        migrated["reasons"] = reasons
    migrated["schema_version"] = "1.1"
    return migrated


# --------------------------------------------------------------------------
# Assembly from stored rows
# --------------------------------------------------------------------------


def _resolve_block(
    blocks: Sequence[SessionBlock], tag: BallTag | None, event: BallEvent | None
) -> SessionBlock | None:
    """The ball's practice block: the tag's explicit block first, else by time."""
    if tag is not None and tag.block_id is not None:
        for block in blocks:
            if block.id == tag.block_id:
                return block
    if event is not None:
        start_s = event.start_ms / 1000.0
        for block in blocks:
            if block.start_s <= start_s and (block.end_s is None or start_s < block.end_s):
                return block
    return None


def _machine_speed(session: Session, block: SessionBlock | None) -> float | None:
    """The recorded machine speed: block settings first, else the session's."""
    for settings in (
        block.machine_settings if block is not None else None,
        session.machine_settings,
    ):
        if settings is not None and isinstance(settings.get("speed_kph"), int | float):
            return float(settings["speed_kph"])
    return None


class _Assembly:
    """Accumulates one record's fields plus confidence/source/reasons maps."""

    def __init__(self) -> None:
        self.fields: dict[str, Any] = dict.fromkeys(METRIC_FIELDS)
        self.confidence: dict[str, float] = {}
        self.source: dict[str, str] = {}
        self.reasons: dict[str, str] = {}

    def set_manual(self, field: str, value: Any) -> None:
        self.fields[field] = value
        self.confidence[field] = 1.0
        self.source[field] = MANUAL_SOURCE
        self.reasons.pop(field, None)

    def set_null(self, field: str, reason: str) -> None:
        self.fields[field] = None
        self.reasons[field] = reason

    def set_from_payload(self, field: str, payload: Mapping[str, Any]) -> bool:
        """Adopt a MetricValue payload; True when it supplied a non-null value."""
        value = payload.get("value")
        if value is None:
            reason = payload.get("reason")
            self.set_null(field, str(reason) if reason else "stored metric value is null")
            return False
        self.fields[field] = value
        self.reasons.pop(field, None)
        confidence = payload.get("confidence")
        if isinstance(confidence, int | float):
            self.confidence[field] = float(confidence)
        stored = payload.get("source")
        if isinstance(stored, str) and stored:
            self.source[field] = stored
        else:
            self.source[field] = "proxy" if payload.get("proxy") else "auto"
        return True


def _metric_row_order(row: BallMetrics) -> tuple[str, str, str]:
    """Pinned deterministic merge order for one ball's metric rows.

    ``(phase, created_at, id)`` — phase value strings sort ``contact <
    flight < pre_release``, and ``uq_metrics_ball_phase`` makes phase alone
    decisive for persisted rows; the timestamp/id legs keep the order total
    for detached/unflushed rows a caller may hand in.
    """
    return (
        row.phase.value,
        row.created_at.isoformat() if row.created_at is not None else "",
        str(row.id) if row.id is not None else "",
    )


def _merged_metric_payloads(metrics: Iterable[BallMetrics]) -> dict[str, dict[str, Any]]:
    """All phases' MetricValue payloads keyed by metric name.

    The pipeline keys phases disjointly, but the metrics PUT endpoint accepts
    arbitrary keys per phase, so a duplicate key across phases is *possible* —
    and database row order is unspecified, so a last-wins flatten over the
    caller's iteration order would make the assembled record depend on the
    query plan. The rows are therefore merged in the pinned
    :func:`_metric_row_order`; on a duplicate key the LATER row in that order
    wins (``pre_release`` beats ``flight`` beats ``contact``), identically on
    every engine, plan and run.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in sorted(metrics, key=_metric_row_order):
        for key, payload in row.metrics.items():
            if isinstance(payload, dict):
                merged[key] = payload
    return merged


def _fill_from_tag(assembly: _Assembly, tag: BallTag) -> None:
    assembly.set_manual("line", tag.line.value)
    assembly.set_manual("length", tag.length.value)
    assembly.set_manual("shot", tag.shot.value)
    assembly.set_manual("footwork", tag.footwork.value)
    assembly.set_manual("contact_quality", tag.contact.value)
    assembly.set_manual("outcome", tag.outcome.value)
    assembly.set_manual("control", tag.control)


def _fill_from_payloads(assembly: _Assembly, payloads: Mapping[str, Mapping[str, Any]]) -> None:
    """Fill every still-unset metric field from stored metric payloads."""
    for field, keys in _PAYLOAD_KEYS.items():
        if assembly.fields[field] is not None:
            continue
        candidates = [payloads[key] for key in keys if key in payloads]
        if not candidates:
            assembly.set_null(field, f"no stored metric provides {field}")
            continue
        for payload in candidates:
            if assembly.set_from_payload(field, payload):
                break


def _fill_context(assembly: _Assembly, session: Session, block: SessionBlock | None) -> None:
    """mode / bowler from session + block configuration."""
    if session.session_type is SessionType.MIXED:
        if block is None:
            assembly.set_null("mode", "mixed session and no block context for this ball")
        else:
            assembly.set_manual("mode", SessionType.BATTING.value)  # blocks are batting practice
    else:
        assembly.set_manual("mode", session.session_type.value)
    bowler = block.bowler_source if block is not None else session.bowler_source
    assembly.set_manual("bowler", bowler.value)


def _fill_speed(
    assembly: _Assembly,
    session: Session,
    block: SessionBlock | None,
    payloads: Mapping[str, Mapping[str, Any]],
    ball_no: int,
) -> None:
    """Resolve ``speed_kph``: a non-null flight-metric payload wins, else the
    recorded machine speed, else null with the honest missing-source reason.

    A payload that is itself null-with-reason yields to the machine settings;
    when neither supplies a value, that stored reason (not a generic
    placeholder) is what survives.
    """
    payload = payloads.get("speed_kph")
    if payload is not None and assembly.set_from_payload("speed_kph", payload):
        return
    speed = _machine_speed(session, block)
    if speed is not None:
        assembly.set_manual("speed_kph", speed)
    elif "speed_kph" not in assembly.reasons:
        assembly.set_null("speed_kph", f"no machine speed recorded for ball {ball_no}")


def _fill_tag_only_nulls(assembly: _Assembly) -> None:
    """outcome/control have no automatic producer yet: tag or null-with-reason."""
    for field in ("outcome", "control"):
        if assembly.fields[field] is None:
            assembly.set_null(field, f"no manual tag provides {field}")


def _fill_variation(assembly: _Assembly, label: DeliveryLabel | None) -> None:
    """variation_intent (human ground truth) and variation_detected (classifier
    output) from the delivery label row (US-I6), never conflated (contract #5)."""
    if label is None:
        assembly.set_null("variation_intent", "no delivery label for this ball")
        assembly.set_null("variation_detected", "no delivery label for this ball")
        return
    assembly.fields["variation_intent"] = label.variation_intent.value
    assembly.confidence["variation_intent"] = 1.0
    assembly.source["variation_intent"] = label.source  # manual|model
    assembly.reasons.pop("variation_intent", None)
    if label.variation_detected is None:
        assembly.set_null("variation_detected", "classifier has not labeled this delivery")
    else:
        assembly.fields["variation_detected"] = label.variation_detected.value
        assembly.source["variation_detected"] = "model"
        assembly.reasons.pop("variation_detected", None)


def _fill_bowling(
    assembly: _Assembly,
    payloads: Mapping[str, Mapping[str, Any]],
    label: DeliveryLabel | None,
) -> None:
    """Populate the v1.1 bowling fields for a bowling delivery (US-I2/I3/I6).

    Release/flight/target fields come from ``ball_metrics`` bowling metric
    payloads (nullable-with-reason when absent, never fabricated); the variation
    pair comes from the delivery label row. Only called for ``mode == "bowling"``
    records — batting records never carry these fields.
    """
    for field, keys in _BOWLING_PAYLOAD_KEYS.items():
        candidates = [payloads[key] for key in keys if key in payloads]
        if not candidates:
            assembly.set_null(field, f"no stored metric provides {field}")
            continue
        for payload in candidates:
            if assembly.set_from_payload(field, payload):
                break
    _fill_variation(assembly, label)


def assemble_ball_record(
    session: Session,
    ball_no: int,
    sources: BallSources = _NO_SOURCES,
    *,
    blocks: Sequence[SessionBlock] = (),
) -> dict[str, Any]:
    """Build and validate one canonical BallRecord dict (US-G1).

    Manual tag values win over stored metric payloads (manual beats machine,
    the system-wide rule); every absent value is null-with-reason; the result
    is validated against the published v1 schema before it is returned.
    """
    assembly = _Assembly()
    payloads = _merged_metric_payloads(sources.metrics)
    if sources.tag is not None:
        _fill_from_tag(assembly, sources.tag)
    _fill_from_payloads(assembly, payloads)
    block = _resolve_block(blocks, sources.tag, sources.event)
    _fill_context(assembly, session, block)
    _fill_speed(assembly, session, block, payloads, ball_no)
    _fill_tag_only_nulls(assembly)
    if assembly.fields["mode"] == SessionType.BOWLING.value:
        _fill_bowling(assembly, payloads, sources.label)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ball_id": ball_no,
        "session_id": str(session.id),
        "block_id": str(block.id) if block is not None else None,
        **assembly.fields,
        "confidence": assembly.confidence,
        "source": assembly.source,
        "reasons": assembly.reasons,
        "clips": {
            clip.camera_id: str(clip.id)
            for clip in sources.clips
            if clip.ball_no == ball_no and clip.status is ClipStatus.CUT
        },
    }
    validate_ball_record(record)
    return record


def session_ball_records(db: OrmSession, session_id: uuid.UUID) -> list[dict[str, Any]]:
    """Assemble the canonical record for every known ball of one session.

    A ball is known when it has a manual tag or a non-rejected event (the same
    known-ball rule the metrics API uses). Records come back ordered by ball
    number, each already producer-validated.
    """
    session = db.get(Session, session_id)
    if session is None:
        raise BallRecordError(f"session not found: {session_id}")
    tags = {
        t.ball_no: t for t in db.scalars(select(BallTag).where(BallTag.session_id == session_id))
    }
    events = {
        e.ball_no: e
        for e in db.scalars(
            select(BallEvent).where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
        )
    }
    metrics_rows = list(db.scalars(select(BallMetrics).where(BallMetrics.session_id == session_id)))
    clip_rows = list(db.scalars(select(Clip).where(Clip.session_id == session_id)))
    labels = {
        label.ball_no: label
        for label in db.scalars(select(DeliveryLabel).where(DeliveryLabel.session_id == session_id))
    }
    blocks = list(
        db.scalars(
            select(SessionBlock)
            .where(SessionBlock.session_id == session_id)
            .order_by(SessionBlock.block_no)
        )
    )
    records = []
    for ball_no in sorted(set(tags) | set(events)):
        records.append(
            assemble_ball_record(
                session,
                ball_no,
                BallSources(
                    tag=tags.get(ball_no),
                    event=events.get(ball_no),
                    metrics=[m for m in metrics_rows if m.ball_no == ball_no],
                    clips=[c for c in clip_rows if c.ball_no == ball_no],
                    label=labels.get(ball_no),
                ),
                blocks=blocks,
            )
        )
    return records
