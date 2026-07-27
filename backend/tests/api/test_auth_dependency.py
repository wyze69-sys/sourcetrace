"""Offline integration tests for FastAPI JWT Bearer authentication dependency."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.dependencies import (
    CurrentOwnerId,
    get_jwt_signer,
    get_session_repository,
)
from sourcetrace.api.errors import register_error_handlers
from sourcetrace.api.routes.health import router as health_router
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import SessionConfigurationError
from sourcetrace.core.security import JWTSigner

TEST_JWT_SECRET = "a_very_secret_jwt_key_that_is_at_least_32_bytes_long!"
TEST_SESSION_SECRET = "a_very_secret_session_key_that_is_32_bytes!"


def create_test_bearer_app(
    settings: Settings, mock_session_repo: MagicMock | None = None
) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(health_router, prefix="/api/v1")

    @app.get("/api/v1/bearer-protected")
    def bearer_protected_route(
        owner_id: CurrentOwnerId,
    ) -> dict[str, str]:
        return {"owner_session_id": owner_id}

    app.dependency_overrides[get_settings] = lambda: settings
    if mock_session_repo is not None:
        app.dependency_overrides[get_session_repository] = lambda: mock_session_repo

    return app


def test_valid_bearer_jwt_returns_exact_owner_session_id_and_performs_no_db_work() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    mock_session_repo = MagicMock()
    app = create_test_bearer_app(settings, mock_session_repo)
    client = TestClient(app)

    signer = JWTSigner(secret=TEST_JWT_SECRET, settings=settings)
    expected_owner_id = "sess_valid_user_123"
    token = signer.create_access_token(expected_owner_id)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"owner_session_id": expected_owner_id}
    mock_session_repo.get_by_id.assert_not_called()
    mock_session_repo.save.assert_not_called()


def test_missing_authorization_header_returns_401() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get("/api/v1/bearer-protected")

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_basic_auth_scheme_returns_401() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_empty_or_malformed_bearer_credentials_return_401() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    app = create_test_bearer_app(settings)
    client = TestClient(app)

    for bad_auth in ["Bearer ", "Bearer not.a.valid.jwt", "Bearer invalid"]:
        response = client.get(
            "/api/v1/bearer-protected",
            headers={"Authorization": bad_auth},
        )
        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"
        assert response.json() == {
            "error": {
                "code": "UNAUTHORIZED",
                "message": "Authentication credentials are missing or invalid.",
                "request_id": None,
            }
        }


def test_tampered_jwt_returns_401() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    signer = JWTSigner(secret=TEST_JWT_SECRET, settings=settings)
    token = signer.create_access_token("sess_tampered_123")
    tampered_token = token[:-4] + "ffff"

    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_expired_jwt_returns_401() -> None:
    import time

    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    signer = JWTSigner(secret=TEST_JWT_SECRET, settings=settings)
    expired_token = signer.create_access_token("sess_expired_123", ttl_seconds=1)

    time.sleep(1.1)

    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": f"Bearer {expired_token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_jwt_signed_with_another_secret_returns_401() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    other_secret = "another_secret_key_that_is_at_least_32_bytes_long!"
    other_signer = JWTSigner(secret=other_secret, settings=settings)
    token = other_signer.create_access_token("sess_wrong_secret")

    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_wrong_issuer_returns_401() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        jwt_issuer="sourcetrace-api-prod",
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    wrong_issuer_settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        jwt_issuer="wrong-issuer",
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    token = JWTSigner(secret=TEST_JWT_SECRET, settings=wrong_issuer_settings).create_access_token(
        "sess_wrong_iss"
    )

    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_wrong_audience_returns_401() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        jwt_audience="sourcetrace-clients",
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    wrong_aud_settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        jwt_audience="wrong-audience",
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    token = JWTSigner(secret=TEST_JWT_SECRET, settings=wrong_aud_settings).create_access_token(
        "sess_wrong_aud"
    )

    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_wrong_token_type_returns_401() -> None:
    import jwt

    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )

    payload = {
        "sub": "sess_wrong_type",
        "iat": 1700000000,
        "exp": 2000000000,
        "jti": "jti_123",
        "type": "refresh_token",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    raw_jwt = jwt.encode(payload, TEST_JWT_SECRET.encode("utf-8"), algorithm="HS256")

    app = create_test_bearer_app(settings)
    client = TestClient(app)

    response = client.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": f"Bearer {raw_jwt}"},
    )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication credentials are missing or invalid.",
            "request_id": None,
        }
    }


def test_get_jwt_signer_uses_jwt_secret_and_does_not_fallback_to_session_signing_secret() -> None:
    settings = Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    signer = get_jwt_signer(settings)
    assert isinstance(signer, JWTSigner)

    no_jwt_settings = Settings(
        jwt_secret=None,
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    with pytest.raises(SessionConfigurationError):
        get_jwt_signer(no_jwt_settings)


def test_missing_jwt_secret_does_not_break_app_creation_or_health_route() -> None:
    no_jwt_settings = Settings(
        jwt_secret=None,
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )
    app = create_test_bearer_app(no_jwt_settings)
    client = TestClient(app)

    response = client.get("/api/v1/health")
    assert response.status_code == 200

    client_no_raise = TestClient(app, raise_server_exceptions=False)
    bearer_resp_no_raise = client_no_raise.get(
        "/api/v1/bearer-protected",
        headers={"Authorization": "Bearer fake"},
    )
    assert bearer_resp_no_raise.status_code == 500
    assert bearer_resp_no_raise.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }
