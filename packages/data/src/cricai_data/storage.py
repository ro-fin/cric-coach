"""Object storage (US-B2): S3-style protocol with a filesystem implementation.

Key layout is the system-wide contract: ``sessions/{session_id}/{camera_id}/{filename}``.
The DB stores object keys, never bytes. A MinIO/S3 client implementing the same
protocol drops in for production deployments.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol

_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")


def video_key(session_id: str, camera_id: str, filename: str) -> str:
    """Canonical object key for a session camera file (US-B2 storage layout)."""
    for part, label in (
        (session_id, "session_id"),
        (camera_id, "camera_id"),
        (filename, "filename"),
    ):
        if not part or "/" in part or ".." in part:
            raise ValueError(f"invalid {label}: {part!r}")
    return f"sessions/{session_id}/{camera_id}/{filename}"


def clip_key(session_id: str, ball_no: int, camera_id: str) -> str:
    """Canonical object key for a per-ball clip (US-D2 layout)."""
    if ball_no < 1:
        raise ValueError(f"ball_no must be >= 1, got {ball_no}")
    return f"sessions/{session_id}/balls/{ball_no}/{camera_id}.mp4"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StorageError(Exception):
    pass


class ObjectStore(Protocol):
    """Minimal S3-compatible surface the system depends on."""

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> bool: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def size(self, key: str) -> int: ...
    def copy(self, src_key: str, dst_key: str) -> None: ...
    def open_read(self, key: str) -> BinaryIO: ...
    def write_to_path(self, key: str, dest: Path) -> None: ...
    def begin_multipart(self) -> str: ...
    def put_part(self, upload_id: str, part_no: int, data: bytes) -> str: ...
    def list_parts(self, upload_id: str) -> dict[int, int]: ...
    def complete_multipart(self, upload_id: str, key: str, part_count: int) -> str: ...
    def abort_multipart(self, upload_id: str) -> None: ...


class FsObjectStore:
    """Filesystem-backed object store (LAN default, dev, tests).

    Multipart uploads land in a staging area and are promoted atomically on
    complete — partial objects are never visible under their final key (US-B2).
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._objects = root / "objects"
        self._uploads = root / "uploads"
        self._staging = root / "staging"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._uploads.mkdir(parents=True, exist_ok=True)
        self._staging.mkdir(parents=True, exist_ok=True)

    def _object_path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key:
            raise StorageError(f"invalid object key: {key!r}")
        return self._objects / key

    def _stage_path(self) -> Path:
        """Fresh scratch path outside the objects tree.

        Staging inside the objects tree (e.g. ``key + '.tmp'``) would silently
        destroy a real object whose key happens to be the staging name.
        Same filesystem as objects, so the final ``replace`` stays atomic.
        """
        return self._staging / uuid.uuid4().hex

    def put(self, key: str, data: bytes) -> None:
        path = self._object_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._stage_path()
        tmp.write_bytes(data)
        tmp.replace(path)  # os.replace: atomic within a filesystem

    def get(self, key: str) -> bytes:
        path = self._object_path(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key!r}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._object_path(key).is_file()

    def delete(self, key: str) -> bool:
        path = self._object_path(key)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def list_keys(self, prefix: str) -> list[str]:
        base = self._object_path(prefix)
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(self._objects)) for p in base.rglob("*") if p.is_file())

    def size(self, key: str) -> int:
        path = self._object_path(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key!r}")
        return path.stat().st_size

    # -- streaming (US-B2 PT contract: never load whole videos into RAM) ------

    def copy(self, src_key: str, dst_key: str) -> None:
        """Store-side copy of an object to a new key without buffering in RAM."""
        src = self._object_path(src_key)
        if not src.is_file():
            raise StorageError(f"object not found: {src_key!r}")
        dst = self._object_path(dst_key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._stage_path()
        shutil.copyfile(src, tmp)
        tmp.replace(dst)

    def open_read(self, key: str) -> BinaryIO:
        """Open an object for streamed reading; the caller must close it."""
        path = self._object_path(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key!r}")
        return path.open("rb")

    def write_to_path(self, key: str, dest: Path) -> None:
        """Stream an object's bytes into a local file (e.g. a probe temp file)."""
        path = self._object_path(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key!r}")
        shutil.copyfile(path, dest)

    # -- multipart -----------------------------------------------------------

    def begin_multipart(self) -> str:
        upload_id = uuid.uuid4().hex
        (self._uploads / upload_id).mkdir(parents=True)
        return upload_id

    def _upload_dir(self, upload_id: str) -> Path:
        # begin_multipart only ever issues uuid4().hex ids; anything else
        # (e.g. '../objects') is a path-traversal attempt, not an upload.
        if _UPLOAD_ID_RE.fullmatch(upload_id) is None:
            raise StorageError(f"invalid upload id: {upload_id!r}")
        path = self._uploads / upload_id
        if not path.is_dir():
            raise StorageError(f"unknown upload: {upload_id!r}")
        return path

    def put_part(self, upload_id: str, part_no: int, data: bytes) -> str:
        """Store one part (1-based, re-uploadable) and return its sha256."""
        if part_no < 1:
            raise StorageError(f"part_no must be >= 1, got {part_no}")
        (self._upload_dir(upload_id) / f"{part_no:06d}.part").write_bytes(data)
        return sha256_hex(data)

    def list_parts(self, upload_id: str) -> dict[int, int]:
        """Received parts → sizes; lets an interrupted client resume (US-B2)."""
        return {int(p.stem): p.stat().st_size for p in self._upload_dir(upload_id).glob("*.part")}

    def complete_multipart(self, upload_id: str, key: str, part_count: int) -> str:
        """Concatenate parts 1..part_count into the final object; returns sha256."""
        upload_dir = self._upload_dir(upload_id)
        received = self.list_parts(upload_id)
        missing = [n for n in range(1, part_count + 1) if n not in received]
        if missing:
            raise StorageError(f"upload {upload_id!r} missing parts: {missing}")

        path = self._object_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._stage_path()
        digest = hashlib.sha256()
        with tmp.open("wb") as out:
            for part_no in range(1, part_count + 1):
                data = (upload_dir / f"{part_no:06d}.part").read_bytes()
                digest.update(data)
                out.write(data)
        tmp.replace(path)
        shutil.rmtree(upload_dir)
        return digest.hexdigest()

    def abort_multipart(self, upload_id: str) -> None:
        shutil.rmtree(self._upload_dir(upload_id))
