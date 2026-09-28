"""US-G1 canonical BallRecord: schema validation, provenance, nullable-with-reason,
assembly precedence (manual beats machine), consumer major-version gate, and the
v1 -> v1.1 additive-migration demo.
"""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from cricai_data.ballrecord import (
    BOWLING_FIELDS,
    METRIC_FIELDS,
    SCHEMA_PATH,
    BallRecordError,
    BallSources,
    _check_node,
    _merged_metric_payloads,
    assemble_ball_record,
    ensure_readable,
    load_schema,
    migrate_v1_to_v1_1,
    parse_schema_version,
    session_ball_records,
    validate_ball_record,
)
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    BowlingVariation,
    BraceState,
    ClipStatus,
    Contact,
    Footwork,
    Length,
    Line,
    MetricPhase,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import (
    BallEvent,
    BallMetrics,
    BallTag,
    Clip,
    DeliveryLabel,
    Player,
    Session,
    SessionBlock,
)


def make_session(
    session_type: SessionType = SessionType.BATTING,
    bowler_source: BowlerSource = BowlerSource.MACHINE,
    machine_settings: dict[str, Any] | None = None,
) -> Session:
    return Session(
        id=uuid.uuid4(),
        player_id=uuid.uuid4(),
        session_date=date(2026, 7, 9),
        session_type=session_type,
        bowler_source=bowler_source,
        machine_settings=machine_settings,
    )


def make_tag(session: Session, ball_no: int, block_id: uuid.UUID | None = None) -> BallTag:
    return BallTag(
        session_id=session.id,
        ball_no=ball_no,
        block_id=block_id,
        line=Line.OUTSIDE_OFF,
        length=Length.FULL,
        shot=Shot.COVER_DRIVE,
        footwork=Footwork.FRONT,
        contact=Contact.MIDDLE,
        outcome=Outcome.CONTROLLED_GROUND_SHOT,
        control=True,
        created_by="coach",
    )


def make_clip(session: Session, ball_no: int, camera_id: str, status: ClipStatus) -> Clip:
    return Clip(
        id=uuid.uuid4(),
        session_id=session.id,
        ball_no=ball_no,
        camera_id=camera_id,
        start_ms=0,
        end_ms=1000,
        status=status,
    )


def metrics_row(session: Session, ball_no: int, phase: MetricPhase, metrics: Any) -> BallMetrics:
    return BallMetrics(session_id=session.id, ball_no=ball_no, phase=phase, metrics=metrics)


def _assemble(
    session: Session,
    ball_no: int,
    *,
    tag: BallTag | None = None,
    event: BallEvent | None = None,
    metrics: Sequence[BallMetrics] = (),
    clips: Sequence[Clip] = (),
    label: DeliveryLabel | None = None,
    blocks: Sequence[SessionBlock] = (),
) -> dict[str, Any]:
    """Keyword-friendly wrapper over the bundled-sources assembler."""
    return assemble_ball_record(
        session,
        ball_no,
        BallSources(tag=tag, event=event, metrics=metrics, clips=clips, label=label),
        blocks=blocks,
    )


# --------------------------------------------------------------------------
# Published schema
# --------------------------------------------------------------------------


class TestPublishedSchema:
    def test_schema_file_is_published_and_versioned(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text())
        assert schema["$id"].endswith("ball_record-v1.json")
        # v1.1 bowling fields are OPTIONAL: declared in properties but kept out
        # of 'required', so a v1.0 record still validates (additive MINOR).
        assert set(schema["required"]) < set(schema["properties"])
        assert set(schema["properties"]) - set(schema["required"]) == set(BOWLING_FIELDS)
        assert schema["properties"]["schema_version"]["pattern"] == "^1\\.[0-9]+$"

    def test_schema_enums_match_canonical_vocabularies(self) -> None:
        properties = load_schema()["properties"]
        assert properties["line"]["enum"] == [*[m.value for m in Line], None]
        assert properties["length"]["enum"] == [*[m.value for m in Length], None]
        assert properties["shot"]["enum"] == [*[m.value for m in Shot], None]
        assert properties["footwork"]["enum"] == [*[m.value for m in Footwork], None]
        assert properties["outcome"]["enum"] == [*[m.value for m in Outcome], None]
        assert properties["bowler"]["enum"] == [*[m.value for m in BowlerSource], None]
        # v1.1 bowling enums (US-I6/I2).
        assert properties["variation_intent"]["enum"] == [
            *[m.value for m in BowlingVariation],
            None,
        ]
        assert properties["variation_detected"]["enum"] == [
            *[m.value for m in BowlingVariation],
            None,
        ]
        assert properties["brace_state"]["enum"] == [*[m.value for m in BraceState], None]

    def test_v1_readers_tolerate_additive_fields(self) -> None:
        assert load_schema()["additionalProperties"] is True


# --------------------------------------------------------------------------
# Producer-side validation
# --------------------------------------------------------------------------


@pytest.fixture
def valid_record() -> dict[str, Any]:
    session = make_session(machine_settings={"speed_kph": 92})
    return _assemble(
        session,
        1,
        tag=make_tag(session, 1),
        clips=[make_clip(session, 1, "C1", ClipStatus.CUT)],
    )


class TestValidateBallRecord:
    def test_valid_record_passes(self, valid_record: dict[str, Any]) -> None:
        validate_ball_record(valid_record)  # no raise

    @pytest.mark.parametrize(
        ("field", "bad", "fragment"),
        [
            ("line", "wide", "not one of"),
            ("ball_id", "1", "expected type integer"),
            ("ball_id", True, "expected type integer"),
            ("ball_id", 0, "below minimum"),
            ("head_stability_score", 1.5, "above maximum"),
            ("session_id", "not-a-uuid", "does not match pattern"),
            ("schema_version", "one.zero", "does not match pattern"),
            ("bat_path", "", "minLength"),
            ("bounce_xy", [1.0], "fewer than 2 items"),
            ("bounce_xy", [1.0, 2.0, 3.0], "more than 2 items"),
            ("bounce_xy", [1.0, "x"], "expected type number"),
            ("control", "yes", "expected type boolean|null"),
            ("block_id", 7, "expected type string|null"),
            ("confidence", {"line": 2.0}, "above maximum"),
            ("confidence", "high", "expected type object"),
            ("source", {"line": 9}, "expected type string"),
            ("clips", {"C1": ""}, "minLength"),
        ],
    )
    def test_invalid_fixtures_are_rejected(
        self, valid_record: dict[str, Any], field: str, bad: Any, fragment: str
    ) -> None:
        with pytest.raises(BallRecordError, match="invalid BallRecord") as excinfo:
            validate_ball_record({**valid_record, field: bad})
        assert fragment in str(excinfo.value)

    def test_missing_required_field_is_rejected(self, valid_record: dict[str, Any]) -> None:
        broken = dict(valid_record)
        del broken["clips"]
        with pytest.raises(BallRecordError, match="missing required property 'clips'"):
            validate_ball_record(broken)

    def test_null_metric_without_reason_is_rejected(self, valid_record: dict[str, Any]) -> None:
        broken = {**valid_record, "outcome": None}  # reasons has no 'outcome' entry
        with pytest.raises(BallRecordError, match="null without a reason code"):
            validate_ball_record(broken)

    def test_non_null_metric_without_provenance_is_rejected(
        self, valid_record: dict[str, Any]
    ) -> None:
        source = {k: v for k, v in valid_record["source"].items() if k != "line"}
        with pytest.raises(BallRecordError, match="non-null without provenance"):
            validate_ball_record({**valid_record, "source": source})

    def test_shape_broken_maps_skip_the_invariant_walk(self, valid_record: dict[str, Any]) -> None:
        # reasons of the wrong type is a schema error; the invariant walk defers to it
        with pytest.raises(BallRecordError, match="expected type object"):
            validate_ball_record({**valid_record, "reasons": "none"})

    def test_every_metric_field_is_schema_covered(self) -> None:
        properties = load_schema()["properties"]
        assert set(METRIC_FIELDS) <= set(properties)


class TestSchemaInterpreterKeywords:
    """Keywords the published schema exercises only partially."""

    def test_closed_object_rejects_unknown_properties(self) -> None:
        errors: list[str] = []
        _check_node({"x": 1}, {"type": "object", "additionalProperties": False}, "$", errors)
        assert errors == ["$: unexpected property 'x'"]

    def test_schema_without_type_or_enum_accepts_anything(self) -> None:
        errors: list[str] = []
        _check_node(object(), {}, "$", errors)
        assert errors == []

    def test_nested_required_is_reported(self) -> None:
        errors: list[str] = []
        _check_node({}, {"type": "object", "required": ["x"]}, "$", errors)
        assert errors == ["$: missing required property 'x'"]

    def test_array_without_items_only_checks_length(self) -> None:
        # the published schema's only array (bounce_xy) carries "items"; an
        # itemless array schema checks bounds but not element types.
        errors: list[str] = []
        _check_node([1, "x", None], {"type": "array"}, "$", errors)
        assert errors == []

    def test_open_object_tolerates_unknown_properties(self) -> None:
        # additionalProperties true (the record schema's default): an unknown
        # key is neither validated against a sub-schema nor rejected — this is
        # how a v1 reader tolerates additive MINOR fields.
        errors: list[str] = []
        _check_node(
            {"future_field": 1}, {"type": "object", "additionalProperties": True}, "$", errors
        )
        assert errors == []


# --------------------------------------------------------------------------
# Consumer version gate + v1 -> v1.1 migration demo
# --------------------------------------------------------------------------


class TestVersionGate:
    def test_parse_schema_version(self) -> None:
        assert parse_schema_version("1.0") == (1, 0)
        assert parse_schema_version("12.34") == (12, 34)

    @pytest.mark.parametrize("bad", [None, 1.0, "1", "1.0.0", "v1.0", ""])
    def test_malformed_versions_are_loud(self, bad: Any) -> None:
        with pytest.raises(BallRecordError, match="malformed schema_version"):
            parse_schema_version(bad)

    def test_unknown_major_is_rejected(self, valid_record: dict[str, Any]) -> None:
        with pytest.raises(BallRecordError, match="unknown BallRecord MAJOR version 2"):
            ensure_readable({**valid_record, "schema_version": "2.0"})

    def test_known_major_any_minor_is_accepted(self, valid_record: dict[str, Any]) -> None:
        assert ensure_readable({**valid_record, "schema_version": "1.7"})["ball_id"] == 1

    def test_v1_consumer_accepts_a_v1_1_bowling_record(self) -> None:
        # A validator pinned to MAJOR 1 (a v1.0 consumer) accepts a v1.1 record
        # carrying the new bowling fields — additive MINOR never breaks it.
        record = _assemble(make_session(session_type=SessionType.BOWLING), 1)
        assert record["schema_version"] == "1.1"
        assert set(BOWLING_FIELDS) <= set(record)
        assert ensure_readable(record)["ball_id"] == 1


class TestMigrationDemo:
    def test_batting_v1_record_only_gets_a_version_stamp(
        self, valid_record: dict[str, Any]
    ) -> None:
        # A batting record gains no bowling fields — just the version bump.
        legacy = {**valid_record, "schema_version": "1.0"}
        migrated = migrate_v1_to_v1_1(legacy)
        assert migrated["schema_version"] == "1.1"
        assert all(field not in migrated for field in BOWLING_FIELDS)
        for field, value in legacy.items():
            if field != "schema_version":
                assert migrated[field] == value

    def test_bowling_v1_record_gains_null_with_reason_bowling_fields(self) -> None:
        # Simulate a legacy v1.0 bowling record by stripping the v1.1 fields.
        record = _assemble(make_session(session_type=SessionType.BOWLING), 1)
        legacy = {k: v for k, v in record.items() if k not in BOWLING_FIELDS}
        legacy["schema_version"] = "1.0"
        legacy["reasons"] = {k: v for k, v in record["reasons"].items() if k not in BOWLING_FIELDS}
        migrated = migrate_v1_to_v1_1(legacy)
        assert migrated["schema_version"] == "1.1"
        for field in BOWLING_FIELDS:
            assert migrated[field] is None
            assert migrated["reasons"][field]
        validate_ball_record(migrated)  # additive: the upgraded record is valid v1.1

    def test_migrated_batting_record_still_reads_as_v1(self, valid_record: dict[str, Any]) -> None:
        migrated = migrate_v1_to_v1_1({**valid_record, "schema_version": "1.0"})
        assert ensure_readable(migrated)["line"] == valid_record["line"]

    def test_already_v1_1_bowling_record_is_left_intact(self) -> None:
        # A bowling record that already carries the v1.1 fields keeps them
        # (migration never overwrites present fields) — only the stamp is set.
        record = _assemble(make_session(session_type=SessionType.BOWLING), 1)
        migrated = migrate_v1_to_v1_1(record)
        assert migrated["schema_version"] == "1.1"
        for field in BOWLING_FIELDS:
            assert migrated[field] == record[field]


# --------------------------------------------------------------------------
# Assembly: precedence, provenance, nullable-with-reason
# --------------------------------------------------------------------------


class TestAssembleFromTag:
    def test_manual_tag_fields_with_manual_provenance(self) -> None:
        session = make_session(machine_settings={"speed_kph": 92})
        record = _assemble(
            session,
            1,
            tag=make_tag(session, 1),
            clips=[
                make_clip(session, 1, "C1", ClipStatus.CUT),
                make_clip(session, 1, "C3", ClipStatus.GAP),  # gaps are not evidence
                make_clip(session, 2, "C1", ClipStatus.CUT),  # other ball
            ],
        )
        assert record["ball_id"] == 1
        assert record["session_id"] == str(session.id)
        assert record["mode"] == "batting"
        assert record["bowler"] == "machine"
        assert record["speed_kph"] == 92.0
        assert record["line"] == "outside_off"
        assert record["contact_quality"] == "middle"
        assert record["control"] is True
        assert record["source"]["line"] == "manual"
        assert record["confidence"]["line"] == 1.0
        assert list(record["clips"]) == ["C1"]

    def test_manual_tag_beats_stored_metric_payloads(self) -> None:
        session = make_session()
        flight = metrics_row(
            session,
            1,
            MetricPhase.FLIGHT,
            {"line": {"value": "leg", "unit": "class", "confidence": 0.4, "source": "auto_v3"}},
        )
        record = _assemble(session, 1, tag=make_tag(session, 1), metrics=[flight])
        assert record["line"] == "outside_off"  # manual wins
        assert record["source"]["line"] == "manual"

    def test_untagged_ball_is_null_with_reason_never_defaults(self) -> None:
        record = _assemble(make_session(), 9)
        for field in ("line", "shot", "outcome", "control", "head_stability_score"):
            assert record[field] is None
            assert record["reasons"][field]
        assert record["reasons"]["outcome"] == "no manual tag provides outcome"
        assert record["clips"] == {}


class TestAssembleFromMetrics:
    def test_metric_payload_provenance_and_confidence(self) -> None:
        session = make_session()
        contact = metrics_row(
            session,
            2,
            MetricPhase.CONTACT,
            {
                "head_stability_score": {"value": 0.44, "unit": "score", "confidence": 0.9},
                "bat_path_class": {
                    "value": "across",
                    "unit": "class",
                    "confidence": 0.7,
                    "proxy": True,
                },
                "front_foot_direction_cm": {
                    "value": None,
                    "unit": "cm",
                    "confidence": 0.0,
                    "reason": "no pixel-to-cm scale",
                },
            },
        )
        flight = metrics_row(
            session,
            2,
            MetricPhase.FLIGHT,
            {
                "line": {"value": "off", "unit": "class", "confidence": 0.97, "source": "auto_v3"},
                "bounce_xy": {"value": [0.2, 6.1], "unit": "m", "confidence": 0.8},
                "not_a_payload": "ignored",
            },
        )
        record = _assemble(session, 2, metrics=[contact, flight])
        assert record["head_stability_score"] == 0.44
        assert record["source"]["head_stability_score"] == "auto"  # no stored source, not proxy
        assert record["bat_path"] == "across"
        assert record["source"]["bat_path"] == "proxy"
        assert record["confidence"]["bat_path"] == 0.7
        assert record["line"] == "off"
        assert record["source"]["line"] == "auto_v3"
        assert record["bounce_xy"] == [0.2, 6.1]
        assert record["front_foot_direction_cm"] is None
        assert record["reasons"]["front_foot_direction_cm"] == "no pixel-to-cm scale"

    def test_fusion_bat_path_beats_the_proxy_key(self) -> None:
        session = make_session()
        contact = metrics_row(
            session,
            2,
            MetricPhase.CONTACT,
            {
                "bat_path": {"value": "straight", "unit": "class", "confidence": 0.8},
                "bat_path_class": {
                    "value": "across",
                    "unit": "class",
                    "confidence": 0.7,
                    "proxy": True,
                },
            },
        )
        record = _assemble(session, 2, metrics=[contact])
        assert record["bat_path"] == "straight"

    def test_null_first_candidate_falls_through_to_the_next(self) -> None:
        session = make_session()
        contact = metrics_row(
            session,
            2,
            MetricPhase.CONTACT,
            {
                "bat_path": {
                    "value": None,
                    "unit": "class",
                    "confidence": 0.0,
                    "reason": "too few frames",
                },
                "bat_path_class": {
                    "value": "across",
                    "unit": "class",
                    "confidence": 0.7,
                    "proxy": True,
                },
            },
        )
        record = _assemble(session, 2, metrics=[contact])
        assert record["bat_path"] == "across"
        assert "bat_path" not in record["reasons"]

    def test_all_null_candidates_keep_the_stored_reason(self) -> None:
        session = make_session()
        contact = metrics_row(
            session,
            2,
            MetricPhase.CONTACT,
            {"bat_path": {"value": None, "unit": "class", "confidence": 0.0, "reason": "dark"}},
        )
        record = _assemble(session, 2, metrics=[contact])
        assert record["bat_path"] is None
        assert record["reasons"]["bat_path"] == "dark"

    def test_null_payload_without_reason_gets_a_default_reason(self) -> None:
        session = make_session()
        contact = metrics_row(
            session,
            2,
            MetricPhase.CONTACT,
            {"head_stability_score": {"value": None, "unit": "score", "confidence": 0.0}},
        )
        record = _assemble(session, 2, metrics=[contact])
        assert record["reasons"]["head_stability_score"] == "stored metric value is null"

    def test_payload_without_confidence_still_has_provenance(self) -> None:
        session = make_session()
        contact = metrics_row(
            session,
            2,
            MetricPhase.CONTACT,
            {"head_stability_score": {"value": 0.5, "unit": "score"}},
        )
        record = _assemble(session, 2, metrics=[contact])
        assert record["source"]["head_stability_score"] == "auto"
        assert "head_stability_score" not in record["confidence"]


class TestDeterministicMetricMerge:
    """Phase-6 finding 43: the metric-payload merge is order-independent.

    The metrics PUT endpoint permits the same key in two phases, and database
    row return order is unspecified — the merge therefore sorts rows by the
    pinned ``(phase, created_at, id)`` order so the assembled record is
    byte-identical on every engine, plan and run.
    """

    def _speed(self, value: float, source: str) -> dict[str, Any]:
        return {"speed_kph": {"value": value, "unit": "kph", "confidence": 1.0, "source": source}}

    def test_cross_phase_duplicate_key_resolves_identically_in_any_row_order(self) -> None:
        session = make_session()
        tracker = metrics_row(session, 1, MetricPhase.FLIGHT, self._speed(118.0, "tracker"))
        manual = metrics_row(session, 1, MetricPhase.PRE_RELEASE, self._speed(95.0, "manual"))
        one = _assemble(session, 1, metrics=[tracker, manual])
        other = _assemble(session, 1, metrics=[manual, tracker])
        assert one == other  # a query-plan artifact can never change the record
        # Pinned precedence: the later phase in value order wins
        # (contact < flight < pre_release).
        assert one["speed_kph"] == 95.0
        assert one["source"]["speed_kph"] == "manual"

    def test_created_at_then_id_break_ties_deterministically(self) -> None:
        """Persisted rows carry timestamps/ids; the tiebreak legs keep the
        order total (and stable) even for pathological same-phase inputs."""
        session = make_session()
        early = metrics_row(session, 1, MetricPhase.FLIGHT, self._speed(90.0, "a"))
        early.id = uuid.UUID(int=2)
        early.created_at = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
        late = metrics_row(session, 1, MetricPhase.FLIGHT, self._speed(91.0, "b"))
        late.id = uuid.UUID(int=1)
        late.created_at = datetime(2026, 7, 9, 11, 0, tzinfo=UTC)
        assert _merged_metric_payloads([early, late]) == _merged_metric_payloads([late, early])
        assert _merged_metric_payloads([late, early])["speed_kph"]["value"] == 91.0  # later row
        twin = metrics_row(session, 1, MetricPhase.FLIGHT, self._speed(92.0, "c"))
        twin.id = uuid.UUID(int=3)
        twin.created_at = early.created_at  # equal timestamps: the id leg decides
        assert _merged_metric_payloads([twin, early]) == _merged_metric_payloads([early, twin])
        assert _merged_metric_payloads([early, twin])["speed_kph"]["value"] == 92.0


class TestContextResolution:
    def test_block_from_tag_wins_and_sets_context(self) -> None:
        session = make_session(session_type=SessionType.MIXED, bowler_source=BowlerSource.MACHINE)
        block = SessionBlock(
            id=uuid.uuid4(),
            session_id=session.id,
            block_no=1,
            start_s=0.0,
            end_s=600.0,
            bowler_source=BowlerSource.COACH,
            machine_settings={"speed_kph": 85},
            intent=BlockIntent.TECHNICAL,
        )
        record = _assemble(session, 1, tag=make_tag(session, 1, block_id=block.id), blocks=[block])
        assert record["block_id"] == str(block.id)
        assert record["mode"] == "batting"  # mixed session, block context
        assert record["bowler"] == "coach"  # block beats session
        assert record["speed_kph"] == 85.0  # block settings beat session's

    def test_block_by_event_timestamp_open_ended(self) -> None:
        session = make_session(session_type=SessionType.MIXED)
        blocks = [
            SessionBlock(
                id=uuid.uuid4(),
                session_id=session.id,
                block_no=1,
                start_s=0.0,
                end_s=60.0,
                bowler_source=BowlerSource.MACHINE,
                intent=BlockIntent.TECHNICAL,
            ),
            SessionBlock(
                id=uuid.uuid4(),
                session_id=session.id,
                block_no=2,
                start_s=60.0,
                end_s=None,  # open block
                bowler_source=BowlerSource.HUMAN,
                intent=BlockIntent.FUN,
            ),
        ]
        event = BallEvent(
            session_id=session.id,
            ball_no=3,
            start_ms=90_000,
            release_ms=90_100,
            end_ms=92_000,
            confidence=0.9,
        )
        record = _assemble(session, 3, event=event, blocks=blocks)
        assert record["block_id"] == str(blocks[1].id)
        assert record["bowler"] == "human"

    def test_tag_block_id_not_in_blocks_falls_back_to_event_time(self) -> None:
        session = make_session()
        block = SessionBlock(
            id=uuid.uuid4(),
            session_id=session.id,
            block_no=1,
            start_s=0.0,
            end_s=60.0,
            bowler_source=BowlerSource.COACH,
            intent=BlockIntent.TECHNICAL,
        )
        tag = make_tag(session, 1, block_id=uuid.uuid4())  # dangling block reference
        event = BallEvent(
            session_id=session.id,
            ball_no=1,
            start_ms=1_000,
            release_ms=1_100,
            end_ms=3_000,
            confidence=0.9,
        )
        record = _assemble(session, 1, tag=tag, event=event, blocks=[block])
        assert record["block_id"] == str(block.id)

    def test_event_outside_every_block_has_no_block(self) -> None:
        session = make_session()
        block = SessionBlock(
            id=uuid.uuid4(),
            session_id=session.id,
            block_no=1,
            start_s=10.0,
            end_s=60.0,
            bowler_source=BowlerSource.COACH,
            intent=BlockIntent.TECHNICAL,
        )
        event = BallEvent(
            session_id=session.id,
            ball_no=1,
            start_ms=1_000,  # before the block starts
            release_ms=1_100,
            end_ms=3_000,
            confidence=0.9,
        )
        record = _assemble(session, 1, event=event, blocks=[block])
        assert record["block_id"] is None

    def test_mixed_session_without_block_mode_is_null_with_reason(self) -> None:
        record = _assemble(make_session(session_type=SessionType.MIXED), 1)
        assert record["mode"] is None
        assert "mixed session" in record["reasons"]["mode"]

    def test_bowling_session_mode(self) -> None:
        record = _assemble(make_session(session_type=SessionType.BOWLING), 1)
        assert record["mode"] == "bowling"

    def test_speed_from_flight_metric_beats_machine_settings(self) -> None:
        session = make_session(machine_settings={"speed_kph": 92})
        flight = metrics_row(
            session,
            1,
            MetricPhase.FLIGHT,
            {"speed_kph": {"value": 88.0, "unit": "kph", "confidence": 1.0, "source": "manual"}},
        )
        record = _assemble(session, 1, metrics=[flight])
        assert record["speed_kph"] == 88.0

    def test_machine_settings_fill_speed_even_after_a_null_payload(self) -> None:
        session = make_session(machine_settings={"speed_kph": 92})
        flight = metrics_row(
            session,
            1,
            MetricPhase.FLIGHT,
            {"speed_kph": {"value": None, "unit": "kph", "confidence": 0.0, "reason": "none"}},
        )
        record = _assemble(session, 1, metrics=[flight])
        assert record["speed_kph"] == 92.0
        assert "speed_kph" not in record["reasons"]

    def test_null_payload_speed_reason_survives_when_no_settings(self) -> None:
        session = make_session()
        flight = metrics_row(
            session,
            1,
            MetricPhase.FLIGHT,
            {"speed_kph": {"value": None, "unit": "kph", "confidence": 0.0, "reason": "manual"}},
        )
        record = _assemble(session, 1, metrics=[flight])
        assert record["reasons"]["speed_kph"] == "manual"

    def test_non_numeric_machine_speed_is_ignored(self) -> None:
        record = _assemble(make_session(machine_settings={"speed_kph": "fast"}), 1)
        assert record["speed_kph"] is None
        assert "no machine speed recorded" in record["reasons"]["speed_kph"]


# --------------------------------------------------------------------------
# Bowling fields (v1.1): populated only for mode == "bowling"
# --------------------------------------------------------------------------


def _label(
    session: Session,
    ball_no: int,
    *,
    intent: BowlingVariation,
    detected: BowlingVariation | None = None,
    source: str = "manual",
) -> DeliveryLabel:
    return DeliveryLabel(
        session_id=session.id,
        ball_no=ball_no,
        variation_intent=intent,
        variation_detected=detected,
        labeler="coach",
        source=source,
    )


class TestBowlingFields:
    def test_bowling_record_without_sources_is_null_with_reason(self) -> None:
        record = _assemble(make_session(session_type=SessionType.BOWLING), 1)
        for field in BOWLING_FIELDS:
            assert record[field] is None
            assert record["reasons"][field]
        validate_ball_record(record)

    def test_bowling_fields_from_metrics_and_label(self) -> None:
        session = make_session(session_type=SessionType.BOWLING)
        pre_release = metrics_row(
            session,
            1,
            MetricPhase.PRE_RELEASE,
            {
                "release_height_cm": {
                    "value": 210.0,
                    "unit": "cm",
                    "confidence": 0.9,
                    "source": "cv_release",
                },
                "brace_state": {"value": "braced", "unit": "class", "confidence": 0.8},
                # A present-but-null candidate keeps its stored reason.
                "turn_cm": {"value": None, "unit": "cm", "confidence": 0.0, "reason": "no track"},
            },
        )
        record = _assemble(
            session,
            1,
            metrics=[pre_release],
            label=_label(
                session, 1, intent=BowlingVariation.GOOGLY, detected=BowlingVariation.LEG_BREAK
            ),
        )
        assert record["release_height_cm"] == 210.0
        assert record["source"]["release_height_cm"] == "cv_release"
        assert record["brace_state"] == "braced"
        assert record["source"]["brace_state"] == "auto"
        assert record["turn_cm"] is None
        assert record["reasons"]["turn_cm"] == "no track"
        # No metric provided release_frame_offset -> null-with-reason.
        assert record["release_frame_offset"] is None
        assert (
            record["reasons"]["release_frame_offset"]
            == "no stored metric provides release_frame_offset"
        )
        # variation_intent (ground truth) and variation_detected (model) — never
        # conflated (contract #5).
        assert record["variation_intent"] == "googly"
        assert record["source"]["variation_intent"] == "manual"
        assert record["variation_detected"] == "leg_break"
        assert record["source"]["variation_detected"] == "model"

    def test_label_without_detected_is_null_with_reason(self) -> None:
        session = make_session(session_type=SessionType.BOWLING)
        record = _assemble(
            session, 1, label=_label(session, 1, intent=BowlingVariation.FLIPPER, source="model")
        )
        assert record["variation_intent"] == "flipper"
        assert record["source"]["variation_intent"] == "model"  # model pre-label
        assert record["variation_detected"] is None
        assert "classifier" in record["reasons"]["variation_detected"]

    def test_batting_record_carries_no_bowling_fields(self) -> None:
        record = _assemble(make_session(), 1)
        assert all(field not in record for field in BOWLING_FIELDS)


# --------------------------------------------------------------------------
# Whole-session assembly from the DB
# --------------------------------------------------------------------------


class TestSessionBallRecords:
    def test_assembles_tagged_and_event_only_balls_in_order(self) -> None:
        engine = make_engine("sqlite://")
        create_all(engine)
        factory = make_session_factory(engine)
        with factory() as db:
            player = Player(name="Arjun", birthdate=date(2014, 11, 20))
            session = Session(
                player=player,
                session_date=date(2026, 7, 9),
                session_type=SessionType.BATTING,
                bowler_source=BowlerSource.MACHINE,
                machine_settings={"speed_kph": 90},
            )
            db.add_all([player, session])
            db.flush()
            db.add(make_tag(session, 2))
            db.add(
                BallEvent(
                    session_id=session.id,
                    ball_no=1,
                    start_ms=0,
                    release_ms=100,
                    end_ms=2000,
                    confidence=0.9,
                )
            )
            db.add(  # rejected events are not balls (US-D4)
                BallEvent(
                    session_id=session.id,
                    ball_no=7,
                    start_ms=9000,
                    release_ms=9100,
                    end_ms=9900,
                    confidence=0.2,
                    valid=False,
                )
            )
            db.add(make_clip(session, 2, "C1", ClipStatus.CUT))
            db.add(
                metrics_row(
                    session,
                    1,
                    MetricPhase.CONTACT,
                    {"head_stability_score": {"value": 0.7, "unit": "score", "confidence": 0.9}},
                )
            )
            db.flush()
            records = session_ball_records(db, session.id)
        assert [r["ball_id"] for r in records] == [1, 2]
        assert records[0]["head_stability_score"] == 0.7
        assert records[1]["line"] == "outside_off"
        assert records[1]["clips"] != {}

    def test_unknown_session_is_loud(self) -> None:
        engine = make_engine("sqlite://")
        create_all(engine)
        factory = make_session_factory(engine)
        with factory() as db, pytest.raises(BallRecordError, match="session not found"):
            session_ball_records(db, uuid.uuid4())


class TestProducerCompatibility:
    def test_manual_and_cv_paths_share_the_schema_shape(self) -> None:
        # US-G1 IT: the manual-tag producer and the CV-metrics producer emit
        # byte-compatible records — identical canonical field set, both valid.
        session = make_session(machine_settings={"speed_kph": 90})
        manual = _assemble(session, 1, tag=make_tag(session, 1))
        cv = _assemble(
            session,
            2,
            metrics=[
                metrics_row(
                    session,
                    2,
                    MetricPhase.FLIGHT,
                    {"line": {"value": "off", "unit": "class", "confidence": 0.9, "source": "cv"}},
                )
            ],
        )
        assert set(manual) == set(cv)
        validate_ball_record(manual)
        validate_ball_record(cv)
