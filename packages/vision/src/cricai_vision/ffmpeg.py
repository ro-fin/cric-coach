"""FFmpeg clip-cutting foundation (US-D2): pure command builder + subprocess adapter.

Mirrors the ``cricai_data.probe`` error split so callers can distinguish "this
video cannot be cut" (:class:`FfmpegError`) from "ffmpeg itself is unavailable,
retry later" (:class:`FfmpegUnavailableError`).

Error contract of :func:`run_ffmpeg` — nothing escapes raw:

- :class:`FfmpegUnavailableError`: the ffmpeg process could not be launched
  at all (binary missing, bad permissions — any :class:`OSError`). A host
  problem; retrying the same input later can succeed.
- :class:`FfmpegTimeoutError` (an :class:`FfmpegError`): ffmpeg launched but
  ran past the per-clip timeout. Treated as a PER-FILE failure — a
  pathological/corrupt source that hangs ffmpeg would hang it again on every
  retry, so callers must fail that clip and continue, not abort the run.
- :class:`FfmpegError`: ffmpeg ran and exited non-zero (bad input, bad window).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Default pre/post-roll around the event window (US-D2: release -1.5s to end +1.5s).
DEFAULT_PRE_ROLL_MS = 1500
DEFAULT_POST_ROLL_MS = 1500

_TIMEOUT_S = 120.0


class FfmpegError(RuntimeError):
    """ffmpeg ran but could not produce the clip (bad input, bad window, hang)."""


class FfmpegTimeoutError(FfmpegError):
    """ffmpeg exceeded the per-clip timeout — a per-input failure, not unavailability."""


class FfmpegUnavailableError(FfmpegError):
    """ffmpeg missing or not runnable — the clip job can be retried later."""


@dataclass(frozen=True)
class ClipWindow:
    """A per-ball cut window on the source video's timeline, in milliseconds."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0:
            raise FfmpegError(f"clip start must be >= 0, got {self.start_ms}")
        if self.end_ms <= self.start_ms:
            raise FfmpegError(f"clip end {self.end_ms} must be after start {self.start_ms}")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def clip_window(
    event_start_ms: int,
    event_end_ms: int,
    *,
    video_duration_ms: int | None = None,
    pre_roll_ms: int = DEFAULT_PRE_ROLL_MS,
    post_roll_ms: int = DEFAULT_POST_ROLL_MS,
) -> ClipWindow:
    """Event window -> cut window with pre/post-roll, clamped to the media.

    The start clamps at 0; when ``video_duration_ms`` is known the end clamps
    at it, so a stored window never overstates the actual media (the last
    ball's post-roll would otherwise claim footage past EOF that ffmpeg
    silently cuts short). An event lying entirely past the media end raises
    :class:`FfmpegError` like any other unusable window.
    """
    if event_end_ms <= event_start_ms:
        raise FfmpegError(f"event end {event_end_ms} must be after start {event_start_ms}")
    if pre_roll_ms < 0 or post_roll_ms < 0:
        raise FfmpegError("pre/post roll must be >= 0")
    end_ms = event_end_ms + post_roll_ms
    if video_duration_ms is not None:
        end_ms = min(end_ms, video_duration_ms)
    return ClipWindow(
        start_ms=max(0, event_start_ms - pre_roll_ms),
        end_ms=end_ms,
    )


def _fmt_ms(ms: int) -> str:
    """Milliseconds -> ffmpeg time string (seconds with millisecond precision)."""
    return f"{ms / 1000:.3f}"


def build_clip_command(
    source: Path,
    dest: Path,
    window: ClipWindow,
    *,
    ffmpeg: str = "ffmpeg",
    reencode: bool = True,
) -> list[str]:
    """The exact ffmpeg argv for one clip.

    Frame-accurate boundaries (US-D2 AC: within ±2 frames) require re-encoding:
    input seeking (-ss before -i) is keyframe-fast but output-decoded frames start
    exactly at the requested time only when re-encoding. ``reencode=False`` gives
    the fast stream-copy variant (keyframe-aligned, NOT frame-accurate) for previews.
    """
    base = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        _fmt_ms(window.start_ms),
        "-i",
        str(source),
        "-t",
        _fmt_ms(window.duration_ms),
    ]
    codec = (
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac"]
        if reencode
        else ["-c", "copy"]
    )
    return [*base, *codec, "-movflags", "+faststart", str(dest)]


def find_ffmpeg() -> str:
    """Locate ffmpeg; raises :class:`FfmpegUnavailableError` when absent."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise FfmpegUnavailableError("ffmpeg not found: install ffmpeg or set PATH")
    return path


def run_ffmpeg(command: list[str], *, timeout_s: float = _TIMEOUT_S) -> None:
    """Run a built command; classify failures per the module error contract.

    Every launch failure in the :class:`OSError` family (missing binary,
    ``PermissionError``, ...) maps to :class:`FfmpegUnavailableError`; a hang
    past ``timeout_s`` maps to the per-file :class:`FfmpegTimeoutError`.
    """
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegTimeoutError(f"ffmpeg timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise FfmpegUnavailableError(f"ffmpeg not runnable: {exc}") from exc
    if completed.returncode != 0:
        lines = (completed.stderr or "").strip().splitlines()
        detail = lines[-1] if lines else "no output"
        raise FfmpegError(f"ffmpeg failed (rc={completed.returncode}): {detail}")
