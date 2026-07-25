from datetime import datetime

from fastapi.testclient import TestClient

from sourcetrace import __version__
from sourcetrace.main import app


def test_health_endpoint_reports_v1_status() -> None:
    client = TestClient(app)

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == __version__
    assert datetime.fromisoformat(data["timestamp"]) is not None


def test_old_unprefixed_health_route_returns_404_envelope() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "The requested resource was not found.",
            "request_id": None,
        }
    }


def test_unknown_api_route_returns_standard_404_envelope() -> None:
    client = TestClient(app)

    response = client.get("/api/v1/nonexistent-path")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "The requested resource was not found.",
            "request_id": None,
        }
    }
