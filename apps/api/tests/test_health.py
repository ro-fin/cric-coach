import cricai_api
from cricai_api.app import create_app
from fastapi.testclient import TestClient


def test_health_endpoint_reports_ok_and_version() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": cricai_api.__version__}
