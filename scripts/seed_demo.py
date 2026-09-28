#!/usr/bin/env python3
"""Seed the synthetic demo session (US-L2). Usage: uv run scripts/seed_demo.py [target_dir]"""

import sys
from pathlib import Path

from cricai_data.demo import write_demo_session


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("datasets/demo")
    path = write_demo_session(target)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
