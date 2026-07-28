"""Unit and contract tests for GET /api/v1/repositories/{repository_id}/files/content route."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_code_chunk_repository,
    get_current_owner_id,
    get_repository_repository,
)
from sourcetrace.models.domain import CodeChunk, RepositoryRecord


def create_test_client(
    mock_repo_repo: MagicMock,
    mock_chunk_repo: MagicMock,
    current_owner_id: str = "sess_owner_1",
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: current_owner_id
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo
    return TestClient(app)


def test_get_repository_file_content_success_unverified_eof() -> None:
    owner_id = "sess_owner_1"
    now = datetime.now(UTC)

    mock_repo = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id="gen_1",
    )

    chunks = [
        CodeChunk(
            chunk_id="c1",
            repository_id="repo_1",
            owner_session_id=owner_id,
            relative_path="src/main.py",
            language="python",
            symbol_name="foo",
            symbol_type="function",
            start_line=1,
            end_line=2,
            content="def foo():\n    return 42",
            content_hash="h1",
            parser_version="v1",
            created_at=now,
            generation_id="gen_1",
        ),
        CodeChunk(
            chunk_id="c2",
            repository_id="repo_1",
            owner_session_id=owner_id,
            relative_path="src/main.py",
            language="python",
            symbol_name="bar",
            symbol_type="function",
            start_line=3,
            end_line=4,
            content="def bar():\n    return 'hello'",
            content_hash="h2",
            parser_version="v1",
            created_at=now,
            generation_id="gen_1",
        ),
    ]

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = mock_repo

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = chunks

    client = create_test_client(mock_repo_repo, mock_chunk_repo, current_owner_id=owner_id)

    res = client.get("/api/v1/repositories/repo_1/files/content?path=src/main.py")

    assert res.status_code == 200
    data = res.json()
    assert data["repository_id"] == "repo_1"
    assert data["path"] == "src/main.py"
    assert data["language"] == "python"
    assert data["line_count"] == 4
    assert data["is_complete"] is False
    assert data["completeness_reason"] == "source_boundary_unavailable"
    assert data["content"] == "def foo():\n    return 42\ndef bar():\n    return 'hello'"


def test_get_repository_file_content_legacy_generation_passes_none_directly() -> None:
    """Verify active_generation_id=None passes generation_id=None directly to list_by_repository."""
    owner_id = "sess_owner_1"
    now = datetime.now(UTC)

    mock_repo = RepositoryRecord(
        repository_id="repo_legacy",
        owner_session_id=owner_id,
        name="legacy-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = mock_repo

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = []

    client = create_test_client(mock_repo_repo, mock_chunk_repo, current_owner_id=owner_id)

    res = client.get("/api/v1/repositories/repo_legacy/files/content?path=src/main.py")

    assert res.status_code == 404
    mock_chunk_repo.list_by_repository.assert_called_once_with(
        owner_session_id=owner_id,
        repository_id="repo_legacy",
        generation_id=None,
    )


def test_get_repository_file_content_cross_owner_404() -> None:
    owner_id_a = "sess_owner_A"
    owner_id_b = "sess_owner_B"
    now = datetime.now(UTC)

    mock_repo_a = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id=owner_id_a,
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
    )

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = mock_repo_a

    mock_chunk_repo = MagicMock()

    client = create_test_client(mock_repo_repo, mock_chunk_repo, current_owner_id=owner_id_b)

    res = client.get("/api/v1/repositories/repo_1/files/content?path=src/main.py")

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert res.json()["error"]["message"] == "The requested resource was not found."


def test_get_repository_file_content_missing_repo_or_file_404() -> None:
    owner_id = "sess_owner_1"
    now = datetime.now(UTC)

    mock_repo = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
    )

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = mock_repo

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = []

    client = create_test_client(mock_repo_repo, mock_chunk_repo, current_owner_id=owner_id)

    res = client.get("/api/v1/repositories/repo_1/files/content?path=nonexistent.py")

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert res.json()["error"]["message"] == "The requested resource was not found."


def test_get_repository_file_content_unsafe_paths_400() -> None:
    owner_id = "sess_owner_1"

    mock_repo_repo = MagicMock()
    mock_chunk_repo = MagicMock()

    client = create_test_client(mock_repo_repo, mock_chunk_repo, current_owner_id=owner_id)

    unsafe_paths = [
        "../secret.py",
        "src/../../etc/passwd",
        "/etc/passwd",
        "\\Windows\\system32",
        "C:\\Windows\\win.ini",
        "src/file%00.py",
    ]

    for p in unsafe_paths:
        res = client.get("/api/v1/repositories/repo_1/files/content", params={"path": p})
        assert res.status_code == 400, f"Failed to reject unsafe path: {p}"
        assert res.json()["error"]["code"] == "BAD_REQUEST"


def test_get_repository_file_content_reconstruction_and_ordering() -> None:
    owner_id = "sess_owner_1"
    now = datetime.now(UTC)

    mock_repo = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
    )

    # Chunks provided out of order
    chunks = [
        CodeChunk(
            chunk_id="c2",
            repository_id="repo_1",
            owner_session_id=owner_id,
            relative_path="src/app.ts",
            language="typescript",
            symbol_name="step2",
            symbol_type="function",
            start_line=3,
            end_line=4,
            content="line 3\nline 4",
            content_hash="h2",
            parser_version="v1",
            created_at=now,
        ),
        CodeChunk(
            chunk_id="c1",
            repository_id="repo_1",
            owner_session_id=owner_id,
            relative_path="src/app.ts",
            language="typescript",
            symbol_name="step1",
            symbol_type="function",
            start_line=1,
            end_line=2,
            content="line 1\nline 2",
            content_hash="h1",
            parser_version="v1",
            created_at=now,
        ),
    ]

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = mock_repo

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = chunks

    client = create_test_client(mock_repo_repo, mock_chunk_repo, current_owner_id=owner_id)

    res = client.get("/api/v1/repositories/repo_1/files/content?path=src/app.ts")

    assert res.status_code == 200
    data = res.json()
    assert data["content"] == "line 1\nline 2\nline 3\nline 4"
    assert data["line_count"] == 4
    assert data["is_complete"] is False
    assert data["completeness_reason"] == "source_boundary_unavailable"


def test_get_repository_file_content_interior_gaps_reason() -> None:
    owner_id = "sess_owner_1"
    now = datetime.now(UTC)

    mock_repo = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
    )

    # Chunk starts at line 4 (lines 1-3 missing)
    chunks = [
        CodeChunk(
            chunk_id="c1",
            repository_id="repo_1",
            owner_session_id=owner_id,
            relative_path="src/partial.py",
            language="python",
            symbol_name="partial_fn",
            symbol_type="function",
            start_line=4,
            end_line=5,
            content="def partial_fn():\n    pass",
            content_hash="h1",
            parser_version="v1",
            created_at=now,
        ),
    ]

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = mock_repo

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = chunks

    client = create_test_client(mock_repo_repo, mock_chunk_repo, current_owner_id=owner_id)

    res = client.get("/api/v1/repositories/repo_1/files/content?path=src/partial.py")

    assert res.status_code == 200
    data = res.json()
    assert data["is_complete"] is False
    assert data["completeness_reason"] == "unindexed_line_gaps"
    assert data["line_count"] == 5
    assert data["content"] == "\n\n\ndef partial_fn():\n    pass"
