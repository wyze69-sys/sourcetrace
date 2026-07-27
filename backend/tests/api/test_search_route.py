"""Offline API route unit tests for POST /api/v1/repositories/{id}/search."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_code_chunk_repository,
    get_current_owner_id,
    get_repository_repository,
)
from sourcetrace.core.exceptions import StorageOperationError
from sourcetrace.models.domain import (
    AnonymousSession,
    CodeChunk,
    RepositoryRecord,
    RetrievalResult,
)


def _make_session() -> AnonymousSession:
    now = datetime.now(UTC)
    return AnonymousSession(
        owner_session_id="owner_search_test",
        last_active_at=now,
        expires_at=now,
        created_at=now,
        updated_at=now,
    )


def test_evidence_search_successful_static_repository():
    """Verify POST /search succeeds for a ready static repository with dependency overrides."""
    now = datetime.now(UTC)
    session = _make_session()

    repo_record = RepositoryRecord(
        repository_id="repo_static_123",
        owner_session_id=session.owner_session_id,
        name="static-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        index_mode="static",
    )

    chunk = CodeChunk(
        chunk_id="chunk_1",
        repository_id="repo_static_123",
        owner_session_id=session.owner_session_id,
        relative_path="src/main.py",
        language="python",
        symbol_name="main",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def main(): pass",
        content_hash="hash1",
        parser_version="1.0",
        created_at=now,
    )

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.search_lexical.return_value = [RetrievalResult(chunk=chunk, score=1.0)]

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo

    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_static_123/search",
        json={"query": "main", "limit": 5},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["repository_id"] == "repo_static_123"
    assert data["total"] == 1
    assert data["items"][0]["symbol_name"] == "main"
    assert data["items"][0]["relative_path"] == "src/main.py"


def test_evidence_search_missing_repository_returns_404():
    """Verify POST /search returns 404 Not Found when repository does not exist."""
    session = _make_session()

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = None

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo

    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_missing/search",
        json={"query": "main"},
    )

    assert response.status_code == 404


def test_evidence_search_not_ready_repository_returns_400():
    """Verify POST /search returns 400 Bad Request when repository is not ready."""
    now = datetime.now(UTC)
    session = _make_session()

    repo_record = RepositoryRecord(
        repository_id="repo_pending",
        owner_session_id=session.owner_session_id,
        name="pending-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        index_mode="static",
    )

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo

    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_pending/search",
        json={"query": "main"},
    )

    assert response.status_code == 400


def test_evidence_search_cloud_repository_rejected():
    """Verify POST /search returns 400 Bad Request when repository is in cloud_ai mode."""
    now = datetime.now(UTC)
    session = _make_session()

    repo_record = RepositoryRecord(
        repository_id="repo_cloud",
        owner_session_id=session.owner_session_id,
        name="cloud-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        index_mode="cloud_ai",
    )

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo

    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_cloud/search",
        json={"query": "main"},
    )

    assert response.status_code == 400
    assert "Evidence search endpoint requires a static repository" in response.text


def test_evidence_search_storage_failure_returns_500():
    """Verify POST /search returns 500 when storage layer raises an operation error."""
    now = datetime.now(UTC)
    session = _make_session()

    repo_record = RepositoryRecord(
        repository_id="repo_static_err",
        owner_session_id=session.owner_session_id,
        name="static-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        index_mode="static",
    )

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.search_lexical.side_effect = StorageOperationError("DB error")

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo

    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_static_err/search",
        json={"query": "main"},
    )

    assert response.status_code == 500
