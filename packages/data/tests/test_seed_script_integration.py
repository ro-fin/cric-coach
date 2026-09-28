"""IT: the seed script produces a loadable demo session end-to-end (US-L2)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_seed_demo_script(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "seed_demo.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "demo_session.json" in result.stdout
    payload = json.loads((tmp_path / "demo_session.json").read_text())
    assert len(payload["balls"]) == 500
