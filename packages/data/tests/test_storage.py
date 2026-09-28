"""US-B2: object store — layout contract, atomicity, resumable multipart."""

from pathlib import Path

import pytest
from cricai_data.storage import (
    FsObjectStore,
    ObjectStore,
    StorageError,
    clip_key,
    sha256_hex,
    video_key,
)


@pytest.fixture
def store(tmp_path: Path) -> FsObjectStore:
    return FsObjectStore(tmp_path / "store")


def test_fs_store_satisfies_object_store_protocol(store: FsObjectStore) -> None:
    # Structural check enforced by mypy: keeps FsObjectStore and the Protocol in sync.
    protocol_view: ObjectStore = store
    assert protocol_view is store


def test_video_key_layout_contract() -> None:
    assert video_key("s1", "C1", "a.mp4") == "sessions/s1/C1/a.mp4"


@pytest.mark.parametrize(
    ("session_id", "camera_id", "filename"),
    [("", "C1", "a.mp4"), ("s/1", "C1", "a.mp4"), ("s1", "..", "a.mp4"), ("s1", "C1", "")],
)
def test_video_key_rejects_traversal(session_id: str, camera_id: str, filename: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        video_key(session_id, camera_id, filename)


def test_clip_key_layout_and_validation() -> None:
    assert clip_key("s1", 12, "C3") == "sessions/s1/balls/12/C3.mp4"
    with pytest.raises(ValueError, match="ball_no"):
        clip_key("s1", 0, "C3")


def test_put_get_exists_size_delete_roundtrip(store: FsObjectStore) -> None:
    key = video_key("s1", "C1", "a.mp4")
    assert not store.exists(key)
    store.put(key, b"hello")
    assert store.exists(key)
    assert store.get(key) == b"hello"
    assert store.size(key) == 5
    assert store.delete(key)
    assert not store.exists(key)
    assert not store.delete(key)  # second delete reports missing


def test_get_and_size_raise_on_missing(store: FsObjectStore) -> None:
    with pytest.raises(StorageError, match="not found"):
        store.get("sessions/x/C1/missing.mp4")
    with pytest.raises(StorageError, match="not found"):
        store.size("sessions/x/C1/missing.mp4")


def test_invalid_keys_rejected(store: FsObjectStore) -> None:
    with pytest.raises(StorageError, match="invalid object key"):
        store.put("/absolute", b"x")
    with pytest.raises(StorageError, match="invalid object key"):
        store.get("../escape")


def test_list_keys_sorted_and_prefix_scoped(store: FsObjectStore) -> None:
    store.put("sessions/s1/C2/b.mp4", b"2")
    store.put("sessions/s1/C1/a.mp4", b"1")
    store.put("sessions/s2/C1/c.mp4", b"3")
    assert store.list_keys("sessions/s1") == [
        "sessions/s1/C1/a.mp4",
        "sessions/s1/C2/b.mp4",
    ]
    assert store.list_keys("sessions/nope") == []


def test_put_does_not_clobber_tmp_sibling_object(store: FsObjectStore) -> None:
    # Staging lives outside the objects tree, so a real object whose key is
    # target + '.tmp' survives a put of the target key (and vice versa).
    store.put("sessions/s1/C1/a.mp4.tmp", b"tmp-named object")
    store.put("sessions/s1/C1/a.mp4", b"real object")
    assert store.get("sessions/s1/C1/a.mp4.tmp") == b"tmp-named object"
    assert store.get("sessions/s1/C1/a.mp4") == b"real object"


def test_complete_multipart_does_not_clobber_tmp_sibling_object(store: FsObjectStore) -> None:
    store.put("sessions/s1/C1/f.mp4.tmp", b"tmp-named object")
    upload_id = store.begin_multipart()
    store.put_part(upload_id, 1, b"aaa")
    store.complete_multipart(upload_id, "sessions/s1/C1/f.mp4", part_count=1)
    assert store.get("sessions/s1/C1/f.mp4.tmp") == b"tmp-named object"
    assert store.get("sessions/s1/C1/f.mp4") == b"aaa"


def test_copy_duplicates_object_without_touching_source(store: FsObjectStore) -> None:
    store.put("sessions/s1/C1/a.mp4", b"payload")
    store.copy("sessions/s1/C1/a.mp4", "sessions/s2/C1/a.mp4")
    assert store.get("sessions/s2/C1/a.mp4") == b"payload"
    assert store.get("sessions/s1/C1/a.mp4") == b"payload"


def test_copy_missing_source_raises(store: FsObjectStore) -> None:
    with pytest.raises(StorageError, match="not found"):
        store.copy("sessions/x/C1/missing.mp4", "sessions/s1/C1/a.mp4")
    assert not store.exists("sessions/s1/C1/a.mp4")


def test_copy_rejects_invalid_keys(store: FsObjectStore) -> None:
    store.put("sessions/s1/C1/a.mp4", b"payload")
    with pytest.raises(StorageError, match="invalid object key"):
        store.copy("../escape", "sessions/s1/C1/b.mp4")
    with pytest.raises(StorageError, match="invalid object key"):
        store.copy("sessions/s1/C1/a.mp4", "../escape")


def test_open_read_streams_object_bytes(store: FsObjectStore) -> None:
    store.put("sessions/s1/C1/a.mp4", b"stream me")
    with store.open_read("sessions/s1/C1/a.mp4") as fh:
        assert fh.read(6) == b"stream"
        assert fh.read() == b" me"


def test_open_read_missing_object_raises(store: FsObjectStore) -> None:
    with pytest.raises(StorageError, match="not found"):
        store.open_read("sessions/x/C1/missing.mp4")


def test_write_to_path_streams_object_to_local_file(store: FsObjectStore, tmp_path: Path) -> None:
    store.put("sessions/s1/C1/a.mp4", b"probe bytes")
    dest = tmp_path / "probe.mp4"
    store.write_to_path("sessions/s1/C1/a.mp4", dest)
    assert dest.read_bytes() == b"probe bytes"


def test_write_to_path_missing_object_raises(store: FsObjectStore, tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="not found"):
        store.write_to_path("sessions/x/C1/missing.mp4", tmp_path / "probe.mp4")
    assert not (tmp_path / "probe.mp4").exists()


def test_multipart_happy_path(store: FsObjectStore) -> None:
    upload_id = store.begin_multipart()
    assert store.put_part(upload_id, 1, b"aaa") == sha256_hex(b"aaa")
    store.put_part(upload_id, 2, b"bbb")
    digest = store.complete_multipart(upload_id, "sessions/s1/C1/f.mp4", part_count=2)
    assert digest == sha256_hex(b"aaabbb")
    assert store.get("sessions/s1/C1/f.mp4") == b"aaabbb"
    # upload staging is cleaned up
    with pytest.raises(StorageError, match="unknown upload"):
        store.list_parts(upload_id)


def test_multipart_resume_after_interruption(store: FsObjectStore) -> None:
    upload_id = store.begin_multipart()
    store.put_part(upload_id, 1, b"aaa")
    store.put_part(upload_id, 3, b"ccc")
    # client asks what arrived, then fills the gap — and may re-send a part
    assert store.list_parts(upload_id) == {1: 3, 3: 3}
    store.put_part(upload_id, 2, b"XXX")
    store.put_part(upload_id, 2, b"bbb")  # re-upload replaces
    digest = store.complete_multipart(upload_id, "sessions/s1/C1/r.mp4", part_count=3)
    assert digest == sha256_hex(b"aaabbbccc")


def test_multipart_incomplete_never_finalizes(store: FsObjectStore) -> None:
    upload_id = store.begin_multipart()
    store.put_part(upload_id, 1, b"aaa")
    with pytest.raises(StorageError, match=r"missing parts: \[2\]"):
        store.complete_multipart(upload_id, "sessions/s1/C1/x.mp4", part_count=2)
    assert not store.exists("sessions/s1/C1/x.mp4")  # partial never visible


@pytest.mark.parametrize(
    "bad_id",
    ["../objects", "/etc/passwd", "", "x/y"],
    ids=["traversal", "absolute", "empty", "nested"],
)
def test_multipart_rejects_malformed_upload_ids(store: FsObjectStore, bad_id: str) -> None:
    # upload_id feeds a filesystem path: anything but a uuid4 hex is traversal.
    store.put("sessions/s1/C1/a.mp4", b"must survive")
    with pytest.raises(StorageError, match="invalid upload id"):
        store.put_part(bad_id, 1, b"x")
    with pytest.raises(StorageError, match="invalid upload id"):
        store.list_parts(bad_id)
    with pytest.raises(StorageError, match="invalid upload id"):
        store.complete_multipart(bad_id, "sessions/s1/C1/b.mp4", part_count=1)
    with pytest.raises(StorageError, match="invalid upload id"):
        store.abort_multipart(bad_id)
    # e.g. abort_multipart('../objects') must never rmtree the objects tree
    assert store.get("sessions/s1/C1/a.mp4") == b"must survive"


def test_multipart_part_validation_and_abort(store: FsObjectStore) -> None:
    upload_id = store.begin_multipart()
    with pytest.raises(StorageError, match="part_no"):
        store.put_part(upload_id, 0, b"x")
    store.abort_multipart(upload_id)
    with pytest.raises(StorageError, match="unknown upload"):
        store.put_part(upload_id, 1, b"x")
    with pytest.raises(StorageError, match="unknown upload"):
        store.abort_multipart(upload_id)
