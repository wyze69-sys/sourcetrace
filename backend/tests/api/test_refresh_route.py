"""Unit tests for POST /repositories/{repository_id}/refresh endpoint."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sourcetrace.api.dependencies import (
    get_current_owner_id,
    get_github_refresh_scheduler,
    get_indexing_job_repository,
    get_repository_repository,
)
from sourcetrace.main import app
from sourcetrace.models.domain import IndexingJobRecord, RepositoryRecord


class FakeRepositoryRepo:
    def __init__(self) -> None:
        self.repos: dict[tuple[str, str], RepositoryRecord] = {}

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.repos.get((owner_session_id, repository_id))

    def save(self, record: RepositoryRecord) -> None:
        self.repos[(record.owner_session_id, record.repository_id)] = record


class FakeIndexingJobRepo:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], IndexingJobRecord] = {}

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        return self.jobs.get((owner_session_id, repository_id))

    def save(self, record: IndexingJobRecord) -> None:
        self.jobs[(record.owner_session_id, record.repository_id)] = record

    def transition_status(
        self, owner_session_id: str, job_id: str, repository_id: str, **kwargs: Any
    ) -> IndexingJobRecord | None:
        key = (owner_session_id, repository_id)
        if key in self.jobs:
            job = self.jobs[key]
            # Simple simulation for testing
            new_status = kwargs.get("new_status", job.status)
            new_step = kwargs.get("current_step", job.current_step)
            updated = IndexingJobRecord(
                job_id=job.job_id,
                repository_id=job.repository_id,
                owner_session_id=job.owner_session_id,
                status=new_status,
                job_type=job.job_type,
                current_step=new_step,
                progress_percentage=kwargs.get("progress_percentage", job.progress_percentage),
                error_message=kwargs.get("error_message", job.error_message),
                created_at=job.created_at,
                updated_at=kwargs.get("updated_at", job.updated_at),
                completed_at=kwargs.get("completed_at", job.completed_at),
            )
            self.jobs[key] = updated
            return updated
        return None


class FakeGitHubRefreshScheduler:
    def __init__(self) -> None:
        self.scheduled_tasks: list[tuple[str, str, str]] = []

    def schedule(
        self,
        background_tasks: Any,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
    ) -> None:
        self.scheduled_tasks.append((owner_session_id, repository_id, job_id))


@pytest.fixture
def fake_repos() -> tuple[FakeRepositoryRepo, FakeIndexingJobRepo, FakeGitHubRefreshScheduler]:
    return FakeRepositoryRepo(), FakeIndexingJobRepo(), FakeGitHubRefreshScheduler()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer fake-test-token"}


def test_refresh_unauthorized(
    fake_repos: tuple[FakeRepositoryRepo, FakeIndexingJobRepo, FakeGitHubRefreshScheduler],
) -> None:
    repo_repo, job_repo, scheduler = fake_repos
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_github_refresh_scheduler] = lambda: scheduler

    try:
        client = TestClient(app)
        response = client.post("/api/v1/repositories/repo_123/refresh")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_refresh_not_found(
    fake_repos: tuple[FakeRepositoryRepo, FakeIndexingJobRepo, FakeGitHubRefreshScheduler],
    auth_headers: dict[str, str],
) -> None:
    repo_repo, job_repo, scheduler = fake_repos
    app.dependency_overrides[get_current_owner_id] = lambda: "sess_owner_123"
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_github_refresh_scheduler] = lambda: scheduler

    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/repositories/repo_nonexistent/refresh", headers=auth_headers
        )
        assert response.status_code == 404
        data = response.json()
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND"
    finally:
        app.dependency_overrides.clear()


def test_refresh_rejects_zip_and_non_ready(
    fake_repos: tuple[FakeRepositoryRepo, FakeIndexingJobRepo, FakeGitHubRefreshScheduler],
    auth_headers: dict[str, str],
) -> None:
    repo_repo, job_repo, scheduler = fake_repos
    now = datetime.now(UTC)

    # 1. ZIP repo (ready)
    zip_repo = RepositoryRecord(
        repository_id="repo_zip",
        owner_session_id="sess_owner_123",
        name="zip-repo",
        source_type="zip",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
    )
    repo_repo.save(zip_repo)

    # 2. GitHub repo (pending)
    pending_repo = RepositoryRecord(
        repository_id="repo_pending",
        owner_session_id="sess_owner_123",
        name="pending-repo",
        source_type="github",
        github_url="https://github.com/org/repo",
        status="pending",
        file_count=0,
        chunk_count=0,
        created_at=now,
        updated_at=now,
    )
    repo_repo.save(pending_repo)

    app.dependency_overrides[get_current_owner_id] = lambda: "sess_owner_123"
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_github_refresh_scheduler] = lambda: scheduler

    try:
        client = TestClient(app)

        # Zip repo -> 422
        res1 = client.post("/api/v1/repositories/repo_zip/refresh", headers=auth_headers)
        assert res1.status_code == 422

        # Pending repo -> 422
        res2 = client.post("/api/v1/repositories/repo_pending/refresh", headers=auth_headers)
        assert res2.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_refresh_rejects_concurrent_active_job(
    fake_repos: tuple[FakeRepositoryRepo, FakeIndexingJobRepo, FakeGitHubRefreshScheduler],
    auth_headers: dict[str, str],
) -> None:
    repo_repo, job_repo, scheduler = fake_repos
    now = datetime.now(UTC)

    github_repo = RepositoryRecord(
        repository_id="repo_github",
        owner_session_id="sess_owner_123",
        name="github-repo",
        source_type="github",
        github_url="https://github.com/org/repo",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
    )
    repo_repo.save(github_repo)

    # Active job in progress
    active_job = IndexingJobRecord(
        job_id="job_active_1",
        repository_id="repo_github",
        owner_session_id="sess_owner_123",
        status="parsing",
        job_type="initial",
        current_step="Parsing code",
        progress_percentage=50,
        created_at=now,
        updated_at=now,
    )
    job_repo.save(active_job)

    app.dependency_overrides[get_current_owner_id] = lambda: "sess_owner_123"
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_github_refresh_scheduler] = lambda: scheduler

    try:
        client = TestClient(app)
        res = client.post("/api/v1/repositories/repo_github/refresh", headers=auth_headers)
        assert res.status_code == 409
        body = res.json()
        assert body["error"]["code"] == "CONFLICT"
    finally:
        app.dependency_overrides.clear()


def test_refresh_success_creates_job_and_schedules_task(
    fake_repos: tuple[FakeRepositoryRepo, FakeIndexingJobRepo, FakeGitHubRefreshScheduler],
    auth_headers: dict[str, str],
) -> None:
    repo_repo, job_repo, scheduler = fake_repos
    now = datetime.now(UTC)

    github_repo = RepositoryRecord(
        repository_id="repo_github",
        owner_session_id="sess_owner_123",
        name="github-repo",
        source_type="github",
        github_url="https://github.com/org/repo",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
    )
    repo_repo.save(github_repo)

    app.dependency_overrides[get_current_owner_id] = lambda: "sess_owner_123"
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_github_refresh_scheduler] = lambda: scheduler

    try:
        client = TestClient(app)
        res = client.post("/api/v1/repositories/repo_github/refresh", headers=auth_headers)
        assert res.status_code == 202
        payload = res.json()
        assert set(payload.keys()) == {"repository", "indexing_job"}
        assert payload["repository"]["repository_id"] == "repo_github"
        assert payload["indexing_job"]["repository_id"] == "repo_github"
        assert payload["indexing_job"]["status"] == "queued"
        assert payload["indexing_job"]["job_type"] == "refresh"

        # Check job repo state
        job_in_repo = job_repo.get_by_repository("sess_owner_123", "repo_github")
        assert job_in_repo is not None
        assert job_in_repo.job_type == "refresh"
        assert job_in_repo.status == "queued"

        # Check background scheduler was invoked
        assert len(scheduler.scheduled_tasks) == 1
        owner, repo_id, job_id = scheduler.scheduled_tasks[0]
        assert owner == "sess_owner_123"
        assert repo_id == "repo_github"
        assert job_id == job_in_repo.job_id
    finally:
        app.dependency_overrides.clear()
