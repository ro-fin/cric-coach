"""US-B6 unit tests: retention selection, forecast math, backup manifest drill."""

import datetime
import uuid
from typing import Any

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    Contact,
    Footwork,
    Length,
    Line,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import BallEvent, BallTag, EventCorrection, Player, Session
from cricai_data.retention import (
    BACKUP_ENTITIES,
    ForecastReport,
    ObjectInfo,
    RetentionPolicy,
    backup_manifest,
    classify_object,
    derive_protected_keys,
    select_expired,
    snapshot_entities,
    storage_forecast,
    verify_restore,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

RAW_KEY = "sessions/s1/C1/full.mp4"
CLIP_KEY = "sessions/s1/balls/12/C1.mp4"


@pytest.fixture
def db_factory() -> sessionmaker[OrmSession]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return make_session_factory(engine)


def _seed_session(db: OrmSession) -> Session:
    player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
    session = Session(
        player=player,
        session_date=datetime.date(2026, 7, 7),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add_all([player, session])
    db.flush()
    return session


def _tag(session: Session, ball_no: int, *, ground_truth_eligible: bool) -> BallTag:
    return BallTag(
        session_id=session.id,
        ball_no=ball_no,
        line=Line.OFF,
        length=Length.GOOD,
        shot=Shot.DRIVE,
        footwork=Footwork.FRONT,
        contact=Contact.MIDDLE,
        outcome=Outcome.CONTROLLED_GROUND_SHOT,
        control=True,
        ground_truth_eligible=ground_truth_eligible,
        created_by="parent",
    )


# -- classification ------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (RAW_KEY, "raw_video"),
        (CLIP_KEY, "clip"),
        ("sessions/s1/balls/12/pose/C1.json", "clip"),  # anything under balls/
        ("backups/db.sql", "other"),  # foreign prefix
        ("sessions/s1/manifest.json", "other"),  # too shallow
        ("sessions/s1/C1/sub/full.mp4", "other"),  # too deep for raw video
        ("sessions//C1/full.mp4", "other"),  # empty segment
        ("exports/s1/balls/1/C1.mp4", "other"),  # balls outside sessions/
    ],
)
def test_classify_object(key: str, expected: str) -> None:
    assert classify_object(key) == expected


# -- retention selection -------------------------------------------------------


def test_select_expired_boundary_age_equal_limit_stays() -> None:
    policy = RetentionPolicy()
    objects = [
        ObjectInfo(RAW_KEY, age_days=90.0),
        ObjectInfo(CLIP_KEY, age_days=730.0),
    ]
    assert select_expired(objects, policy, set()) == []


def test_select_expired_age_over_limit_goes_in_input_order() -> None:
    policy = RetentionPolicy()
    objects = [
        ObjectInfo(CLIP_KEY, age_days=730.5),
        ObjectInfo(RAW_KEY, age_days=90.5),
        ObjectInfo("sessions/s2/C3/full.mp4", age_days=89.9),  # still fresh
    ]
    assert select_expired(objects, policy, set()) == [CLIP_KEY, RAW_KEY]


def test_select_expired_never_returns_other_objects() -> None:
    objects = [ObjectInfo("backups/db.sql", age_days=10_000.0)]
    assert select_expired(objects, RetentionPolicy(), set()) == []


def test_select_expired_none_limit_means_forever() -> None:
    policy = RetentionPolicy(raw_video_days=None, clip_days=None)
    objects = [
        ObjectInfo(RAW_KEY, age_days=10_000.0),
        ObjectInfo(CLIP_KEY, age_days=10_000.0),
    ]
    assert select_expired(objects, policy, set()) == []


def test_select_expired_custom_policy() -> None:
    policy = RetentionPolicy(raw_video_days=7, clip_days=30)
    objects = [ObjectInfo(RAW_KEY, age_days=8.0), ObjectInfo(CLIP_KEY, age_days=29.0)]
    assert select_expired(objects, policy, set()) == [RAW_KEY]


@pytest.mark.safety
def test_select_expired_never_returns_protected_ground_truth_clips() -> None:
    """SAF: eval-set clips are immune to retention no matter how old."""
    objects = [
        ObjectInfo(CLIP_KEY, age_days=10_000.0),
        ObjectInfo("sessions/s1/balls/13/C1.mp4", age_days=10_000.0),
    ]
    expired = select_expired(objects, RetentionPolicy(), protected_keys={CLIP_KEY})
    assert expired == ["sessions/s1/balls/13/C1.mp4"]
    assert CLIP_KEY not in expired


@pytest.mark.safety
def test_select_expired_never_returns_keys_under_protected_prefixes() -> None:
    """SAF: everything under a ground-truth ball prefix is immune (US-B6)."""
    objects = [
        ObjectInfo(CLIP_KEY, age_days=10_000.0),
        ObjectInfo("sessions/s1/balls/12/pose/C1.json", age_days=10_000.0),
        ObjectInfo("sessions/s1/balls/13/C1.mp4", age_days=10_000.0),
    ]
    expired = select_expired(
        objects, RetentionPolicy(), set(), protected_prefixes={"sessions/s1/balls/12/"}
    )
    assert expired == ["sessions/s1/balls/13/C1.mp4"]


def test_select_expired_prefix_and_exact_protection_union() -> None:
    objects = [
        ObjectInfo(CLIP_KEY, age_days=10_000.0),
        ObjectInfo(RAW_KEY, age_days=10_000.0),
    ]
    expired = select_expired(
        objects, RetentionPolicy(), {RAW_KEY}, protected_prefixes={"sessions/s1/balls/12/"}
    )
    assert expired == []


# -- derived ground-truth protections -------------------------------------------


def test_derive_protected_keys_only_ground_truth_eligible_tags(
    db_factory: sessionmaker[OrmSession],
) -> None:
    with db_factory() as db:
        session = _seed_session(db)
        db.add_all(
            [
                _tag(session, 1, ground_truth_eligible=True),
                _tag(session, 2, ground_truth_eligible=False),
            ]
        )
        db.flush()
        assert derive_protected_keys(db) == {f"sessions/{session.id}/balls/1/"}


def test_derive_protected_keys_empty_without_tags(db_factory: sessionmaker[OrmSession]) -> None:
    with db_factory() as db:
        assert derive_protected_keys(db) == set()


# -- storage forecast ----------------------------------------------------------


def test_forecast_least_squares_slope_on_perfect_line() -> None:
    report = storage_forecast([(0, 0), (1, 100), (2, 200)], capacity_bytes=1_000)
    assert report == ForecastReport(
        bytes_used=200, capacity_bytes=1_000, daily_rate_bytes=100.0, days_remaining=8.0
    )


def test_forecast_least_squares_slope_on_noisy_history() -> None:
    # x̄=1, ȳ=16: slope = ((-1)(-6) + 0·(-8) + 1·14) / 2 = 10
    report = storage_forecast([(0, 10), (1, 8), (2, 30)], capacity_bytes=130)
    assert report.daily_rate_bytes == pytest.approx(10.0)
    assert report.bytes_used == 30
    assert report.days_remaining == pytest.approx(10.0)


def test_forecast_unordered_history_uses_latest_day_for_usage() -> None:
    report = storage_forecast([(2, 200), (0, 0), (1, 100)], capacity_bytes=400)
    assert report.bytes_used == 200
    assert report.days_remaining == pytest.approx(2.0)


def test_forecast_shrinking_usage_has_no_exhaustion_date() -> None:
    report = storage_forecast([(0, 100), (1, 90)], capacity_bytes=1_000)
    assert report.daily_rate_bytes == pytest.approx(-10.0)
    assert report.days_remaining is None


def test_forecast_single_point_rate_zero() -> None:
    report = storage_forecast([(0, 42)], capacity_bytes=100)
    assert report == ForecastReport(
        bytes_used=42, capacity_bytes=100, daily_rate_bytes=0.0, days_remaining=None
    )


def test_forecast_empty_history() -> None:
    report = storage_forecast([], capacity_bytes=100)
    assert report == ForecastReport(
        bytes_used=0, capacity_bytes=100, daily_rate_bytes=0.0, days_remaining=None
    )


def test_forecast_already_over_capacity_clamps_to_zero_days() -> None:
    report = storage_forecast([(0, 0), (1, 100)], capacity_bytes=50)
    assert report.days_remaining == 0.0


@pytest.mark.parametrize("capacity", [0, -1])
def test_forecast_rejects_non_positive_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity_bytes must be positive"):
        storage_forecast([(0, 1)], capacity_bytes=capacity)


# -- backup manifest & restore drill -------------------------------------------


def _snapshot() -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    entities: dict[str, list[dict[str, Any]]] = {
        "sessions": [
            {"id": uuid.UUID("00000000-0000-0000-0000-000000000001"), "notes": "evening"},
            {"id": uuid.UUID("00000000-0000-0000-0000-000000000002"), "notes": None},
        ],
        "ball_tags": [{"ball_no": 1, "at": datetime.datetime(2026, 7, 7, tzinfo=datetime.UTC)}],
    }
    return entities, [RAW_KEY, CLIP_KEY]


def test_backup_manifest_counts_and_digest() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    assert manifest.entity_counts == {"sessions": 2, "ball_tags": 1}
    assert manifest.object_count == 2
    assert len(manifest.sha256) == 64


def test_backup_manifest_is_order_independent() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    shuffled = {
        "ball_tags": entities["ball_tags"],
        "sessions": list(reversed(entities["sessions"])),
    }
    assert backup_manifest(shuffled, list(reversed(keys))).sha256 == manifest.sha256


def test_verify_restore_bit_exact_returns_no_discrepancies() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    assert verify_restore(manifest, entities, keys) == []


def test_verify_restore_missing_entity_table() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    restored = {name: rows for name, rows in entities.items() if name != "ball_tags"}
    discrepancies = verify_restore(manifest, restored, keys)
    assert "missing entity table: ball_tags" in discrepancies
    assert any("checksum mismatch" in d for d in discrepancies)


def test_verify_restore_row_count_mismatch() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    restored = entities | {"sessions": entities["sessions"][:1]}
    discrepancies = verify_restore(manifest, restored, keys)
    assert "entity count mismatch for sessions: expected 2, got 1" in discrepancies


def test_verify_restore_unexpected_entity_table() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    restored = entities | {"stray": [{"x": 1}]}
    discrepancies = verify_restore(manifest, restored, keys)
    assert "unexpected entity table: stray" in discrepancies


def test_verify_restore_extra_object_key() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    discrepancies = verify_restore(manifest, entities, [*keys, "sessions/s9/C1/extra.mp4"])
    assert "object count mismatch: expected 2, got 3" in discrepancies
    assert any("checksum mismatch" in d for d in discrepancies)


def test_verify_restore_changed_row_caught_only_by_checksum() -> None:
    entities, keys = _snapshot()
    manifest = backup_manifest(entities, keys)
    restored = entities | {"ball_tags": [{"ball_no": 2, "at": "2026-07-07T00:00:00+00:00"}]}
    discrepancies = verify_restore(manifest, restored, keys)
    assert discrepancies == ["content checksum mismatch: restored data is not bit-exact"]


@pytest.mark.safety
def test_restore_drill_catches_dropped_event_corrections_table(
    db_factory: sessionmaker[OrmSession],
) -> None:
    """SAF: losing the US-D4 human ground-truth trail is reported loudly."""
    with db_factory() as db:
        session = _seed_session(db)
        event = BallEvent(
            session_id=session.id,
            ball_no=1,
            start_ms=1000,
            release_ms=1500,
            contact_ms=2000,
            end_ms=3000,
            confidence=0.9,
        )
        db.add(event)
        db.flush()
        db.add(
            EventCorrection(
                event_id=event.id,
                action="adjust",
                before={"start_ms": 900},
                after={"start_ms": 1000},
                actor="coach",
            )
        )
        db.flush()
        event_id = str(event.id)
        snapshot = snapshot_entities(db)
    assert [row["id"] for row in snapshot["ball_events"]] == [event_id]
    assert len(snapshot["event_corrections"]) == 1

    manifest = backup_manifest(snapshot, [])
    assert manifest.entity_counts["ball_events"] == 1
    assert manifest.entity_counts["event_corrections"] == 1

    restored = {name: rows for name, rows in snapshot.items() if name != "event_corrections"}
    discrepancies = verify_restore(manifest, restored, [])
    assert "missing entity table: event_corrections" in discrepancies
    assert any("checksum mismatch" in d for d in discrepancies)


# -- DB entity snapshot ----------------------------------------------------------


def test_snapshot_entities_covers_all_backup_tables_when_empty(
    db_factory: sessionmaker[OrmSession],
) -> None:
    with db_factory() as db:
        snapshot = snapshot_entities(db)
    assert set(snapshot) == set(BACKUP_ENTITIES)
    assert set(BACKUP_ENTITIES) == {
        "players",
        "sessions",
        "session_blocks",
        "videos",
        "ball_tags",
        "bounce_marks",
        "ball_events",
        "event_corrections",
        "clips",
        "pose_tracks",
        "ball_metrics",
        "reference_balls",
        "frame_samples",
        "annotations",
        "datasets",
        "dataset_members",
        "model_runs",
        "model_versions",
        "ball_tracks",
        "bounce_estimates",
        "coaching_rules",
        "rule_overrides",
        "safety_configs",
        "reports",
        "drills",
        "drill_plans",
        "bowling_ledger_entries",
        "wellness_checkins",
        "pain_clearances",
        "evidence_verdicts",
        "bowling_targets",
        "delivery_labels",
        "coach_notes",
        "milestones",
        "app_settings",
    }
    assert all(rows == [] for rows in snapshot.values())


def test_snapshot_entities_rows_are_id_plus_field_hash(
    db_factory: sessionmaker[OrmSession],
) -> None:
    with db_factory() as db:
        session = _seed_session(db)
        db.add(_tag(session, 1, ground_truth_eligible=True))
        db.flush()
        snapshot = snapshot_entities(db)
        assert [row["id"] for row in snapshot["sessions"]] == [str(session.id)]
        (tag_row,) = snapshot["ball_tags"]
        assert set(tag_row) == {"id", "fields_sha256"}
        assert len(tag_row["fields_sha256"]) == 64


def test_snapshot_entities_hash_changes_when_a_field_changes(
    db_factory: sessionmaker[OrmSession],
) -> None:
    """A restore drill must catch edited rows, not just missing ones."""
    with db_factory() as db:
        session = _seed_session(db)
        db.flush()
        before = snapshot_entities(db)["sessions"][0]
        session.notes = "tampered after backup"
        db.flush()
        after = snapshot_entities(db)["sessions"][0]
    assert before["id"] == after["id"]
    assert before["fields_sha256"] != after["fields_sha256"]
