"""ffprobe adapter (US-B2): measure a video's real fps/resolution/codec/duration.

The server never trusts client-claimed metadata: once an upload is assembled,
the file is probed and claims are compared against measured values. ffprobe is
located on PATH (``FFPROBE`` env var overrides) and its JSON output is parsed
into a :class:`VideoProbe`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Generous cap — probing reads headers, not frames; a hang means a broken file.
PROBE_TIMEOUT_S = 30.0

_FFPROBE_ARGS = ("-v", "error", "-print_format", "json", "-show_streams", "-show_format")


class ProbeError(Exception):
    """The probe ran but the file could not be decoded or produced unusable output.

    Callers treat this as evidence the video itself is corrupted (US-B2).
    """


class ProbeUnavailableError(ProbeError):
    """ffprobe could not deliver a verdict: binary missing, not runnable, or timed out.

    A retryable environment problem — the video must NOT be treated as
    corrupted; a later re-probe on a healthy host can still succeed.
    """


@dataclass(frozen=True)
class VideoProbe:
    """Measured video metadata — the source of truth over client claims."""

    fps: float
    width: int
    height: int
    resolution: str  # "WxH", e.g. "1920x1080"
    codec: str
    duration_s: float


def parse_rate(rate: str) -> float:
    """Parse an ffprobe rational like ``'120/1'`` (or plain ``'29.97'``) into fps."""
    num_text, _, den_text = rate.partition("/")
    try:
        num = float(num_text)
        den = float(den_text) if den_text else 1.0
    except ValueError as exc:
        raise ProbeError(f"unparseable frame rate: {rate!r}") from exc
    if den == 0 or num <= 0:
        raise ProbeError(f"invalid frame rate: {rate!r}")
    return num / den


def find_ffprobe() -> str:
    """Locate the ffprobe binary (``FFPROBE`` env var overrides the PATH lookup)."""
    binary = shutil.which(os.environ.get("FFPROBE", "ffprobe"))
    if binary is None:
        raise ProbeUnavailableError("ffprobe not found: install ffmpeg or set FFPROBE")
    return binary


def probe_video(source: str | Path | bytes) -> VideoProbe:
    """Probe a video given a file path or raw bytes (staged to a temp file)."""
    if isinstance(source, bytes):
        with tempfile.NamedTemporaryFile(suffix=".video") as handle:
            handle.write(source)
            handle.flush()
            return _probe_path(Path(handle.name))
    return _probe_path(Path(source))


def _probe_path(path: Path) -> VideoProbe:
    command = [find_ffprobe(), *_FFPROBE_ARGS, str(path)]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeUnavailableError(f"ffprobe timed out after {PROBE_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise ProbeUnavailableError(f"ffprobe could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise ProbeError(f"ffprobe failed (exit {completed.returncode}): {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe produced malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProbeError("ffprobe produced an unexpected payload shape")
    return _parse_payload(payload)


def _parse_payload(payload: dict[str, Any]) -> VideoProbe:
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError("no video stream found")
    fps = parse_rate(str(video.get("r_frame_rate", "")))
    try:
        width = int(video["width"])
        height = int(video["height"])
        duration_s = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError(f"probe payload missing fields: {exc!r}") from exc
    return VideoProbe(
        fps=fps,
        width=width,
        height=height,
        resolution=f"{width}x{height}",
        codec=str(video.get("codec_name", "unknown")),
        duration_s=duration_s,
    )
