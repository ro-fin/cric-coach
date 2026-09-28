"""Real-PostgreSQL test infrastructure without Docker.

Preference order:
1. ``CRICAI_TEST_DATABASE_URL`` (CI service container).
2. A throwaway local cluster via ``initdb``/``pg_ctl`` (developer machines).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PG_BIN_CANDIDATES = (
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/lib/postgresql/16/bin",
    "/usr/local/opt/postgresql@16/bin",
)


def find_pg_bin() -> Path | None:
    for candidate in PG_BIN_CANDIDATES:
        if (Path(candidate) / "initdb").exists():
            return Path(candidate)
    initdb = shutil.which("initdb")
    return Path(initdb).parent if initdb else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class TempPostgres:
    url: str
    data_dir: Path | None = None
    pg_bin: Path | None = None
    port: int | None = None

    def stop(self) -> None:
        if self.pg_bin is not None and self.data_dir is not None:
            subprocess.run(
                [str(self.pg_bin / "pg_ctl"), "-D", str(self.data_dir), "stop", "-m", "immediate"],
                check=False,
                capture_output=True,
            )
            shutil.rmtree(self.data_dir, ignore_errors=True)


def start_temp_postgres() -> TempPostgres:
    env_url = os.environ.get("CRICAI_TEST_DATABASE_URL")
    if env_url:
        return TempPostgres(url=env_url)

    pg_bin = find_pg_bin()
    if pg_bin is None:
        raise RuntimeError("no PostgreSQL binaries found and CRICAI_TEST_DATABASE_URL is not set")

    data_dir = Path(tempfile.mkdtemp(prefix="cricai-pg-"))
    port = _free_port()
    subprocess.run(
        [str(pg_bin / "initdb"), "-D", str(data_dir), "-U", "cricai", "-A", "trust", "--no-sync"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(pg_bin / "pg_ctl"),
            "-D",
            str(data_dir),
            "-o",
            f"-p {port} -F -c listen_addresses=127.0.0.1",
            "-w",
            "start",
            "-l",
            str(data_dir / "pg.log"),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(pg_bin / "createdb"),
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "cricai",
            "cricai_test",
        ],
        check=True,
        capture_output=True,
    )
    return TempPostgres(
        url=f"postgresql+psycopg://cricai@127.0.0.1:{port}/cricai_test",
        data_dir=data_dir,
        pg_bin=pg_bin,
        port=port,
    )


@contextmanager
def temp_postgres() -> Iterator[TempPostgres]:
    pg = start_temp_postgres()
    try:
        yield pg
    finally:
        pg.stop()
