"""FFmpeg foundation tests (US-D2): window math, command builder, runner error split."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from cricai_vision.ffmpeg import (
    ClipWindow,
    FfmpegError,
    FfmpegTimeoutError,
    FfmpegUnavailableError,
    build_clip_command,
    clip_window,
    find_ffmpeg,
    run_ffmpeg,
)


def test_clip_window_applies_rolls_and_clamps_at_zero() -> None:
    window = clip_window(1000, 4000)
    assert (window.start_ms, window.end_ms) == (0, 5500)  # 1000-1500 clamps to 0
    window = clip_window(10_000, 12_000, pre_roll_ms=500, post_roll_ms=250)
    assert (window.start_ms, window.end_ms) == (9500, 12_250)
    assert window.duration_ms == 2750


def test_clip_window_clamps_post_roll_at_video_duration() -> None:
    # Last ball of a 15s recording: the +1.5s post-roll must not overstate EOF.
    window = clip_window(10_000, 14_000, video_duration_ms=15_000)
    assert (window.start_ms, window.end_ms) == (8500, 15_000)
    # Plenty of media left: the duration clamp changes nothing.
    window = clip_window(10_000, 14_000, video_duration_ms=60_000)
    assert (window.start_ms, window.end_ms) == (8500, 15_500)
    # An event lying entirely past the media end is an unusable window.
    with pytest.raises(FfmpegError, match="must be after start"):
        clip_window(20_000, 24_000, video_duration_ms=15_000)


@pytest.mark.parametrize(
    ("start", "end", "pre", "post", "match"),
    [
        (5000, 5000, 0, 0, "must be after start"),
        (5000, 4000, 0, 0, "must be after start"),
        (0, 1000, -1, 0, "roll must be"),
        (0, 1000, 0, -1, "roll must be"),
    ],
)
def test_clip_window_rejects_bad_inputs(
    start: int, end: int, pre: int, post: int, match: str
) -> None:
    with pytest.raises(FfmpegError, match=match):
        clip_window(start, end, pre_roll_ms=pre, post_roll_ms=post)


def test_clip_window_dataclass_validation() -> None:
    with pytest.raises(FfmpegError, match="start must be >= 0"):
        ClipWindow(start_ms=-1, end_ms=100)
    with pytest.raises(FfmpegError, match="must be after start"):
        ClipWindow(start_ms=100, end_ms=100)


def test_build_clip_command_reencode_and_copy() -> None:
    window = ClipWindow(start_ms=1500, end_ms=6750)
    argv = build_clip_command(Path("in.mp4"), Path("out.mp4"), window)
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-ss") + 1] == "1.500"
    assert argv[argv.index("-t") + 1] == "5.250"
    assert "libx264" in argv
    assert argv[-1] == "out.mp4"

    copy_argv = build_clip_command(
        Path("in.mp4"), Path("out.mp4"), window, ffmpeg="/usr/bin/ffmpeg", reencode=False
    )
    assert copy_argv[0] == "/usr/bin/ffmpeg"
    assert copy_argv[copy_argv.index("-c") + 1] == "copy"
    assert "libx264" not in copy_argv


def test_find_ffmpeg_present_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cricai_vision.ffmpeg.shutil.which", lambda _: "/opt/bin/ffmpeg")
    assert find_ffmpeg() == "/opt/bin/ffmpeg"
    monkeypatch.setattr("cricai_vision.ffmpeg.shutil.which", lambda _: None)
    with pytest.raises(FfmpegUnavailableError, match="ffmpeg not found"):
        find_ffmpeg()


SubprocessCall = tuple[list[str], dict[str, object]]


def _expected_kwargs(timeout_s: float) -> dict[str, object]:
    """The exact subprocess.run kwargs run_ffmpeg must use: captured/decoded
    output for stderr-based errors, a hang-breaking timeout, no check-raise."""
    return {"capture_output": True, "timeout": timeout_s, "check": False, "text": True}


def _recording_fake(
    calls: list[SubprocessCall],
    *,
    returncode: int = 0,
    stderr: str = "",
    raises: Exception | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)

    return fake_run


def test_run_ffmpeg_success_passes_exact_argv_and_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[SubprocessCall] = []
    monkeypatch.setattr("cricai_vision.ffmpeg.subprocess.run", _recording_fake(calls))
    run_ffmpeg(["ffmpeg", "-i", "in.mp4", "out.mp4"])
    assert calls == [(["ffmpeg", "-i", "in.mp4", "out.mp4"], _expected_kwargs(120.0))]

    calls.clear()
    run_ffmpeg(["ffmpeg"], timeout_s=5.0)
    assert calls == [(["ffmpeg"], _expected_kwargs(5.0))]


def test_run_ffmpeg_failure_reports_last_stderr_line(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[SubprocessCall] = []
    fake = _recording_fake(calls, returncode=1, stderr="warning\nbad input\n")
    monkeypatch.setattr("cricai_vision.ffmpeg.subprocess.run", fake)
    with pytest.raises(FfmpegError, match=r"rc=1.*bad input"):
        run_ffmpeg(["ffmpeg"])
    assert calls == [(["ffmpeg"], _expected_kwargs(120.0))]


def test_run_ffmpeg_failure_without_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[SubprocessCall] = []
    monkeypatch.setattr("cricai_vision.ffmpeg.subprocess.run", _recording_fake(calls, returncode=2))
    with pytest.raises(FfmpegError, match="no output"):
        run_ffmpeg(["ffmpeg"])
    assert calls == [(["ffmpeg"], _expected_kwargs(120.0))]


def test_run_ffmpeg_timeout_is_a_per_file_ffmpeg_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hang past the timeout is a per-input failure, never 'unavailable'
    (an unavailable classification would livelock the clip job on one bad
    source); the configured timeout must actually reach subprocess.run."""
    calls: list[SubprocessCall] = []
    fake = _recording_fake(calls, raises=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1.0))
    monkeypatch.setattr("cricai_vision.ffmpeg.subprocess.run", fake)
    with pytest.raises(FfmpegTimeoutError, match="timed out") as excinfo:
        run_ffmpeg(["ffmpeg"], timeout_s=1.0)
    assert not isinstance(excinfo.value, FfmpegUnavailableError)
    assert calls == [(["ffmpeg"], _expected_kwargs(1.0))]


@pytest.mark.parametrize(
    "launch_error",
    [
        FileNotFoundError("ffmpeg"),
        PermissionError(13, "Permission denied", "/opt/bin/ffmpeg"),
        NotADirectoryError(20, "Not a directory", "/opt/bin/ffmpeg/ffmpeg"),
    ],
)
def test_run_ffmpeg_any_launch_oserror_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, launch_error: OSError
) -> None:
    """The whole OSError family maps to FfmpegUnavailableError — no raw escape."""
    calls: list[SubprocessCall] = []
    monkeypatch.setattr(
        "cricai_vision.ffmpeg.subprocess.run", _recording_fake(calls, raises=launch_error)
    )
    with pytest.raises(FfmpegUnavailableError, match="not runnable"):
        run_ffmpeg(["ffmpeg"])
    assert calls == [(["ffmpeg"], _expected_kwargs(120.0))]
