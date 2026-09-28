"""OpenAPI dump for the dashboard contract test (Phase 8, T1).

``apps/web/test/contract.test.ts`` checks the dashboard's hand-written API
mirrors against ``apps/web/test/openapi.json``. That only means something if
the checked-in dump is the API's CURRENT schema, so this test fails whenever
a router change has not been followed by ``uv run scripts/dump_openapi.py``.

``scripts`` is not an importable package, so the module is loaded by path
(the repo's established pattern, see test_verify_deploy.py).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "dump_openapi", _REPO_ROOT / "scripts" / "dump_openapi.py"
)
assert _spec is not None and _spec.loader is not None
dump_openapi = importlib.util.module_from_spec(_spec)
sys.modules["dump_openapi"] = dump_openapi
_spec.loader.exec_module(dump_openapi)


def test_checked_in_dump_is_current() -> None:
    assert dump_openapi.main(["--check"]) == 0, (
        "apps/web/test/openapi.json is stale: run `uv run scripts/dump_openapi.py`"
    )


def test_dump_covers_every_type_the_dashboard_mirrors() -> None:
    schemas = dump_openapi.build_schema()["components"]["schemas"]
    for name in (
        "SessionOut",
        "SessionPage",
        "TagOut",
        "TagAuditOut",
        "EventOut",
        "ClipOut",
        "VideoOut",
        "PhaseMetricsOut",
        "ReviewQueueItemOut",
        "PublishOut",
    ):
        assert name in schemas, name


def test_render_is_deterministic() -> None:
    schema = dump_openapi.build_schema()
    first = dump_openapi.render(schema)
    assert first == dump_openapi.render(dump_openapi.build_schema())
    assert first.endswith("}\n")
    assert json.loads(first) == schema


def test_writes_then_checks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "nested" / "openapi.json"
    assert dump_openapi.main(["--check", "--out", str(out)]) == 1
    assert "stale" in capsys.readouterr().err

    assert dump_openapi.main(["--out", str(out)]) == 0
    assert out.read_bytes().count(b"\r\n") == 0
    assert dump_openapi.main(["--check", "--out", str(out)]) == 0
    assert "is current" in capsys.readouterr().out

    out.write_text(
        out.read_text(encoding="utf-8").replace("cricAI API", "drifted"), encoding="utf-8"
    )
    assert dump_openapi.main(["--check", "--out", str(out)]) == 1
