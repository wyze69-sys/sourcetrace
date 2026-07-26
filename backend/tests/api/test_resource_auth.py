"""Integration tests for JWT Bearer authentication across all 9 protected resource routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_indexing_job_repository,
    get_repository_repository,
    get_session_repository,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import JWTSigner, SessionSigner
from sourcetrace.models.domain import (
    IndexingJobRecord,
    RepositoryRecord,
)

TEST_JWT_SECRET = "a_very_secret_jwt_key_that_is_at_least_32_bytes_long!"
TEST_SESSION_SECRET = "a_very_secret_session_key_that_is_32_bytes!"


def get_test_settings() -> Settings:
    return Settings(
        jwt_secret=SecretStr(TEST_JWT_SECRET),
        session_signing_secret=SecretStr(TEST_SESSION_SECRET),
    )


def create_valid_token(owner_session_id: str, ttl_seconds: int = 3600) -> str:
    signer = JWTSigner(secret=TEST_JWT_SECRET, settings=get_test_settings())
    return signer.create_access_token(owner_session_id, ttl_seconds=ttl_seconds)


MINIMAL_ZIP = b"PK\x05\x06" + b"\x00" * 18

# The 9 protected resource endpoints and representative request definitions
PROTECTED_ENDPOINTS = [
    (
        "POST",
        "/api/v1/repositories/upload",
        {"files": {"file": ("test.zip", MINIMAL_ZIP, "application/zip")}},
    ),
    ("GET", "/api/v1/repositories", {}),
    ("GET", "/api/v1/repositories/repo_123", {}),
    (
        "POST",
        "/api/v1/repositories",
        {"json": {"github_url": "https://github.com/octocat/Hello-World"}},
    ),
    ("GET", "/api/v1/indexing-jobs/job_123", {}),
    (
        "POST",
        "/api/v1/repositories/repo_123/search",
        {"json": {"query": "test", "limit": 5}},
    ),
    (
        "POST",
        "/api/v1/repositories/repo_123/conversations",
        {"json": {"question": "What does this code do?"}},
    ),
    ("GET", "/api/v1/repositories/repo_123/conversations/conv_123", {}),
    (
        "POST",
        "/api/v1/repositories/repo_123/conversations/conv_123/messages",
        {"json": {"question": "Follow up question"}},
    ),
]


def test_protected_routes_reject_unauthenticated_requests() -> None:
    mock_session_repo = MagicMock()
    mock_repo_repo = MagicMock()
    mock_job_repo = MagicMock()

    app = create_app()
    app.dependency_overrides[get_settings] = get_test_settings
    app.dependency_overrides[get_session_repository] = lambda: mock_session_repo
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: mock_job_repo

    client = TestClient(app)

    for method, path, kwargs in PROTECTED_ENDPOINTS:
        if method == "GET":
            response = client.get(path, **kwargs)
        elif method == "POST":
            response = client.post(path, **kwargs)
        else:
            raise ValueError(f"Unsupported method: {method}")

        err_msg = f"{method} {path} returned {response.status_code}, expected 401"
        assert response.status_code == 401, err_msg
        assert response.headers.get("www-authenticate") == "Bearer"
        assert response.json() == {
            "error": {
                "code": "UNAUTHORIZED",
                "message": "Authentication credentials are missing or invalid.",
                "request_id": None,
            }
        }, f"{method} {path} returned invalid error envelope"
        assert "set-cookie" not in response.headers

    mock_session_repo.get_by_id.assert_not_called()


def test_protected_routes_reject_legacy_cookie_without_bearer() -> None:
    mock_session_repo = MagicMock()
    mock_repo_repo = MagicMock()

    app = create_app()
    app.dependency_overrides[get_settings] = get_test_settings
    app.dependency_overrides[get_session_repository] = lambda: mock_session_repo
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo

    client = TestClient(app)

    session_signer = SessionSigner(TEST_SESSION_SECRET)
    exp = datetime.now(UTC) + timedelta(days=7)
    legacy_cookie = session_signer.create_cookie_token("sess_cookie_user", exp)
    client.cookies.set("sourcetrace_session", legacy_cookie)

    for method, path, kwargs in PROTECTED_ENDPOINTS:
        if method == "GET":
            response = client.get(path, **kwargs)
        elif method == "POST":
            response = client.post(path, **kwargs)
        else:
            raise ValueError(f"Unsupported method: {method}")

        assert response.status_code == 401
        assert response.headers.get("www-authenticate") == "Bearer"
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    mock_session_repo.get_by_id.assert_not_called()


def test_valid_bearer_token_grants_access() -> None:
    owner_id = "sess_bearer_owner"
    token = create_valid_token(owner_id)

    mock_repo_repo = MagicMock()
    mock_repo_repo.list_by_owner.return_value = []

    app = create_app()
    app.dependency_overrides[get_settings] = get_test_settings
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/api/v1/repositories", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"repositories": []}

    mock_repo_repo.list_by_owner.assert_called_once_with(owner_id)


def test_bearer_token_owner_scoping_prevents_cross_owner_access() -> None:
    owner1 = "sess_owner_1"
    owner2 = "sess_owner_2"
    now = datetime.now(UTC)

    owner2_repo = RepositoryRecord(
        repository_id="repo_owner2",
        owner_session_id=owner2,
        name="owner2-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
    )

    def _mock_get_by_id(owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        if owner_session_id == owner2 and repository_id == "repo_owner2":
            return owner2_repo
        return None

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.side_effect = _mock_get_by_id
    mock_job_repo = MagicMock()
    mock_job_repo.get_by_repository.return_value = None

    app = create_app()
    app.dependency_overrides[get_settings] = get_test_settings
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: mock_job_repo

    client = TestClient(app)

    token_owner1 = create_valid_token(owner1)
    res_owner1 = client.get(
        "/api/v1/repositories/repo_owner2",
        headers={"Authorization": f"Bearer {token_owner1}"},
    )
    assert res_owner1.status_code == 404

    token_owner2 = create_valid_token(owner2)
    res_owner2 = client.get(
        "/api/v1/repositories/repo_owner2",
        headers={"Authorization": f"Bearer {token_owner2}"},
    )
    assert res_owner2.status_code == 200
    assert res_owner2.json()["repository_id"] == "repo_owner2"


def test_bearer_token_cannot_be_overridden_by_request_body_or_query_params() -> None:
    token_owner = "sess_legitimate_owner"
    token = create_valid_token(token_owner)

    def _mock_get_job(owner_session_id: str, job_id: str) -> IndexingJobRecord | None:
        if owner_session_id == token_owner:
            return IndexingJobRecord(
                job_id=job_id,
                repository_id="repo_1",
                owner_session_id=owner_session_id,
                status="ready",
                progress_percentage=100,
                current_step="Completed",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        return None

    mock_job_repo = MagicMock()
    mock_job_repo.get_by_id.side_effect = _mock_get_job

    app = create_app()
    app.dependency_overrides[get_settings] = get_test_settings
    app.dependency_overrides[get_indexing_job_repository] = lambda: mock_job_repo

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.get(
        "/api/v1/indexing-jobs/job_123?owner_session_id=sess_attacker",
        headers=headers,
    )
    assert res.status_code == 200
    mock_job_repo.get_by_id.assert_called_once_with(token_owner, "job_123")
