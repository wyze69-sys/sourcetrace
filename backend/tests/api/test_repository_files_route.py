"""Offline API route unit tests for GET /api/v1/repositories/{id}/files."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_code_chunk_repository,
    get_current_owner_id,
    get_repository_repository,
)
from sourcetrace.models.domain import (
    AnonymousSession,
    CodeChunk,
    RepositoryRecord,
)


def _make_session(owner_id: str = "owner_files_test") -> AnonymousSession:
    now = datetime.now(UTC)
    return AnonymousSession(
        owner_session_id=owner_id,
        last_active_at=now,
        expires_at=now,
        created_at=now,
        updated_at=now,
    )


def _make_repo(
    repository_id: str = "repo_files_123",
    owner_id: str = "owner_files_test",
    active_generation_id: str | None = "gen_1",
) -> RepositoryRecord:
    now = datetime.now(UTC)
    return RepositoryRecord(
        repository_id=repository_id,
        owner_session_id=owner_id,
        name="test-files-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=active_generation_id,
    )


def _make_chunk(
    chunk_id: str,
    relative_path: str,
    language: str = "python",
    generation_id: str | None = "gen_1",
    owner_id: str = "owner_files_test",
    repository_id: str = "repo_files_123",
) -> CodeChunk:
    now = datetime.now(UTC)
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id=repository_id,
        owner_session_id=owner_id,
        relative_path=relative_path,
        language=language,
        symbol_name=f"sym_{chunk_id}",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def func(): pass",
        content_hash="hash123",
        parser_version="1.0",
        created_at=now,
        generation_id=generation_id,
    )


def test_list_repository_files_successful_unique_sorted() -> None:
    """Verify GET /files returns a deterministic, unique, path-sorted list of indexed files."""
    session = _make_session()
    repo_record = _make_repo()

    # Out of order file paths
    chunk_z = _make_chunk("c1", "src/z_module.py", language="python")
    chunk_a = _make_chunk("c2", "src/a_module.py", language="python")
    chunk_m = _make_chunk("c3", "lib/m_utils.js", language="javascript")

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = [chunk_z, chunk_a, chunk_m]

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo

    client = TestClient(app)
    response = client.get("/api/v1/repositories/repo_files_123/files")

    assert response.status_code == 200
    data = response.json()
    assert data["repository_id"] == "repo_files_123"
    assert len(data["files"]) == 3

    # Assert sorted order by relative path
    assert data["files"][0] == {
        "path": "lib/m_utils.js",
        "language": "javascript",
        "chunk_count": 1,
    }
    assert data["files"][1] == {
        "path": "src/a_module.py",
        "language": "python",
        "chunk_count": 1,
    }
    assert data["files"][2] == {
        "path": "src/z_module.py",
        "language": "python",
        "chunk_count": 1,
    }

    mock_chunk_repo.list_by_repository.assert_called_once_with(
        owner_session_id=session.owner_session_id,
        repository_id="repo_files_123",
        generation_id="gen_1",
    )


def test_list_repository_files_owner_isolation_and_404() -> None:
    """Verify non-existent or non-owned repositories return uniform HTTP 404."""
    session = _make_session("owner_A")

    mock_repo_repo = MagicMock()
    # Case 1: Repository not found in DB
    mock_repo_repo.get_by_id.return_value = None

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo

    client = TestClient(app)

    # Test non-existent repository
    res1 = client.get("/api/v1/repositories/nonexistent_repo/files")
    assert res1.status_code == 404
    assert res1.json()["error"]["message"] == "The requested resource was not found."

    # Case 2: Repository owned by owner_B
    other_repo = _make_repo("repo_B", owner_id="owner_B")
    mock_repo_repo.get_by_id.return_value = other_repo

    res2 = client.get("/api/v1/repositories/repo_B/files")
    assert res2.status_code == 404
    assert res2.json()["error"]["message"] == "The requested resource was not found."


def test_list_repository_files_duplicate_chunks_collapse() -> None:
    """Verify multiple chunks in the same file collapse into one file entry."""
    session = _make_session()
    repo_record = _make_repo()

    c1 = _make_chunk("c1", "src/core.py", language="python")
    c2 = _make_chunk("c2", "src/core.py", language="python")
    c3 = _make_chunk("c3", "src/core.py", language="python")
    c4 = _make_chunk("c4", "src/helper.py", language="python")

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = [c1, c2, c3, c4]

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo

    client = TestClient(app)
    response = client.get("/api/v1/repositories/repo_files_123/files")

    assert response.status_code == 200
    data = response.json()
    assert len(data["files"]) == 2

    assert data["files"][0] == {"path": "src/core.py", "language": "python", "chunk_count": 3}
    assert data["files"][1] == {"path": "src/helper.py", "language": "python", "chunk_count": 1}


def test_list_repository_files_current_generation_filtering() -> None:
    """Verify chunk listing uses active_generation_id for generation filtering."""
    session = _make_session()
    repo_record = _make_repo(active_generation_id="gen_v2")

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    chunk_gen2 = _make_chunk("c_v2", "src/active.py", generation_id="gen_v2")
    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = [chunk_gen2]

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo

    client = TestClient(app)
    response = client.get("/api/v1/repositories/repo_files_123/files")

    assert response.status_code == 200
    data = response.json()
    assert len(data["files"]) == 1
    assert data["files"][0]["path"] == "src/active.py"

    # Verify that code_chunk_repo.list_by_repository was passed gen_v2
    mock_chunk_repo.list_by_repository.assert_called_once_with(
        owner_session_id=session.owner_session_id,
        repository_id="repo_files_123",
        generation_id="gen_v2",
    )


def test_list_repository_files_empty_indexed_files_returns_empty_list() -> None:
    """Verify an owned repository with no indexed files returns 200 OK with empty files list."""
    session = _make_session()
    repo_record = _make_repo(active_generation_id=None)

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = []

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: session.owner_session_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo

    client = TestClient(app)
    response = client.get("/api/v1/repositories/repo_files_123/files")

    assert response.status_code == 200
    data = response.json()
    assert data["repository_id"] == "repo_files_123"
    assert data["files"] == []
