"""Compose-config lint (Phase-7 plan, design milestone 6; US-L3 LAN deploy).

``deploy/docker-compose.yaml`` is AUTHORED but cannot be executed in CI or on
the lab box (local Docker broken — design milestone 6), so this pure-Python
lint keeps it from rotting: the file must YAML-parse, declare exactly the five
required services with healthchecks, pin postgres:16/redis:7, wire the
CRICAI_*/NEXT_PUBLIC_* environment the API/worker/web contracts require, and
persist storage + pg data in named volumes. Every host-side ``${VAR}``
interpolation must be documented in ``deploy/.env.example``.

Phase-7 fx1 (findings 3/22/29): the app services build small local
provisioning images (``deploy/Dockerfile.{api,worker,web}``) so system
binaries (ffmpeg, pnpm) are baked at BUILD time — container recreation must
never hit the internet, and the api container (which runs upload probing and
the pipeline DAG in-process) needs ffmpeg exactly like the worker.

No docker invocation anywhere — PyYAML only (shipped via uvicorn[standard],
imported dynamically because the workspace carries no yaml type stubs).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest

yaml = importlib.import_module("yaml")

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO_ROOT / "deploy" / "docker-compose.yaml"
ENV_EXAMPLE_PATH = REPO_ROOT / "deploy" / ".env.example"

REQUIRED_SERVICES = frozenset({"api", "worker", "web", "postgres", "redis"})

#: App services build their provisioning image from a small Dockerfile in
#: deploy/ (context "." = deploy/, the compose project directory).
BUILT_SERVICES = ("api", "worker", "web")

#: Host-side compose interpolations: ``${VAR}``, ``${VAR:-default}``,
#: ``${VAR:?message}`` — but never the ``$$``-escaped container-side form.
_INTERPOLATION_RE = re.compile(r"(?<!\$)\$\{([A-Z_][A-Z0-9_]*)")


def _has_from_line(dockerfile: str, image_prefix: str) -> bool:
    """A ``FROM <image_prefix>...`` build stage exists (a documentation comment
    header before FROM is valid Dockerfile syntax, so don't require line 1)."""
    return any(line.startswith(f"FROM {image_prefix}") for line in dockerfile.splitlines())


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    parsed = yaml.safe_load(COMPOSE_PATH.read_text())
    assert isinstance(parsed, dict), "compose file must be a YAML mapping"
    return parsed


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    assert isinstance(compose.get("services"), dict)
    return dict(compose["services"])


def _env_keys(service: dict[str, Any]) -> set[str]:
    environment = service.get("environment", {})
    assert isinstance(environment, dict), "environment must use mapping form"
    return set(environment)


def test_compose_declares_exactly_the_required_services(services: dict[str, Any]) -> None:
    assert set(services) == set(REQUIRED_SERVICES)


def test_every_service_has_a_healthcheck_and_restart_policy(services: dict[str, Any]) -> None:
    for name, service in services.items():
        healthcheck = service.get("healthcheck")
        assert isinstance(healthcheck, dict), f"{name}: healthcheck missing"
        assert healthcheck.get("test"), f"{name}: healthcheck.test missing"
        assert service.get("restart") == "unless-stopped", f"{name}: restart policy missing"


def test_datastore_images_are_version_pinned(services: dict[str, Any]) -> None:
    assert str(services["postgres"]["image"]).startswith("postgres:16")
    assert str(services["redis"]["image"]).startswith("redis:7")


def test_api_service_env_and_wiring(services: dict[str, Any]) -> None:
    api = services["api"]
    assert _env_keys(api) >= {
        "CRICAI_DATABASE_URL",
        "CRICAI_STORAGE_ROOT",
        "CRICAI_PARENT_TOKEN",
        "CRICAI_COACH_TOKEN",
        "CRICAI_PLAYER_TOKEN",
        # The api runs the pipeline DAG in-process (US-E1..E4 pose stage):
        # without the passthrough, setting the var in deploy/.env can never
        # reach the container (environment blocks are explicit allowlists).
        "CRICAI_POSE_MODEL_ASSET",
    }
    # The DB URL must point INSIDE the compose network, not at localhost.
    assert "@postgres:5432/" in api["environment"]["CRICAI_DATABASE_URL"]
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert any(str(port).endswith(":8000") for port in api["ports"])
    command = " ".join(api["command"])
    assert "alembic" in command and "upgrade head" in command, "api must migrate before serving"
    assert "uvicorn cricai_api.app:create_app --factory" in command


def test_worker_service_env_and_wiring(services: dict[str, Any]) -> None:
    worker = services["worker"]
    assert _env_keys(worker) >= {
        "CRICAI_DATABASE_URL",
        "CRICAI_REDIS_URL",
        "CRICAI_STORAGE_ROOT",
        "CRICAI_POSE_MODEL_ASSET",
    }
    assert worker["environment"]["CRICAI_REDIS_URL"].startswith("redis://redis:")
    for dependency in ("postgres", "redis"):
        assert worker["depends_on"][dependency]["condition"] == "service_healthy"
    command = " ".join(worker["command"])
    assert "rq worker" in command


def test_app_services_build_local_provisioning_images(services: dict[str, Any]) -> None:
    """Findings 3/22/29: system provisioning is baked at image BUILD time so
    recreating a container on an offline LAN box never crash-loops on apt."""
    for name in BUILT_SERVICES:
        build = services[name].get("build")
        assert isinstance(build, dict), f"{name}: must build its provisioning image"
        assert build.get("context") == ".", f"{name}: build context must be deploy/"
        assert build.get("dockerfile") == f"Dockerfile.{name}"
        assert (REPO_ROOT / "deploy" / f"Dockerfile.{name}").is_file()


def test_api_and_worker_images_bake_ffmpeg(services: dict[str, Any]) -> None:
    """Finding 3: the api executes upload probing (ffprobe) and the pipeline
    DAG's clip stage (ffmpeg) IN-PROCESS — it needs ffmpeg exactly like the
    worker. Finding 22: neither may apt-get at container start."""
    for name in ("api", "worker"):
        dockerfile = (REPO_ROOT / "deploy" / f"Dockerfile.{name}").read_text()
        assert _has_from_line(dockerfile, "ghcr.io/astral-sh/uv:python3.12"), (
            f"Dockerfile.{name}: base image must stay the pinned uv image"
        )
        assert "ffmpeg" in dockerfile, f"Dockerfile.{name}: must bake ffmpeg at build"
        command = " ".join(services[name]["command"])
        assert "apt-get" not in command, (
            f"{name}: apt at container start breaks offline recreation (finding 22)"
        )


def test_web_image_bakes_pnpm(services: dict[str, Any]) -> None:
    """Finding 29: pnpm is provisioned (version-pinned) at build, not fetched
    by corepack on every container start."""
    dockerfile = (REPO_ROOT / "deploy" / "Dockerfile.web").read_text()
    assert _has_from_line(dockerfile, "node:22"), "Dockerfile.web: pinned node base image"
    assert "corepack" in dockerfile and "pnpm@" in dockerfile
    command = " ".join(services["web"]["command"])
    assert "corepack" not in command, "pnpm must already be provisioned in the image"


def test_web_service_env_and_wiring(services: dict[str, Any]) -> None:
    web = services["web"]
    assert _env_keys(web) >= {
        "NEXT_PUBLIC_API_BASE_URL",
        "NEXT_PUBLIC_API_TOKEN",
        "NEXT_PUBLIC_CRICAI_MEDIA_BASE",
    }
    assert web["depends_on"]["api"]["condition"] == "service_healthy"
    assert any(str(port).endswith(":3000") for port in web["ports"])
    command = " ".join(web["command"])
    # NEXT_PUBLIC_* are inlined at build: the container must build at start.
    assert "build" in command and "start" in command


def test_storage_and_pgdata_persist_in_named_volumes(
    compose: dict[str, Any], services: dict[str, Any]
) -> None:
    named = set(compose.get("volumes", {}))
    assert {"cricai-pgdata", "cricai-storage"} <= named
    assert "cricai-pgdata:/var/lib/postgresql/data" in services["postgres"]["volumes"]
    for name in ("api", "worker"):
        mounts = services[name]["volumes"]
        assert "cricai-storage:/data/storage" in mounts, f"{name}: shared storage mount missing"
        assert services[name]["environment"]["CRICAI_STORAGE_ROOT"] == "/data/storage"
    # Container venvs must live OUTSIDE the repo bind-mount (host .venv safety).
    assert "cricai-api-venv:/opt/venv" in services["api"]["volumes"]
    assert "cricai-worker-venv:/opt/venv" in services["worker"]["volumes"]


def test_every_interpolated_variable_is_documented_in_env_example() -> None:
    referenced = set(_INTERPOLATION_RE.findall(COMPOSE_PATH.read_text()))
    assert referenced, "compose file should be driven by deploy/.env"
    documented = {
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE_PATH.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    missing = sorted(referenced - documented)
    assert not missing, f"undocumented in deploy/.env.example: {missing}"
