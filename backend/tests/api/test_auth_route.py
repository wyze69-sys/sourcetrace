"""Offline integration tests for FastAPI JWT provisioning endpoint POST /api/v1/auth/session."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_session_repository,
    get_session_signer,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import JWTSigner, SessionSigner
from sourcetrace.models.domain import AnonymousSession

TEST_JWT_SECRET = "a_very_secret_jwt_key_that_is_at_least_32_bytes_long!"
TEST_SESSION_SECRET = "a_very_secret_session_key_that_is_32_bytes!"


def test_new_browser_provisioning_returns_200_and_sets_cookie_and_returns_jwt() -> None:
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: mock_repo
    client = TestClient(app)

    response = client.post("/api/v1/auth/session")

    assert response.status_code == 200
    data = response.json()

    assert "access_token" in data
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 604800
    assert "owner_session_id" not in data
    assert "jti" not in data

    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("pragma") == "no-cache"

    assert "set-cookie" in response.headers
    assert "sourcetrace_session=" in response.headers["set-cookie"]

    signer = JWTSigner(secret=TEST_JWT_SECRET, settings=settings)
    verified_owner_id = signer.verify_access_token(data["access_token"])

    mock_repo.save.assert_called_once()
    saved_session = mock_repo.save.call_args[0][0]
    assert verified_owner_id == saved_session.owner_session_id


def test_existing_browser_with_valid_cookie_reuses_session_and_creates_jwt() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    existing_owner_id = "sess_existing_browser_123"

    session_signer = SessionSigner(secret=TEST_SESSION_SECRET)
    cookie_token = session_signer.create_cookie_token(existing_owner_id, exp)

    stored_session = AnonymousSession(
        owner_session_id=existing_owner_id,
        created_at=now,
        updated_at=now,
        last_active_at=now,
        expires_at=exp,
    )

    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = stored_session

    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: mock_repo
    app.dependency_overrides[get_session_signer] = lambda: session_signer
    client = TestClient(app)

    client.cookies.set("sourcetrace_session", cookie_token)
    response = client.post("/api/v1/auth/session")

    assert response.status_code == 200
    data = response.json()

    jwt_signer = JWTSigner(secret=TEST_JWT_SECRET, settings=settings)
    verified_owner_id = jwt_signer.verify_access_token(data["access_token"])

    assert verified_owner_id == existing_owner_id
    mock_repo.get_by_id.assert_called_once_with(existing_owner_id)
    mock_repo.save.assert_not_called()


def test_invalid_legacy_cookie_provisions_fresh_session() -> None:
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None

    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: mock_repo
    client = TestClient(app)

    client.cookies.set("sourcetrace_session", "v1.sess_invalid.1700000000.bad_sig")
    response = client.post("/api/v1/auth/session")

    assert response.status_code == 200
    data = response.json()

    jwt_signer = JWTSigner(secret=TEST_JWT_SECRET, settings=settings)
    verified_owner_id = jwt_signer.verify_access_token(data["access_token"])

    assert verified_owner_id != "sess_invalid"
    assert verified_owner_id.startswith("sess_")
    mock_repo.save.assert_called_once()


def test_no_jwt_secret_returns_500_error_envelope() -> None:
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None

    no_jwt_settings = Settings(
        jwt_secret=None,
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: no_jwt_settings
    app.dependency_overrides[get_session_repository] = lambda: mock_repo
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/v1/auth/session")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }


def test_bootstrap_cookie_attributes_and_narrow_path() -> None:
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None
    settings = Settings(
        env="production",
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: mock_repo
    client = TestClient(app)

    response = client.post("/api/v1/auth/session")

    assert response.status_code == 200
    headers = response.headers.get_list("set-cookie")
    assert any("path=/api/v1/auth/session" in h.lower() for h in headers)
    assert any("httponly" in h.lower() for h in headers)
    assert any("samesite=strict" in h.lower() for h in headers)
    assert any("secure" in h.lower() for h in headers)


def test_legacy_root_cookie_migration_expires_root_path_and_sets_narrow_path() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    existing_owner_id = "sess_legacy_root_owner"

    session_signer = SessionSigner(secret=TEST_SESSION_SECRET)
    cookie_token = session_signer.create_cookie_token(existing_owner_id, exp)

    stored_session = AnonymousSession(
        owner_session_id=existing_owner_id,
        created_at=now,
        updated_at=now,
        last_active_at=now,
        expires_at=exp,
    )

    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = stored_session

    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: mock_repo
    app.dependency_overrides[get_session_signer] = lambda: session_signer
    client = TestClient(app)

    client.cookies.set("sourcetrace_session", cookie_token)
    response = client.post("/api/v1/auth/session")

    assert response.status_code == 200
    set_cookie_headers = response.headers.get_list("set-cookie")

    # Assert root cookie deletion header exists
    has_root_delete = any(
        "path=/" in h.lower() and ("max-age=0" in h.lower() or "expires=" in h.lower())
        for h in set_cookie_headers
    )
    assert has_root_delete

    # Assert replacement narrow cookie exists
    has_narrow_set = any(
        "path=/api/v1/auth/session" in h.lower() and "samesite=strict" in h.lower()
        for h in set_cookie_headers
    )
    assert has_narrow_set


def test_render_yaml_configuration_has_separate_generated_jwt_secret() -> None:
    render_yaml_path = Path(__file__).resolve().parents[3] / "render.yaml"
    assert render_yaml_path.exists()

    with open(render_yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    services = data.get("services", [])
    assert len(services) > 0
    backend_service = next((s for s in services if s.get("name") == "sourcetrace-backend"), None)
    assert backend_service is not None

    env_vars = {item["key"]: item for item in backend_service.get("envVars", [])}
    assert "SOURCETRACE_SESSION_SIGNING_SECRET" in env_vars
    assert env_vars["SOURCETRACE_SESSION_SIGNING_SECRET"].get("generateValue") is True

    assert "SOURCETRACE_JWT_SECRET" in env_vars
    assert env_vars["SOURCETRACE_JWT_SECRET"].get("generateValue") is True

    assert env_vars["SOURCETRACE_SESSION_SIGNING_SECRET"] != env_vars["SOURCETRACE_JWT_SECRET"]
