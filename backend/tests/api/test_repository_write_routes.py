"""Offline API integration tests for repository write routes (POST /repositories)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_github_indexing_scheduler,
    get_indexing_job_repository,
    get_ingestion_service,
    get_repository_repository,
    get_session_repository,
    get_session_signer,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import SessionSigner
from sourcetrace.ingestion.service import IngestionService
from sourcetrace.models.domain import (
    AnonymousSession,
    IndexingJobRecord,
    RepositoryRecord,
)

TEST_SECRET = "a_very_secret_key_that_is_at_least_32_bytes_long!"


class InMemoryAnonymousSessionRepository:
    def __init__(self, sessions: list[AnonymousSession] | None = None) -> None:
        self.sessions: dict[str, AnonymousSession] = {
            s.owner_session_id: s for s in (sessions or [])
        }
        self.reserve_called_count = 0
        self.release_called_count = 0

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
        self.reserve_called_count += 1
        session = self.get_by_id(owner_session_id)
        if session is None or session.expires_at <= now:
            return None
        active_count = getattr(session, "active_repository_count", 0)
        if active_count >= max_quota:
            return None
        # Return updated session with incremented quota
        updated = AnonymousSession(
            owner_session_id=session.owner_session_id,
            created_at=session.created_at,
            updated_at=now,
            last_active_at=now,
            expires_at=session.expires_at,
            active_repository_count=active_count + 1,
        )
        self.sessions[owner_session_id] = updated
        return updated

    def release_repository_slot(self, owner_session_id: str) -> bool:
        self.release_called_count += 1
        session = self.get_by_id(owner_session_id)
        if session is None:
            return False
        active_count = getattr(session, "active_repository_count", 0)
        new_count = max(0, active_count - 1)
        updated = AnonymousSession(
            owner_session_id=session.owner_session_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            last_active_at=session.last_active_at,
            expires_at=session.expires_at,
            active_repository_count=new_count,
        )
        self.sessions[owner_session_id] = updated
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

    def transition_status(
        self,
        owner_session_id: str,
        repository_id: str,
        expected_status: str | tuple[str, ...],
        new_status: str,
        updated_at: datetime | None = None,
        file_count: int | None = None,
        chunk_count: int | None = None,
    ) -> RepositoryRecord | None:
        rec = self.get_by_id(owner_session_id, repository_id)
        if rec is None:
            return None
        allowed = (
            (expected_status,)
            if isinstance(expected_status, str)
            else expected_status
        )
        if rec.status not in allowed:
            return None
        updated_rec = RepositoryRecord(
            repository_id=rec.repository_id,
            owner_session_id=rec.owner_session_id,
            name=rec.name,
            source_type=rec.source_type,
            status=new_status,  # type: ignore[arg-type]
            created_at=rec.created_at,
            updated_at=updated_at or datetime.now(UTC),
            github_url=rec.github_url,
            file_count=file_count if file_count is not None else rec.file_count,
            chunk_count=chunk_count if chunk_count is not None else rec.chunk_count,
        )
        self.records = [
            (
                updated_rec
                if r.repository_id == repository_id
                and r.owner_session_id == owner_session_id
                else r
            )
            for r in self.records
        ]
        return updated_rec

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

    def transition_status(
        self,
        owner_session_id: str,
        job_id: str,
        repository_id: str,
        expected_status: str | tuple[str, ...],
        new_status: str,
        current_step: str,
        progress_percentage: int | None,
        updated_at: datetime,
        error_message: str | None = None,
        completed_at: datetime | None = None,
    ) -> IndexingJobRecord | None:
        job = self.get_by_id(owner_session_id, job_id)
        if job is None or job.repository_id != repository_id:
            return None
        allowed = (
            (expected_status,)
            if isinstance(expected_status, str)
            else expected_status
        )
        if job.status not in allowed:
            return None
        updated_job = IndexingJobRecord(
            job_id=job.job_id,
            repository_id=job.repository_id,
            owner_session_id=job.owner_session_id,
            status=new_status,  # type: ignore[arg-type]
            current_step=current_step if current_step is not None else job.current_step,
            progress_percentage=(
                progress_percentage
                if progress_percentage is not None
                else job.progress_percentage
            ),
            error_message=error_message if error_message is not None else job.error_message,
            completed_at=completed_at if completed_at is not None else job.completed_at,
            created_at=job.created_at,
            updated_at=updated_at or datetime.now(UTC),
        )
        self.records = [
            updated_job if j.job_id == job_id and j.owner_session_id == owner_session_id else j
            for j in self.records
        ]
        return updated_job

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        initial = len(self.records)
        self.records = [
            j for j in self.records
            if not (j.owner_session_id == owner_session_id and j.repository_id == repository_id)
        ]
        return initial - len(self.records)


class RecordingGitHubIndexingScheduler:
    def __init__(self, should_fail: bool = False) -> None:
        self.scheduled_calls: list[tuple[str, str, str]] = []
        self.should_fail = should_fail

    def schedule(
        self,
        background_tasks: Any,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
    ) -> None:
        if self.should_fail:
            raise RuntimeError("Scheduler failure simulated")
        self.scheduled_calls.append((owner_session_id, repository_id, job_id))


def setup_write_test_app(
    session_repo: InMemoryAnonymousSessionRepository,
    repo_repo: InMemoryRepositoryRepository,
    job_repo: InMemoryIndexingJobRepository,
    scheduler: RecordingGitHubIndexingScheduler | None = None,
):
    app = create_app()
    settings = Settings(
        env="development", session_signing_secret=SecretStr(TEST_SECRET)
    )
    actual_scheduler = scheduler or RecordingGitHubIndexingScheduler()
    ingestion_service = IngestionService(
        session_repo=session_repo,
        repository_repo=repo_repo,
        job_repo=job_repo,
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_signer] = lambda: SessionSigner(secret=TEST_SECRET)
    app.dependency_overrides[get_session_repository] = lambda: session_repo
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_ingestion_service] = lambda: ingestion_service
    app.dependency_overrides[get_github_indexing_scheduler] = lambda: actual_scheduler

    return app, actual_scheduler


def test_post_github_repository_success_returns_202() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_write_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    payload = {"github_url": "https://github.com/octocat/Hello-World"}
    res = client.post("/api/v1/repositories", json=payload)

    assert res.status_code == 202
    data = res.json()

    assert "repository" in data
    assert "indexing_job" in data

    repo_data = data["repository"]
    job_data = data["indexing_job"]

    assert repo_data["name"] == "Hello-World"
    assert repo_data["source_type"] == "github"
    assert repo_data["github_url"] == "https://github.com/octocat/Hello-World"
    assert repo_data["status"] == "pending"
    assert repo_data["file_count"] == 0
    assert repo_data["chunk_count"] == 0
    assert "owner_session_id" not in repo_data
    assert "_id" not in repo_data

    assert job_data["repository_id"] == repo_data["repository_id"]
    assert job_data["status"] == "queued"
    assert job_data["progress_percentage"] == 0
    assert job_data["current_step"] == "Queued for acquisition"
    assert job_data["error_message"] is None
    assert "owner_session_id" not in job_data
    assert "_id" not in job_data

    # Scheduler verified
    assert len(scheduler.scheduled_calls) == 1
    sched_owner, sched_repo, sched_job = scheduler.scheduled_calls[0]
    assert sched_owner == owner_id
    assert sched_repo == repo_data["repository_id"]
    assert sched_job == job_data["job_id"]

    # Cookie preserved
    assert "sourcetrace_session" in client.cookies


def test_post_github_repository_missing_cookie_provisions_session() -> None:
    session_repo = InMemoryAnonymousSessionRepository()
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_write_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    payload = {"github_url": "https://github.com/octocat/Hello-World"}
    res = client.post("/api/v1/repositories", json=payload)

    assert res.status_code == 202
    assert "set-cookie" in res.headers

    assert len(scheduler.scheduled_calls) == 1
    sched_owner, _, _ = scheduler.scheduled_calls[0]
    assert sched_owner.startswith("sess_")


@pytest.mark.parametrize(
    "invalid_url",
    [
        "",
        "   ",
        "not_a_url",
        "https://gitlab.com/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "https://github.com/owner/repo/issues/1",
        "https://user:pass@github.com/owner/repo",
        "ftp://github.com/owner/repo",
    ],
)
def test_post_github_repository_validation_failures_return_422(invalid_url: str) -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_write_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    payload = {"github_url": invalid_url}
    res = client.post("/api/v1/repositories", json=payload)

    assert res.status_code == 422
    assert res.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "request_id": None,
        }
    }

    # No reservation, persistence, or scheduling
    assert session_repo.reserve_called_count == 0
    assert len(repo_repo.records) == 0
    assert len(job_repo.records) == 0
    assert len(scheduler.scheduled_calls) == 0


def test_post_github_repository_missing_body_returns_422() -> None:
    session_repo = InMemoryAnonymousSessionRepository()
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_write_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    res = client.post("/api/v1/repositories", json={})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert len(scheduler.scheduled_calls) == 0


def test_post_github_repository_quota_exceeded_returns_429() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_quota_full"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
            active_repository_count=3,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_write_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    payload = {"github_url": "https://github.com/octocat/Hello-World"}
    res = client.post("/api/v1/repositories", json=payload)

    assert res.status_code == 429
    assert res.json() == {
        "error": {
            "code": "QUOTA_EXCEEDED",
            "message": "The request cannot be processed at this time.",
            "request_id": None,
        }
    }

    assert len(scheduler.scheduled_calls) == 0
    assert len(repo_repo.records) == 0
    assert len(job_repo.records) == 0


def test_post_github_repository_persistence_failure_returns_500() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    failing_repo = MagicMock()
    failing_repo.save.side_effect = RuntimeError("Database down")
    failing_repo.get_by_id.return_value = None
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_write_test_app(session_repo, failing_repo, job_repo)
    client = TestClient(app, raise_server_exceptions=False)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    payload = {"github_url": "https://github.com/octocat/Hello-World"}
    res = client.post("/api/v1/repositories", json=payload)

    assert res.status_code == 500
    assert res.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }
    assert len(scheduler.scheduled_calls) == 0


def test_post_github_repository_scheduling_failure_performs_compensation_and_returns_500() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    failing_scheduler = RecordingGitHubIndexingScheduler(should_fail=True)

    app, _ = setup_write_test_app(
        session_repo, repo_repo, job_repo, scheduler=failing_scheduler
    )
    client = TestClient(app, raise_server_exceptions=False)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    payload = {"github_url": "https://github.com/octocat/Hello-World"}
    res = client.post("/api/v1/repositories", json=payload)

    assert res.status_code == 500
    assert res.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }

    # Compensation checked
    assert len(repo_repo.records) == 1
    assert repo_repo.records[0].status == "failed"

    assert len(job_repo.records) == 1
    assert job_repo.records[0].status == "failed"
    assert job_repo.records[0].current_step == "Scheduling failed"
    assert job_repo.records[0].error_message == "Indexing could not be scheduled safely."
    assert job_repo.records[0].completed_at is not None

    # Quota slot is NOT released while failed repo record remains
    assert session_repo.release_called_count == 0


def test_post_github_repository_ignores_cross_session_injection() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_legit"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_write_test_app(session_repo, repo_repo, job_repo)
    client = TestClient(app)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    # Attempt to pass hostile fields in payload
    payload = {
        "github_url": "https://github.com/octocat/Hello-World",
        "owner_session_id": "sess_injected_attacker",
        "status": "ready",
    }
    res = client.post("/api/v1/repositories", json=payload)
    assert res.status_code == 202

    sched_owner, _, _ = scheduler.scheduled_calls[0]
    assert sched_owner == owner_id
    assert repo_repo.records[0].owner_session_id == owner_id
    assert job_repo.records[0].owner_session_id == owner_id


def test_post_github_repository_scheduling_failure_exact_signature_and_gating() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    failing_scheduler = RecordingGitHubIndexingScheduler(should_fail=True)

    job_transition_calls: list[dict[str, Any]] = []
    original_job_transition = job_repo.transition_status

    def spy_job_transition(*args: Any, **kwargs: Any) -> IndexingJobRecord | None:
        job_transition_calls.append({"args": args, "kwargs": kwargs})
        return original_job_transition(*args, **kwargs)

    job_repo.transition_status = spy_job_transition  # type: ignore[assignment]

    app, _ = setup_write_test_app(
        session_repo, repo_repo, job_repo, scheduler=failing_scheduler
    )
    client = TestClient(app, raise_server_exceptions=False)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.post(
        "/api/v1/repositories",
        json={"github_url": "https://github.com/octocat/Hello-World"},
    )
    assert res.status_code == 500

    assert len(job_transition_calls) == 1
    call_kwargs = job_transition_calls[0]["kwargs"]
    assert call_kwargs["owner_session_id"] == owner_id
    assert call_kwargs["expected_status"] == "queued"
    assert call_kwargs["new_status"] == "failed"
    assert call_kwargs["current_step"] == "Scheduling failed"
    assert call_kwargs["progress_percentage"] is None
    assert call_kwargs["error_message"] == "Indexing could not be scheduled safely."
    assert call_kwargs["completed_at"] is not None
    assert call_kwargs["updated_at"] is not None

    assert len(repo_repo.records) == 1
    assert repo_repo.records[0].status == "failed"


def test_post_github_repository_scheduling_failure_lost_race_prevents_repo_compensation() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    failing_scheduler = RecordingGitHubIndexingScheduler(should_fail=True)

    # Job transition returns None (simulating lost claim race)
    job_repo.transition_status = MagicMock(return_value=None)
    repo_repo.transition_status = MagicMock()

    app, _ = setup_write_test_app(
        session_repo, repo_repo, job_repo, scheduler=failing_scheduler
    )
    client = TestClient(app, raise_server_exceptions=False)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.post(
        "/api/v1/repositories",
        json={"github_url": "https://github.com/octocat/Hello-World"},
    )
    assert res.status_code == 500

    # Repository compensation must NOT be called if job transition returned None
    repo_repo.transition_status.assert_not_called()


def test_post_github_repository_scheduling_failure_job_exception_masks_secrets() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    failing_scheduler = RecordingGitHubIndexingScheduler(should_fail=True)

    secret_msg = "secret_db_password_12345"
    job_repo.transition_status = MagicMock(side_effect=RuntimeError(secret_msg))
    repo_repo.transition_status = MagicMock()

    app, _ = setup_write_test_app(
        session_repo, repo_repo, job_repo, scheduler=failing_scheduler
    )
    client = TestClient(app, raise_server_exceptions=False)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.post(
        "/api/v1/repositories",
        json={"github_url": "https://github.com/octocat/Hello-World"},
    )
    assert res.status_code == 500
    res_text = res.text
    assert secret_msg not in res_text
    assert res.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }
    repo_repo.transition_status.assert_not_called()


@pytest.mark.parametrize(
    "malformed_result",
    [
        True,
        {"status": "failed"},
        IndexingJobRecord(
            job_id="job_wrong",
            repository_id="repo_wrong",
            owner_session_id="sess_wrong",
            status="failed",
            current_step="Failed",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        IndexingJobRecord(
            job_id="job_wrong",
            repository_id="repo_wrong",
            owner_session_id="sess_owner123",
            status="ready",
            current_step="Ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
    ],
)
def test_post_github_repository_scheduling_failure_malformed_job_result_prevents_repo_compensation(
    malformed_result: Any,
) -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_owner123"

    session_repo = InMemoryAnonymousSessionRepository([
        AnonymousSession(
            owner_session_id=owner_id,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            expires_at=exp,
        )
    ])
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    failing_scheduler = RecordingGitHubIndexingScheduler(should_fail=True)

    job_repo.transition_status = MagicMock(return_value=malformed_result)
    repo_repo.transition_status = MagicMock()

    app, _ = setup_write_test_app(
        session_repo, repo_repo, job_repo, scheduler=failing_scheduler
    )
    client = TestClient(app, raise_server_exceptions=False)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    res = client.post(
        "/api/v1/repositories",
        json={"github_url": "https://github.com/octocat/Hello-World"},
    )
    assert res.status_code == 500
    repo_repo.transition_status.assert_not_called()


def test_create_github_repository_request_openapi_uri_schema() -> None:
    app = create_app()
    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    assert "CreateGitHubRepositoryRequest" in schemas
    req_schema = schemas["CreateGitHubRepositoryRequest"]
    assert req_schema["properties"]["github_url"]["type"] == "string"
    assert req_schema["properties"]["github_url"]["format"] == "uri"
    assert "owner_session_id" not in req_schema.get("properties", {})


def test_create_github_repository_invalid_index_mode_returns_422() -> None:
    """Verify requesting an invalid index_mode returns HTTP 422 without creating repository."""
    signer = SessionSigner(secret=TEST_SECRET)
    now = datetime.now(UTC)
    session = AnonymousSession(
        owner_session_id="sess_invalid_mode",
        created_at=now,
        updated_at=now,
        last_active_at=now,
        expires_at=now + timedelta(days=1),
    )
    cookie_val = signer.create_cookie_token(session.owner_session_id, session.expires_at)

    session_repo = InMemoryAnonymousSessionRepository([session])
    app = create_app()
    app.dependency_overrides[get_session_repository] = lambda: session_repo
    app.dependency_overrides[get_session_signer] = lambda: signer

    client = TestClient(app)
    client.cookies.set("sourcetrace_session", cookie_val)

    response = client.post(
        "/api/v1/repositories",
        json={
            "github_url": "https://github.com/octocat/Hello-World",
            "index_mode": "unsupported_super_ai",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


