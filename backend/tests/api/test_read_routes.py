"""Offline API integration tests for ownership-scoped repository and job read routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_indexing_job_repository,
    get_repository_repository,
    get_session_repository,
    get_session_signer,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import SessionSigner
from sourcetrace.models.domain import (
    AnonymousSession,
    IndexingJobRecord,
    RepositoryRecord,
)
from sourcetrace.storage.repositories import (
    AnonymousSessionRepository,
    IndexingJobRepository,
    RepositoryRepository,
)

TEST_SECRET = "a_very_secret_key_that_is_at_least_32_bytes_long!"


class InMemoryAnonymousSessionRepository:
    def __init__(self, sessions: list[AnonymousSession] | None = None) -> None:
        self.sessions: dict[str, AnonymousSession] = {
            s.owner_session_id: s for s in (sessions or [])
        }
        self.reserve_called = False

    def get_by_id(self, owner_session_id: str) -> AnonymousSession | None:
        return self.sessions.get(owner_session_id)

    def save(self, session: AnonymousSession) -> AnonymousSession:
        self.sessions[session.owner_session_id] = session
        return session

    def delete(self, owner_session_id: str) -> bool:
        return self.sessions.pop(owner_session_id, None) is not None

    def reserve_repository_slot(
        self,
        owner_session_id: str,
        now: datetime,
        max_quota: int = 3,
        retention_days: int = 7,
    ) -> AnonymousSession | None:
        self.reserve_called = True
        return self.get_by_id(owner_session_id)

    def release_repository_slot(self, owner_session_id: str) -> bool:
        return True


class InMemoryRepositoryRepository:
    def __init__(self, records: list[RepositoryRecord] | None = None) -> None:
        self.records: list[RepositoryRecord] = records or []

    def get_by_id(
        self, owner_session_id: str, repository_id: str
    ) -> RepositoryRecord | None:
        for r in self.records:
            if r.owner_session_id == owner_session_id and r.repository_id == repository_id:
                return r
        return None

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for r in self.records if r.owner_session_id == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        self.records.append(repository)
        return repository

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        initial = len(self.records)
        self.records = [
            r for r in self.records
            if not (r.owner_session_id == owner_session_id and r.repository_id == repository_id)
        ]
        return len(self.records) < initial


class InMemoryIndexingJobRepository:
    def __init__(self, records: list[IndexingJobRecord] | None = None) -> None:
        self.records: list[IndexingJobRecord] = records or []

    def get_by_id(
        self, owner_session_id: str, job_id: str
    ) -> IndexingJobRecord | None:
        for j in self.records:
            if j.owner_session_id == owner_session_id and j.job_id == job_id:
                return j
        return None

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        for j in self.records:
            if j.owner_session_id == owner_session_id and j.repository_id == repository_id:
                return j
        return None

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        self.records.append(job)
        return job

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        initial = len(self.records)
        self.records = [
            j for j in self.records
            if not (j.owner_session_id == owner_session_id and j.repository_id == repository_id)
        ]
        return initial - len(self.records)


def setup_test_app(
    session_repo: AnonymousSessionRepository,
    repo_repo: RepositoryRepository,
    job_repo: IndexingJobRepository,
):
    app = create_app()
    settings = Settings(
        env="development", session_signing_secret=SecretStr(TEST_SECRET)
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_signer] = lambda: SessionSigner(secret=TEST_SECRET)
    app.dependency_overrides[get_session_repository] = lambda: session_repo
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo

    return app, settings


def test_import_causes_no_mongodb_connection() -> None:
    """Verifies that importing modules does not initialize MongoDB connections."""
    import sourcetrace.api.app
    import sourcetrace.api.dependencies
    import sourcetrace.api.routes.indexing_jobs
    import sourcetrace.api.routes.repositories

    assert sourcetrace.api.app.create_app is not None


def test_health_endpoint_sets_no_cookie() -> None:
    session_repo = InMemoryAnonymousSessionRepository()
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    res = client.get("/api/v1/health")
    assert res.status_code == 200
    assert "set-cookie" not in res.headers


def test_list_returns_only_current_owner_records_and_excludes_cross_owner() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner1 = "sess_owner1"
    owner2 = "sess_owner2"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner1,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        ),
        AnonymousSession(
            owner_session_id=owner2,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        ),
    ])

    repo_repo = InMemoryRepositoryRepository([
        RepositoryRecord(
            repository_id="repo_owner1_a",
            owner_session_id=owner1,
            name="owner1-repo-a",
            source_type="github",
            status="ready",
            created_at=now,
            updated_at=now,
            github_url="https://github.com/owner1/repo-a",
            file_count=5,
            chunk_count=10,
        ),
        RepositoryRecord(
            repository_id="repo_owner2_b",
            owner_session_id=owner2,
            name="owner2-repo-b",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
            file_count=2,
            chunk_count=0,
        ),
    ])
    job_repo = InMemoryIndexingJobRepository()

    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    signer = SessionSigner(TEST_SECRET)
    token = signer.create_cookie_token(owner1, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.get("/api/v1/repositories")
    assert res.status_code == 200
    data = res.json()

    assert "repositories" in data
    assert len(data["repositories"]) == 1
    repo_data = data["repositories"][0]
    assert repo_data["repository_id"] == "repo_owner1_a"
    assert repo_data["name"] == "owner1-repo-a"
    assert repo_data["source_type"] == "github"
    assert repo_data["github_url"] == "https://github.com/owner1/repo-a"
    assert repo_data["status"] == "ready"
    assert repo_data["file_count"] == 5
    assert repo_data["chunk_count"] == 10

    # Ensure sensitive fields are NEVER exposed
    assert "owner_session_id" not in repo_data
    assert "_id" not in repo_data
    assert "active_repository_count" not in repo_data


def test_repository_detail_returns_owned_record() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner1 = "sess_owner1"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner1,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        )
    ])
    repo_repo = InMemoryRepositoryRepository([
        RepositoryRecord(
            repository_id="repo_target",
            owner_session_id=owner1,
            name="my-repo",
            source_type="github",
            status="ready",
            created_at=now,
            updated_at=now,
            github_url="https://github.com/test/repo",
            file_count=12,
            chunk_count=45,
        )
    ])
    job_repo = InMemoryIndexingJobRepository()

    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)
    token = SessionSigner(TEST_SECRET).create_cookie_token(owner1, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.get("/api/v1/repositories/repo_target")
    assert res.status_code == 200
    data = res.json()
    assert data["repository_id"] == "repo_target"
    assert data["name"] == "my-repo"
    assert data["file_count"] == 12
    assert data["chunk_count"] == 45
    assert datetime.fromisoformat(data["created_at"]) is not None
    assert datetime.fromisoformat(data["updated_at"]) is not None
    assert "owner_session_id" not in data
    assert "_id" not in data


def test_missing_and_cross_owner_repository_return_identical_safe_404() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner1 = "sess_owner1"
    owner2 = "sess_owner2"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner1,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        ),
        AnonymousSession(
            owner_session_id=owner2,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        ),
    ])
    repo_repo = InMemoryRepositoryRepository([
        RepositoryRecord(
            repository_id="repo_other",
            owner_session_id=owner2,
            name="other-repo",
            source_type="zip",
            status="ready",
            created_at=now,
            updated_at=now,
        )
    ])
    job_repo = InMemoryIndexingJobRepository()

    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)
    token = SessionSigner(TEST_SECRET).create_cookie_token(owner1, exp)
    client.cookies.set("sourcetrace_session", token)

    # 1. Missing repository
    res_missing = client.get("/api/v1/repositories/repo_nonexistent")
    assert res_missing.status_code == 404
    missing_json = res_missing.json()

    # 2. Cross-owner repository
    res_cross = client.get("/api/v1/repositories/repo_other")
    assert res_cross.status_code == 404
    cross_json = res_cross.json()

    expected_envelope = {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "The requested resource was not found.",
            "request_id": None,
        }
    }
    assert missing_json == expected_envelope
    assert cross_json == expected_envelope


def test_job_polling_returns_owned_job() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner1 = "sess_owner1"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner1,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository([
        IndexingJobRecord(
            job_id="job_123",
            repository_id="repo_123",
            owner_session_id=owner1,
            status="parsing",
            current_step="Parsing Python AST symbols",
            created_at=now,
            updated_at=now,
            progress_percentage=45,
            error_message=None,
            completed_at=None,
        )
    ])

    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)
    token = SessionSigner(TEST_SECRET).create_cookie_token(owner1, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.get("/api/v1/indexing-jobs/job_123")
    assert res.status_code == 200
    data = res.json()

    assert data["job_id"] == "job_123"
    assert data["repository_id"] == "repo_123"
    assert data["status"] == "parsing"
    assert data["progress_percentage"] == 45
    assert data["current_step"] == "Parsing Python AST symbols"
    assert data["error_message"] is None
    assert data["completed_at"] is None
    assert datetime.fromisoformat(data["created_at"]) is not None
    assert datetime.fromisoformat(data["updated_at"]) is not None
    assert "owner_session_id" not in data
    assert "_id" not in data


def test_missing_and_cross_owner_job_return_identical_safe_404() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner1 = "sess_owner1"
    owner2 = "sess_owner2"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner1,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        ),
        AnonymousSession(
            owner_session_id=owner2,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        ),
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository([
        IndexingJobRecord(
            job_id="job_owner2",
            repository_id="repo_owner2",
            owner_session_id=owner2,
            status="ready",
            current_step="Completed",
            created_at=now,
            updated_at=now,
            progress_percentage=100,
            completed_at=now,
        )
    ])

    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)
    token = SessionSigner(TEST_SECRET).create_cookie_token(owner1, exp)
    client.cookies.set("sourcetrace_session", token)

    res_missing = client.get("/api/v1/indexing-jobs/job_nonexistent")
    assert res_missing.status_code == 404
    missing_json = res_missing.json()

    res_cross = client.get("/api/v1/indexing-jobs/job_owner2")
    assert res_cross.status_code == 404
    cross_json = res_cross.json()

    expected_envelope = {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "The requested resource was not found.",
            "request_id": None,
        }
    }
    assert missing_json == expected_envelope
    assert cross_json == expected_envelope


def test_read_operations_do_not_reserve_slot_or_modify_session_activity() -> None:
    now = datetime.now(UTC) - timedelta(hours=1)
    exp = datetime.now(UTC) + timedelta(days=7)
    owner1 = "sess_owner1"

    original_session = AnonymousSession(
        owner_session_id=owner1,
        last_active_at=now,
        expires_at=exp,
        created_at=now,
        updated_at=now,
    )
    session_repo = InMemoryAnonymousSessionRepository([original_session])
    repo_repo = InMemoryRepositoryRepository([
        RepositoryRecord(
            repository_id="repo_1",
            owner_session_id=owner1,
            name="repo-1",
            source_type="github",
            status="ready",
            created_at=now,
            updated_at=now,
        )
    ])
    job_repo = InMemoryIndexingJobRepository([
        IndexingJobRecord(
            job_id="job_1",
            repository_id="repo_1",
            owner_session_id=owner1,
            status="ready",
            current_step="Done",
            created_at=now,
            updated_at=now,
        )
    ])

    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)
    token = SessionSigner(TEST_SECRET).create_cookie_token(owner1, exp)
    client.cookies.set("sourcetrace_session", token)

    client.get("/api/v1/repositories")
    client.get("/api/v1/repositories/repo_1")
    client.get("/api/v1/indexing-jobs/job_1")

    # Slot reservation was never called
    assert session_repo.reserve_called is False

    # Session timestamps remain unchanged
    saved_session = session_repo.get_by_id(owner1)
    assert saved_session is not None
    assert saved_session.last_active_at == now
    assert saved_session.expires_at == exp


def test_first_request_without_cookie_provisions_session_without_exposing_records() -> None:
    now = datetime.now(UTC)
    owner_other = "sess_other"

    session_repo = InMemoryAnonymousSessionRepository()
    repo_repo = InMemoryRepositoryRepository([
        RepositoryRecord(
            repository_id="repo_other",
            owner_session_id=owner_other,
            name="other-repo",
            source_type="github",
            status="ready",
            created_at=now,
            updated_at=now,
        )
    ])
    job_repo = InMemoryIndexingJobRepository()

    app, _ = setup_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    res = client.get("/api/v1/repositories")
    assert res.status_code == 200
    assert "set-cookie" in res.headers
    data = res.json()
    assert data == {"repositories": []}

    # Session created
    assert len(session_repo.sessions) == 1
    new_session = list(session_repo.sessions.values())[0]
    assert new_session.owner_session_id.startswith("sess_")
    assert new_session.owner_session_id != owner_other


def test_storage_exception_returns_fixed_safe_500_envelope() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner1 = "sess_owner1"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner1,
            last_active_at=now,
            expires_at=exp,
            created_at=now,
            updated_at=now,
        )
    ])
    failing_repo = MagicMock()
    failing_repo.list_by_owner.side_effect = RuntimeError(
        "Database connection lost or document corrupted"
    )
    job_repo = InMemoryIndexingJobRepository()

    app, _ = setup_test_app(session_repo, failing_repo, job_repo)
    client = TestClient(app, raise_server_exceptions=False)
    token = SessionSigner(TEST_SECRET).create_cookie_token(owner1, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.get("/api/v1/repositories")
    assert res.status_code == 500
    assert res.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }
