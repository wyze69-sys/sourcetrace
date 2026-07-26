"""Offline integration tests for FastAPI session dependency injection."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from unittest.mock import MagicMock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.dependencies import (
    get_current_session,
    get_session_repository,
    get_session_signer,
)
from sourcetrace.api.routes.health import router as health_router
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import SessionSigner
from sourcetrace.models.domain import AnonymousSession

TEST_SECRET = "a_very_secret_key_that_is_at_least_32_bytes_long!"


def create_test_app(settings: Settings, mock_repo: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(health_router, prefix="/api/v1")

    @app.get("/api/v1/protected")
    def protected_route(
        session: Annotated[AnonymousSession, Depends(get_current_session)],
    ) -> dict[str, str]:
        return {"owner_session_id": session.owner_session_id}

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: mock_repo
    app.dependency_overrides[get_session_signer] = lambda: SessionSigner(
        secret=TEST_SECRET
    )

    return app


def test_health_response_sets_no_cookie_and_performs_no_session_repository_work() -> (
    None
):
    mock_repo = MagicMock()
    settings = Settings(session_signing_secret=SecretStr(TEST_SECRET))
    app = create_test_app(settings, mock_repo)
    client = TestClient(app)

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    mock_repo.get_by_id.assert_not_called()
    mock_repo.save.assert_not_called()


def test_no_cookie_protected_request_creates_one_persisted_session_and_set_cookie() -> (
    None
):
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None
    settings = Settings(
        env="development", session_signing_secret=SecretStr(TEST_SECRET)
    )
    app = create_test_app(settings, mock_repo)
    client = TestClient(app)

    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    cookie_headers = response.headers.get_list("set-cookie")
    assert any("sourcetrace_session=" in h for h in cookie_headers)
    assert any("httponly" in h.lower() for h in cookie_headers)
    assert any("samesite=strict" in h.lower() for h in cookie_headers)
    assert any("max-age=604800" in h.lower() for h in cookie_headers)
    assert any("path=/api/v1/auth/session" in h.lower() for h in cookie_headers)

    mock_repo.save.assert_called_once()
    saved_session = mock_repo.save.call_args[0][0]
    assert saved_session.owner_session_id.startswith("sess_")


def test_development_cookie_has_secure_false() -> None:
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None
    settings = Settings(
        env="development", session_signing_secret=SecretStr(TEST_SECRET)
    )
    app = create_test_app(settings, mock_repo)
    client = TestClient(app)

    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    cookie_header = response.headers["set-cookie"]
    assert "secure" not in cookie_header.lower()


def test_production_cookie_has_secure_true() -> None:
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None
    settings = Settings(
        env="production", session_signing_secret=SecretStr(TEST_SECRET)
    )
    app = create_test_app(settings, mock_repo)
    client = TestClient(app)

    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    cookie_header = response.headers["set-cookie"]
    assert "secure" in cookie_header.lower()


def test_valid_existing_cookie_does_not_create_a_second_session() -> None:
    signer = SessionSigner(secret=TEST_SECRET)
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    valid_token = signer.create_cookie_token("sess_existing123", exp)

    stored_session = AnonymousSession(
        owner_session_id="sess_existing123",
        created_at=now,
        updated_at=now,
        last_active_at=now,
        expires_at=exp,
    )

    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = stored_session

    settings = Settings(session_signing_secret=SecretStr(TEST_SECRET))
    app = create_test_app(settings, mock_repo)
    client = TestClient(app)

    client.cookies.set("sourcetrace_session", valid_token)
    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    assert response.json()["owner_session_id"] == "sess_existing123"
    mock_repo.get_by_id.assert_called_once_with("sess_existing123")
    mock_repo.save.assert_not_called()


def test_invalid_expired_missing_stored_session_creates_fresh_session() -> None:
    signer = SessionSigner(secret=TEST_SECRET)
    now = datetime.now(UTC)

    # 1. Tampered cookie
    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = None
    settings = Settings(session_signing_secret=SecretStr(TEST_SECRET))
    app = create_test_app(settings, mock_repo)
    client = TestClient(app)

    client.cookies.set("sourcetrace_session", "v1.sess_fake.1700000000.bad_sig")
    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    mock_repo.save.assert_called_once()
    new_id_1 = response.json()["owner_session_id"]
    assert new_id_1 != "sess_fake"

    # 2. Expired cookie
    past = now - timedelta(days=1)
    expired_token = signer.create_cookie_token("sess_expired", past)
    mock_repo.reset_mock()

    client.cookies.set("sourcetrace_session", expired_token)
    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    mock_repo.save.assert_called_once()
    new_id_2 = response.json()["owner_session_id"]
    assert new_id_2 != "sess_expired"

    # 3. Missing from repository
    valid_token = signer.create_cookie_token("sess_deleted", now + timedelta(days=7))
    mock_repo.reset_mock()
    mock_repo.get_by_id.return_value = None

    client.cookies.set("sourcetrace_session", valid_token)
    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    mock_repo.save.assert_called_once()
    new_id_3 = response.json()["owner_session_id"]
    assert new_id_3 != "sess_deleted"


def test_mismatched_stored_session_owner_creates_fresh_session() -> None:
    signer = SessionSigner(secret=TEST_SECRET)
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    valid_token = signer.create_cookie_token("sess_claimed123", exp)

    mismatched_session = AnonymousSession(
        owner_session_id="sess_different_owner_456",
        created_at=now,
        updated_at=now,
        last_active_at=now,
        expires_at=exp,
    )

    mock_repo = MagicMock()
    mock_repo.get_by_id.return_value = mismatched_session

    settings = Settings(session_signing_secret=SecretStr(TEST_SECRET))
    app = create_test_app(settings, mock_repo)
    client = TestClient(app)

    client.cookies.set("sourcetrace_session", valid_token)
    response = client.get("/api/v1/protected")

    assert response.status_code == 200
    returned_id = response.json()["owner_session_id"]

    assert returned_id != "sess_different_owner_456"
    assert returned_id != "sess_claimed123"
    assert returned_id.startswith("sess_")

    mock_repo.save.assert_called_once()
    saved_session = mock_repo.save.call_args[0][0]
    assert saved_session.owner_session_id == returned_id
