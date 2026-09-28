"""US-B2: ffprobe adapter — rate parsing, subprocess handling, payload validation."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cricai_data.probe import (
    ProbeError,
    ProbeUnavailableError,
    VideoProbe,
    parse_rate,
    probe_video,
)


@pytest.fixture(autouse=True)
def _ffprobe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve 'ffprobe' to an existing executable; subprocess.run is mocked anyway."""
    monkeypatch.setenv("FFPROBE", sys.executable)


def _payload(
    stream_overrides: dict[str, Any] | None = None,
    format_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stream: dict[str, Any] = {
        "codec_type": "video",
        "codec_name": "h264",
        "r_frame_rate": "120/1",
        "width": 1920,
        "height": 1080,
    }
    stream.update(stream_overrides or {})
    fmt: dict[str, Any] = {"duration": "60.000000"}
    fmt.update(format_overrides or {})
    return {"streams": [{"codec_type": "audio"}, stream], "format": fmt}


def _install_run(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any] | None = None,
    stdout: bytes | None = None,
    returncode: int = 0,
    stderr: bytes = b"",
    exc: BaseException | None = None,
) -> list[list[str]]:
    """Replace subprocess.run with a canned response; returns the observed commands."""
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if exc is not None:
            raise exc
        out = stdout if stdout is not None else json.dumps(payload).encode()
        return subprocess.CompletedProcess(command, returncode, out, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_parse_rate_fractions_and_decimals() -> None:
    assert parse_rate("120/1") == 120.0
    assert parse_rate("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert parse_rate("29.97") == pytest.approx(29.97)


@pytest.mark.parametrize("rate", ["0/0", "10/0", "0/1", "-24/1", "abc", "1/x", ""])
def test_parse_rate_rejects_invalid(rate: str) -> None:
    with pytest.raises(ProbeError):
        parse_rate(rate)


def test_probe_video_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_run(monkeypatch, payload=_payload())
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake video bytes")
    result = probe_video(clip)
    assert result == VideoProbe(
        fps=120.0, width=1920, height=1080, resolution="1920x1080", codec="h264", duration_s=60.0
    )
    (command,) = calls
    assert command[0] == sys.executable  # FFPROBE override honored
    assert "-show_streams" in command
    assert "-show_format" in command
    assert command[-1] == str(clip)


def test_probe_video_accepts_str_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, payload=_payload())
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    assert probe_video(str(clip)).fps == 120.0


def test_probe_video_accepts_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    source = b"synthetic-video-bytes"
    seen: dict[str, bytes] = {}

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen["staged"] = Path(command[-1]).read_bytes()
        return subprocess.CompletedProcess(command, 0, json.dumps(_payload()).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    result = probe_video(source)
    assert seen["staged"] == source  # bytes are staged to a temp file for ffprobe
    assert result.resolution == "1920x1080"


def test_probe_unavailable_is_a_probe_error() -> None:
    """Callers that only catch ProbeError keep working (US-B2 compat)."""
    assert issubclass(ProbeUnavailableError, ProbeError)


def test_ffprobe_missing_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    with pytest.raises(ProbeUnavailableError, match="not found"):
        probe_video(b"x")


def test_ffprobe_default_name_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FFPROBE", raising=False)
    seen: list[str] = []

    def which(cmd: str) -> str | None:
        seen.append(cmd)
        return "/fake/ffprobe"

    monkeypatch.setattr(shutil, "which", which)
    calls = _install_run(monkeypatch, payload=_payload())
    probe_video(b"x")
    assert seen == ["ffprobe"]
    assert calls[0][0] == "/fake/ffprobe"


def test_nonzero_exit_raises_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undecodable file is a plain ProbeError — the probe DID run (US-B2)."""
    _install_run(monkeypatch, stdout=b"", returncode=1, stderr=b"moov atom not found")
    with pytest.raises(ProbeError, match="moov atom not found") as excinfo:
        probe_video(b"x")
    assert not isinstance(excinfo.value, ProbeUnavailableError)


def test_timeout_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, exc=subprocess.TimeoutExpired(cmd="ffprobe", timeout=30))
    with pytest.raises(ProbeUnavailableError, match="timed out"):
        probe_video(b"x")


def test_oserror_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, exc=OSError("exec format error"))
    with pytest.raises(ProbeUnavailableError, match="could not run"):
        probe_video(b"x")


def test_malformed_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, stdout=b"{not json")
    with pytest.raises(ProbeError, match="malformed JSON") as excinfo:
        probe_video(b"x")
    assert not isinstance(excinfo.value, ProbeUnavailableError)


def test_non_object_payload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, stdout=b"[]")
    with pytest.raises(ProbeError, match="payload shape"):
        probe_video(b"x")


def test_missing_streams_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, stdout=b'{"format": {"duration": "1.0"}}')
    with pytest.raises(ProbeError, match="no video stream"):
        probe_video(b"x")


def test_audio_only_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"streams": [{"codec_type": "audio"}], "format": {"duration": "1.0"}}
    _install_run(monkeypatch, payload=payload)
    with pytest.raises(ProbeError, match="no video stream"):
        probe_video(b"x")


def test_missing_width_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    del payload["streams"][1]["width"]
    _install_run(monkeypatch, payload=payload)
    with pytest.raises(ProbeError, match="missing fields"):
        probe_video(b"x")


def test_null_height_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_run(monkeypatch, payload=_payload(stream_overrides={"height": None}))
    with pytest.raises(ProbeError, match="missing fields"):
        probe_video(b"x")


def test_missing_duration_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    del payload["format"]["duration"]
    _install_run(monkeypatch, payload=payload)
    with pytest.raises(ProbeError, match="missing fields"):
        probe_video(b"x")


def test_missing_frame_rate_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    del payload["streams"][1]["r_frame_rate"]
    _install_run(monkeypatch, payload=payload)
    with pytest.raises(ProbeError, match="frame rate"):
        probe_video(b"x")


def test_missing_codec_name_defaults_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    del payload["streams"][1]["codec_name"]
    _install_run(monkeypatch, payload=payload)
    assert probe_video(b"x").codec == "unknown"
