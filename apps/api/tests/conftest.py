from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricai_testing.apptest import make_test_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path))
