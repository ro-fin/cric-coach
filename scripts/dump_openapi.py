#!/usr/bin/env python3
"""Dump the cricai_api OpenAPI schema for the dashboard contract test (Phase 8, T1).

The web dashboard mirrors the API's response models by hand in
``apps/web/lib/api.ts``. ``apps/web/test/contract.test.ts`` checks those
mirrors against this dump, so a backend change that the dashboard has not
followed fails the web gate instead of failing silently on a tablet.

Usage::

    uv run scripts/dump_openapi.py            # rewrite apps/web/test/openapi.json
    uv run scripts/dump_openapi.py --check    # exit 1 if the checked-in dump is stale

The schema is generated from ``create_app`` with an in-memory SQLite engine and
a throwaway storage root; generating it touches no database or files.
Output is deterministic (sorted keys, two-space indent, trailing newline).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from cricai_api.app import create_app
from cricai_api.settings import Settings
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "apps" / "web" / "test" / "openapi.json"


def build_schema() -> dict[str, Any]:
    """Return the API's OpenAPI document."""
    with tempfile.TemporaryDirectory() as storage:
        settings = Settings(database_url="sqlite://", storage_root=Path(storage))
        engine = create_engine("sqlite://", poolclass=StaticPool)
        try:
            return create_app(settings=settings, engine=engine).openapi()
        finally:
            engine.dispose()


def render(schema: dict[str, Any]) -> str:
    """Serialise deterministically so the dump diffs cleanly."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check", action="store_true", help="fail if the file differs from a fresh dump"
    )
    args = parser.parse_args(argv)
    fresh = render(build_schema())
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != fresh:
            print(f"{args.out} is stale: run `uv run scripts/dump_openapi.py`", file=sys.stderr)
            return 1
        print(f"{args.out} is current")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(fresh, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
