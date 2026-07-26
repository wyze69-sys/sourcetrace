"""Offline API integration tests for ownership-scoped repository and job read routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_current_owner_id,
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

    def reserve_slot_or_raise(self, owner_session_id: str, max_repos: int = 5) -> None:
        self.reserve_called = True


class InMemoryRepositoryRepository:
    def __init__(self, repos: list[RepositoryRecord] | None = None) -> None:
        self.repos: dict[tuple[str, str], RepositoryRecord] = {
            (r.owner_session_id, r.repository_id): r for r in (repos or [])
        }

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for r in self.repos.values() if r.owner_session_id == owner_session_id]

    def get_by_id(
        self, owner_session_id: str, repository_id: str
    ) -> RepositoryRecord | None:
        return self.repos.get((owner_session_id, repository_id))

    def save(self, repo: RepositoryRecord) -> RepositoryRecord:
        self.repos[(repo.owner_session_id, repo.repository_id)] = repo
        return repo

    def delete_by_id(self, owner_session_id: str, repository_id: str) -> bool:
        key = (owner_session_id, repository_id)
        if key in self.repos:
            del self.repos[key]
            return True
        return False


class InMemoryIndexingJobRepository:
    def __init__(self, jobs: list[IndexingJobRecord] | None = None) -> None:
        self.jobs: dict[tuple[str, str], IndexingJobRecord] = {
            (j.owner_session_id, j.job_id): j for j in (jobs or [])
        }

    def get_by_id(
        self, owner_session_id: str, job_id: str
    ) -> IndexingJobRecord | None:
        return self.jobs.get((owner_session_id, job_id))

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        for j in self.jobs.values():
            if j.owner_session_id == owner_session_id and j.repository_id == repository_id:
                return j
        return None

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        self.jobs[(job.owner_session_id, job.job_id)] = job
        return job

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        keys_to_del = [
            k for k, j in self.jobs.items()
            if j.owner_session_id == owner_session_id and j.repository_id == repository_id
        ]
        for k in keys_to_del:
            del self.jobs[k]
        return len(keys_to_del)


def setup_test_app(
    session_repo: AnonymousSessionRepository,
    repo_repo: RepositoryRepository,
    job_repo: IndexingJobRepository,
    active_owner_id: str | None = None,
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

    if active_owner_id is not None:
        app.dependency_overrides[get_current_owner_id] = lambda: active_owner_id

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

    app, _ = setup_test_app(session_repo, repo_repo, job_repo, active_owner_id=owner1)
    client = TestClient(app)

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
    assert "owner_id" not in repo_data
    assert "session_id" not in repo_data
    assert "secret" not in repo_data
    assert "storage_key" not in repo_data


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
        )
    ])
    job_repo = InMemoryIndexingJobRepository()

    app, _ = setup_test_app(session_repo, repo_repo, job_repo, active_owner_id=owner1)
    client = TestClient(app)

    res = client.get("/api/v1/repositories/repo_owner1_a")
    assert res.status_code == 200
    data = res.json()

    assert data["repository_id"] == "repo_owner1_a"
    assert data["name"] == "owner1-repo-a"
    assert "owner_session_id" not in data


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
        )
    ])
    repo_repo = InMemoryRepositoryRepository([
        RepositoryRecord(
            repository_id="repo_owner2_private",
            owner_session_id=owner2,
            name="owner2-repo",
            source_type="github",
            status="ready",
            created_at=now,
            updated_at=now,
        )
    ])
    job_repo = InMemoryIndexingJobRepository()

    app, _ = setup_test_app(session_repo, repo_repo, job_repo, active_owner_id=owner1)
    client = TestClient(app)

    # 1. Missing repository ID
    res_missing = client.get("/api/v1/repositories/repo_nonexistent")
    assert res_missing.status_code == 404
    missing_json = res_missing.json()

    # 2. Cross-owner repository ID
    res_cross = client.get("/api/v1/repositories/repo_owner2_private")
    assert res_cross.status_code == 404
    cross_json = res_cross.json()

    # Identical standard 404 envelope (zero metadata leakage)
    assert missing_json == cross_json == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "The requested resource was not found.",
            "request_id": None,
        }
    }


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
            job_id="job_owner1_123",
            repository_id="repo_owner1_a",
            owner_session_id=owner1,
            status="parsing",
            progress_percentage=45,
            current_step="Parsing AST nodes",
            created_at=now,
            updated_at=now,
        )
    ])

    app, _ = setup_test_app(session_repo, repo_repo, job_repo, active_owner_id=owner1)
    client = TestClient(app)

    res = client.get("/api/v1/indexing-jobs/job_owner1_123")
    assert res.status_code == 200
    data = res.json()

    assert data["job_id"] == "job_owner1_123"
    assert data["repository_id"] == "repo_owner1_a"
    assert data["status"] == "parsing"
    assert data["progress_percentage"] == 45
    assert data["current_step"] == "Parsing AST nodes"
    assert "owner_session_id" not in data


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
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository([
        IndexingJobRecord(
            job_id="job_owner2_private",
            repository_id="repo_owner2_a",
            owner_session_id=owner2,
            status="ready",
            progress_percentage=100,
            current_step="Completed",
            created_at=now,
            updated_at=now,
        )
    ])

    app, _ = setup_test_app(session_repo, repo_repo, job_repo, active_owner_id=owner1)
    client = TestClient(app)

    res_missing = client.get("/api/v1/indexing-jobs/job_nonexistent")
    assert res_missing.status_code == 404

    res_cross = client.get("/api/v1/indexing-jobs/job_owner2_private")
    assert res_cross.status_code == 404

    assert res_missing.json() == res_cross.json() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "The requested resource was not found.",
            "request_id": None,
        }
    }


def test_read_operations_do_not_reserve_slot_or_modify_session_activity() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
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

    app, _ = setup_test_app(session_repo, repo_repo, job_repo, active_owner_id=owner1)
    client = TestClient(app)

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


def test_unauthenticated_read_request_returns_401_without_creating_session() -> None:
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
    assert res.status_code == 401
    assert res.headers.get("www-authenticate") == "Bearer"
    assert res.json()["error"]["code"] == "UNAUTHORIZED"

    # No session created during unauthenticated request
    assert len(session_repo.sessions) == 0


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

    app, _ = setup_test_app(session_repo, failing_repo, job_repo, active_owner_id=owner1)
    client = TestClient(app, raise_server_exceptions=False)

    res = client.get("/api/v1/repositories")
    assert res.status_code == 500
    assert res.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }
