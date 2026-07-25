"""Tests for API CORS middleware configuration."""

import pytest
from fastapi.testclient import TestClient

from sourcetrace.api.app import create_app


def test_cors_preflight_request_allowed_origin(monkeypatch):
    monkeypatch.setenv("SOURCETRACE_CORS_ORIGINS", "https://sourcetrace-frontend.vercel.app,http://localhost:5173")
    app = create_app()
    client = TestClient(app)

    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://sourcetrace-frontend.vercel.app",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://sourcetrace-frontend.vercel.app"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_preflight_request_vercel_regex_match(monkeypatch):
    monkeypatch.setenv("SOURCETRACE_CORS_ORIGINS", "")
    app = create_app()
    client = TestClient(app)

    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://sourcetrace-preview-xyz.vercel.app",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://sourcetrace-preview-xyz.vercel.app"
    assert response.headers.get("access-control-allow-credentials") == "true"
