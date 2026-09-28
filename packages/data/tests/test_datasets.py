"""US-F1 dataset assembly: versioning, split-disjointness checker, freeze/immutability."""

import datetime
import uuid
from collections.abc import Iterator

import pytest
from cricai_data.datasets import (
    DatasetError,
    FrozenDatasetError,
    SplitLeakageError,
    add_members,
    create_dataset,
    dataset_counts,
    dataset_manifest,
    dataset_members,
    freeze_dataset,
    manifest_digest,
    remove_members,
    split_violations,
)
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import AnnotationSource, BowlerSource, DatasetSplit, LabelClass, SessionType
from cricai_data.models import Annotation, Dataset, FrameSample, Player, Session
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool


@pytest.fixture
def db() -> Iterator[OrmSession]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    with make_session_factory(engine)() as session:
        yield session


def _seed_session(db: OrmSession) -> uuid.UUID:
    player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
    session = Session(
        player=player,
        session_date=datetime.date(2026, 7, 8),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add_all([player, session])
    db.flush()
    return session.id


def _seed_frame(db: OrmSession, session_id: uuid.UUID, frame_no: int) -> uuid.UUID:
    frame = FrameSample(
        session_id=session_id,
        ball_no=1,
        camera_id="C1",
        frame_no=frame_no,
        ts_ms=frame_no * 8,
        object_key=f"sessions/{session_id}/frames/C1/frame-{frame_no:06d}.jpg",
        stratum={"lighting": "daylight"},
        sampler_version="frame-sampler-1",
    )
    db.add(frame)
    db.flush()
    return frame.id


def _seed_annotation(db: OrmSession, frame_id: uuid.UUID, label: LabelClass) -> None:
    db.add(
        Annotation(
            frame_id=frame_id,
            label_class=label,
            cx=0.5,
            cy=0.5,
            w=0.1,
            h=0.1,
            annotator="mira",
            source=AnnotationSource.MANUAL,
        )
    )
    db.flush()


# --- create -------------------------------------------------------------------------


def test_create_dataset(db: OrmSession) -> None:
    dataset = create_dataset(db, version="v1", notes="first batch")
    assert dataset.version == "v1"
    assert dataset.frozen is False
    assert dataset.manifest_digest is None
    assert db.get(Dataset, dataset.id) is dataset


def test_create_dataset_rejects_blank_and_duplicate_versions(db: OrmSession) -> None:
    with pytest.raises(DatasetError, match="non-empty"):
        create_dataset(db, version="   ")
    create_dataset(db, version="v1")
    with pytest.raises(DatasetError, match="already exists"):
        create_dataset(db, version="v1")


# --- membership ---------------------------------------------------------------------


def test_add_members_with_splits(db: OrmSession) -> None:
    session_id = _seed_session(db)
    frames = [_seed_frame(db, session_id, n) for n in (1, 2)]
    dataset = create_dataset(db, version="v1")
    added = add_members(
        db, dataset, [(frames[0], DatasetSplit.TRAIN), (frames[1], DatasetSplit.VAL)]
    )
    assert added == 2
    members = dataset_members(db, dataset)
    assert sorted((str(f.id), s.value) for f, s in members) == sorted(
        [(str(frames[0]), "train"), (str(frames[1]), "val")]
    )


def test_add_members_unknown_frame_is_loud(db: OrmSession) -> None:
    dataset = create_dataset(db, version="v1")
    ghost = uuid.uuid4()
    with pytest.raises(LookupError, match=f"frame not found: {ghost}"):
        add_members(db, dataset, [(ghost, DatasetSplit.TRAIN)])


def test_add_members_duplicate_is_loud(db: OrmSession) -> None:
    session_id = _seed_session(db)
    frame = _seed_frame(db, session_id, 1)
    dataset = create_dataset(db, version="v1")
    add_members(db, dataset, [(frame, DatasetSplit.TRAIN)])
    with pytest.raises(DatasetError, match="already a member"):
        add_members(db, dataset, [(frame, DatasetSplit.TEST)])
    # ... including duplicates within one request
    frame2 = _seed_frame(db, session_id, 2)
    with pytest.raises(DatasetError, match="already a member"):
        add_members(db, dataset, [(frame2, DatasetSplit.TRAIN), (frame2, DatasetSplit.TRAIN)])


def test_remove_members(db: OrmSession) -> None:
    session_id = _seed_session(db)
    frame = _seed_frame(db, session_id, 1)
    dataset = create_dataset(db, version="v1")
    add_members(db, dataset, [(frame, DatasetSplit.TRAIN)])
    assert remove_members(db, dataset, [frame]) == 1
    assert dataset_members(db, dataset) == []
    with pytest.raises(LookupError, match="not a member"):
        remove_members(db, dataset, [frame])


# --- split disjointness (the US-F1 AC) ------------------------------------------------


def test_split_violations_flags_test_sessions_shared_with_train_or_val(db: OrmSession) -> None:
    session_a, session_b = _seed_session(db), _seed_session(db)
    dataset = create_dataset(db, version="v1")
    add_members(
        db,
        dataset,
        [
            (_seed_frame(db, session_a, 1), DatasetSplit.TRAIN),
            (_seed_frame(db, session_a, 2), DatasetSplit.TEST),  # leak: same session
            (_seed_frame(db, session_b, 1), DatasetSplit.TEST),  # clean test session
        ],
    )
    (violation,) = split_violations(db, dataset)
    assert violation.session_id == str(session_a)
    assert violation.splits == ("test", "train")


def test_train_and_val_may_share_a_session(db: OrmSession) -> None:
    session_id = _seed_session(db)
    dataset = create_dataset(db, version="v1")
    add_members(
        db,
        dataset,
        [
            (_seed_frame(db, session_id, 1), DatasetSplit.TRAIN),
            (_seed_frame(db, session_id, 2), DatasetSplit.VAL),
        ],
    )
    assert split_violations(db, dataset) == []


def test_val_test_leak_is_a_violation(db: OrmSession) -> None:
    session_id = _seed_session(db)
    dataset = create_dataset(db, version="v1")
    add_members(
        db,
        dataset,
        [
            (_seed_frame(db, session_id, 1), DatasetSplit.VAL),
            (_seed_frame(db, session_id, 2), DatasetSplit.TEST),
        ],
    )
    (violation,) = split_violations(db, dataset)
    assert violation.splits == ("test", "val")


# --- freeze + immutability -------------------------------------------------------------


def _disjoint_dataset(db: OrmSession, version: str = "v1") -> Dataset:
    train_session, test_session = _seed_session(db), _seed_session(db)
    dataset = create_dataset(db, version=version)
    add_members(
        db,
        dataset,
        [
            (_seed_frame(db, train_session, 1), DatasetSplit.TRAIN),
            (_seed_frame(db, train_session, 2), DatasetSplit.VAL),
            (_seed_frame(db, test_session, 1), DatasetSplit.TEST),
        ],
    )
    return dataset


def test_freeze_pins_digest_and_flips_frozen(db: OrmSession) -> None:
    dataset = _disjoint_dataset(db)
    digest = freeze_dataset(db, dataset)
    assert dataset.frozen is True
    assert dataset.manifest_digest == digest
    assert len(digest) == 64  # sha256 hex fits the String(64) column
    # The digest is exactly the canonical manifest's hash — recomputable forever.
    assert manifest_digest(dataset_manifest(db, dataset)) == digest


def test_manifest_is_canonical_and_membership_sensitive(db: OrmSession) -> None:
    dataset = _disjoint_dataset(db)
    manifest = dataset_manifest(db, dataset)
    assert manifest["dataset_version"] == "v1"
    assert [m["frame_id"] for m in manifest["members"]] == sorted(
        m["frame_id"] for m in manifest["members"]
    )
    before = manifest_digest(manifest)
    # Changing one split assignment changes the digest.
    train_frame = next(f for f, s in dataset_members(db, dataset) if s is DatasetSplit.TRAIN)
    remove_members(db, dataset, [train_frame.id])
    add_members(db, dataset, [(train_frame.id, DatasetSplit.VAL)])
    assert manifest_digest(dataset_manifest(db, dataset)) != before


def test_freeze_digest_pins_annotation_content(db: OrmSession) -> None:
    """A label edit behind the freeze must change the recomputed digest (US-F1)."""
    dataset = _disjoint_dataset(db)
    train_frame = next(f for f, s in dataset_members(db, dataset) if s is DatasetSplit.TRAIN)
    _seed_annotation(db, train_frame.id, LabelClass.BALL)
    digest = freeze_dataset(db, dataset)
    _seed_annotation(db, train_frame.id, LabelClass.BAT)  # label added after freeze
    assert manifest_digest(dataset_manifest(db, dataset)) != digest


def test_freeze_digest_pins_frame_pixel_identity(db: OrmSession) -> None:
    """A sampler rewrite of a member frame (new ts/ball at the same object key)
    must change the recomputed digest — the pixels behind the key changed."""
    dataset = _disjoint_dataset(db)
    digest = freeze_dataset(db, dataset)
    frame = dataset_members(db, dataset)[0][0]
    frame.ts_ms += 8  # image re-extracted at a different instant, same key
    db.flush()
    assert manifest_digest(dataset_manifest(db, dataset)) != digest


def test_manifest_annotations_are_content_sorted(db: OrmSession) -> None:
    """Insertion order of labels must not affect the manifest (digest determinism)."""
    dataset = _disjoint_dataset(db)
    train_frame = next(f for f, s in dataset_members(db, dataset) if s is DatasetSplit.TRAIN)
    _seed_annotation(db, train_frame.id, LabelClass.BAT)  # inserted out of canonical order
    _seed_annotation(db, train_frame.id, LabelClass.BALL)
    manifest = dataset_manifest(db, dataset)
    member = next(m for m in manifest["members"] if m["frame_id"] == str(train_frame.id))
    assert [a["label_class"] for a in member["annotations"]] == ["ball", "bat"]
    assert member["annotations"][0]["annotator"] == "mira"
    assert member["annotations"][0]["source"] == "manual"


def test_freeze_refuses_empty_dataset(db: OrmSession) -> None:
    dataset = create_dataset(db, version="v1")
    with pytest.raises(DatasetError, match="empty dataset"):
        freeze_dataset(db, dataset)
    assert dataset.frozen is False


def test_freeze_refuses_split_leakage_with_violations(db: OrmSession) -> None:
    session_id = _seed_session(db)
    dataset = create_dataset(db, version="v1")
    add_members(
        db,
        dataset,
        [
            (_seed_frame(db, session_id, 1), DatasetSplit.TRAIN),
            (_seed_frame(db, session_id, 2), DatasetSplit.TEST),
        ],
    )
    with pytest.raises(SplitLeakageError, match=str(session_id)) as excinfo:
        freeze_dataset(db, dataset)
    assert excinfo.value.violations[0].session_id == str(session_id)
    assert dataset.frozen is False
    assert dataset.manifest_digest is None


def test_frozen_dataset_refuses_every_mutation(db: OrmSession) -> None:
    dataset = _disjoint_dataset(db)
    freeze_dataset(db, dataset)
    member = dataset_members(db, dataset)[0]
    extra_frame = _seed_frame(db, _seed_session(db), 9)
    with pytest.raises(FrozenDatasetError, match="frozen"):
        add_members(db, dataset, [(extra_frame, DatasetSplit.TRAIN)])
    with pytest.raises(FrozenDatasetError, match="frozen"):
        remove_members(db, dataset, [member[0].id])
    with pytest.raises(FrozenDatasetError, match="frozen"):  # a second freeze is a mutation too
        freeze_dataset(db, dataset)
    # Nothing slipped through: membership and digest are exactly as frozen.
    assert len(dataset_members(db, dataset)) == 3
    assert manifest_digest(dataset_manifest(db, dataset)) == dataset.manifest_digest


# --- counts ------------------------------------------------------------------------


def test_dataset_counts_per_split_and_class(db: OrmSession) -> None:
    train_session, test_session = _seed_session(db), _seed_session(db)
    dataset = create_dataset(db, version="v1")
    train_frame = _seed_frame(db, train_session, 1)
    test_frame = _seed_frame(db, test_session, 1)
    _seed_annotation(db, train_frame, LabelClass.BALL)
    _seed_annotation(db, train_frame, LabelClass.BALL)
    _seed_annotation(db, train_frame, LabelClass.BAT)
    _seed_annotation(db, test_frame, LabelClass.STUMPS)
    add_members(db, dataset, [(train_frame, DatasetSplit.TRAIN), (test_frame, DatasetSplit.TEST)])
    counts = dataset_counts(db, dataset)
    assert counts["train"].frames == 1
    assert counts["train"].classes == {"ball": 2, "bat": 1}
    assert counts["test"].frames == 1
    assert counts["test"].classes == {"stumps": 1}
    assert counts["val"].frames == 0  # every split reported, zeros kept
    assert counts["val"].classes == {}


def test_dataset_counts_empty_dataset(db: OrmSession) -> None:
    dataset = create_dataset(db, version="v1")
    counts = dataset_counts(db, dataset)
    assert {split: c.frames for split, c in counts.items()} == {"train": 0, "val": 0, "test": 0}
