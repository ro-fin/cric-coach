from pathlib import Path

import pytest
from cricai_api.app import ROUTER_MODULES, create_app
from cricai_api.deps import get_store
from starlette.requests import Request

from cricai_testing.apptest import make_test_app


def test_create_app_with_env_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # default storage root lands in tmp
    app = create_app()
    assert app.state.settings.parent_token == ""
    assert (tmp_path / "storage" / "objects").is_dir()


def test_all_router_modules_are_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app()
    paths = app.openapi()["paths"]
    assert "/health" in paths
    assert "/sessions" in paths
    assert "/players" in paths
    assert len(ROUTER_MODULES) == 33


def test_get_store_returns_app_store(tmp_path: Path) -> None:
    app = make_test_app(tmp_path)
    request = Request({"type": "http", "app": app})
    assert get_store(request) is app.state.store
