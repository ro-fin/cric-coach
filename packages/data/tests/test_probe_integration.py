"""US-B2 integration: probe a real 1-second synthetic clip with real ffmpeg/ffprobe."""

import shutil
import subprocess
from pathlib import Path

import pytest
from cricai_data.probe import ProbeError, probe_video

pytestmark = pytest.mark.integration


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("ffmpeg not installed")
    return binary


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    """1s 320x240 30fps synthetic test pattern encoded to H.264 MP4."""
    out = tmp_path / "clip.mp4"
    subprocess.run(
        [
            _ffmpeg(),
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=30",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return out


def test_probe_real_clip_from_path(clip: Path) -> None:
    result = probe_video(clip)
    assert result.width == 320
    assert result.height == 240
    assert result.resolution == "320x240"
    assert result.fps == pytest.approx(30.0, abs=0.5)
    assert result.duration_s == pytest.approx(1.0, abs=0.5)
    assert result.codec == "h264"


def test_probe_real_clip_from_bytes(clip: Path) -> None:
    result = probe_video(clip.read_bytes())
    assert result.resolution == "320x240"
    assert result.fps == pytest.approx(30.0, abs=0.5)


def test_probe_real_garbage_fails(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is definitely not a video")
    with pytest.raises(ProbeError):
        probe_video(junk)
