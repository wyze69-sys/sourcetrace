"""Unit and integration tests for REPO-001 Phase 5 on-demand GitHub freshness checking."""

import dataclasses
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from sourcetrace.api.dependencies import (
    get_current_owner_id,
    get_repository_repository,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.ingestion.freshness import (
    GitHubRefCache,
    check_github_freshness,
    fetch_remote_branch_sha,
)
from sourcetrace.main import app
from sourcetrace.models.domain import RepositoryRecord


class FakeRepositoryRepository:
    def __init__(self, initial_repos: list[RepositoryRecord] | None = None) -> None:
        self.repos: dict[tuple[str, str], RepositoryRecord] = {}
        if initial_repos:
            for r in initial_repos:
                self.save(r)

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.repos.get((owner_session_id, repository_id))

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for (owner, _), r in self.repos.items() if owner == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        self.repos[(repository.owner_session_id, repository.repository_id)] = repository
        return repository

    def transition_status(
        self, owner_session_id: str, repository_id: str, **kwargs: Any
    ) -> RepositoryRecord | None:
        key = (owner_session_id, repository_id)
        if key in self.repos:
            repo = self.repos[key]
            updated = dataclasses.replace(
                repo,
                status=kwargs.get("new_status", repo.status),
                updated_at=kwargs.get("updated_at", repo.updated_at),
            )
            self.repos[key] = updated
            return updated
        return None

    def update_active_generation(
        self, owner_session_id: str, repository_id: str, **kwargs: Any
    ) -> RepositoryRecord | None:
        return None

    def update_staleness(
        self,
        owner_session_id: str,
        repository_id: str,
        is_stale: bool,
        stale_checked_at: datetime,
    ) -> RepositoryRecord | None:
        key = (owner_session_id, repository_id)
        if key in self.repos:
            repo = self.repos[key]
            updated = dataclasses.replace(
                repo,
                is_stale=is_stale,
                stale_checked_at=stale_checked_at,
                updated_at=stale_checked_at,
            )
            self.repos[key] = updated
            return updated
        return None

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        key = (owner_session_id, repository_id)
        if key in self.repos:
            del self.repos[key]
            return True
        return False


def test_github_ref_cache_ttl_and_eviction() -> None:
    cache = GitHubRefCache(ttl_seconds=300.0, max_entries=2)
    t0 = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)

    cache.set("octocat", "repo1", "main", "sha1", now=t0)
    cache.set("octocat", "repo2", "main", "sha2", now=t0)

    # Cache hit within TTL
    hit, sha = cache.get("octocat", "repo1", "main", now=t0)
    assert hit is True
    assert sha == "sha1"

    # Eviction on max entries
    cache.set("octocat", "repo3", "main", "sha3", now=t0)
    hit_evicted, _ = cache.get("octocat", "repo1", "main", now=t0)
    assert hit_evicted is False  # oldest entry repo1 was evicted

    # Cache expiration after TTL (301 seconds)
    t1 = datetime(2026, 7, 27, 12, 5, 1, tzinfo=UTC)
    hit_expired, _ = cache.get("octocat", "repo2", "main", now=t1)
    assert hit_expired is False


def test_fetch_remote_branch_sha_success_and_failure() -> None:
    # 1. Success mock
    def mock_handler_success(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octocat/Hello-World/commits/main":
            return httpx.Response(200, json={"sha": "remote_sha_999"})
        return httpx.Response(404)

    client_success = httpx.Client(
        transport=httpx.MockTransport(mock_handler_success),
        trust_env=False,
    )
    sha = fetch_remote_branch_sha(
        "octocat",
        "Hello-World",
        "main",
        client=client_success,
        resolver=lambda h: ["140.82.121.4"],
    )
    assert sha == "remote_sha_999"

    # 2. Network/Rate-limit failure mock (429 Too Many Requests)
    client_fail = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(429, text="Rate limit exceeded")),
        trust_env=False,
    )
    sha_fail = fetch_remote_branch_sha(
        "octocat",
        "Hello-World",
        "main",
        client=client_fail,
        resolver=lambda h: ["140.82.121.4"],
    )
    assert sha_fail is None


def test_check_github_freshness_fresh_stale_unknown() -> None:
    cache = GitHubRefCache()
    now = datetime.now(UTC)

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sha": "sha_remote_current"})

    client = httpx.Client(
        transport=httpx.MockTransport(mock_handler),
        trust_env=False,
    )

    def resolver(h: str) -> list[str]:
        return ["140.82.121.4"]

    # Fresh: indexed SHA equals remote SHA
    is_stale, sha = check_github_freshness(
        github_url="https://github.com/octocat/Hello-World",
        indexed_branch="main",
        indexed_commit_sha="sha_remote_current",
        cache=cache,
        client=client,
        resolver=resolver,
        now=now,
    )
    assert is_stale is False
    assert sha == "sha_remote_current"

    # Stale: indexed SHA differs from cached remote SHA
    is_stale_2, sha_2 = check_github_freshness(
        github_url="https://github.com/octocat/Hello-World",
        indexed_branch="main",
        indexed_commit_sha="sha_old_outdated",
        cache=cache,
        client=client,
        resolver=resolver,
        now=now,
    )
    assert is_stale_2 is True
    assert sha_2 == "sha_remote_current"


def test_get_repository_freshness_route_integration() -> None:
    now = datetime.now(UTC)
    owner = "sess_owner_777"

    github_repo = RepositoryRecord(
        repository_id="repo_gh_freshness",
        owner_session_id=owner,
        name="Hello-World",
        source_type="github",
        github_url="https://github.com/octocat/Hello-World",
        status="ready",
        file_count=10,
        chunk_count=20,
        created_at=now,
        updated_at=now,
        indexed_branch="main",
        indexed_commit_sha="sha_v1_old",
    )

    zip_repo = RepositoryRecord(
        repository_id="repo_zip_freshness",
        owner_session_id=owner,
        name="Zip-Repo",
        source_type="zip",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
    )

    repo_repo = FakeRepositoryRepository([github_repo, zip_repo])

    app.dependency_overrides[get_current_owner_id] = lambda: owner
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_settings] = lambda: Settings(enable_stale_check=True)

    auth_headers = {"Authorization": "Bearer fake-token"}

    # Mock low-level fetcher to isolate network in route test
    import sourcetrace.ingestion.freshness as freshness_module

    freshness_module.get_global_ref_cache().clear()
    original_fetch = freshness_module.fetch_remote_branch_sha
    freshness_module.fetch_remote_branch_sha = lambda *args, **kwargs: "sha_v2_new"

    try:
        test_client = TestClient(app)

        # 1. Default check_freshness=False -> does not check staleness
        res_default = test_client.get(
            "/api/v1/repositories/repo_gh_freshness", headers=auth_headers
        )
        assert res_default.status_code == 200
        data_default = res_default.json()
        assert data_default["is_stale"] is None
        assert data_default["stale_checked_at"] is None

        # 2. Opt-in check_freshness=true -> detects stale index (sha_v2_new != sha_v1_old)
        res_fresh = test_client.get(
            "/api/v1/repositories/repo_gh_freshness?check_freshness=true",
            headers=auth_headers,
        )
        assert res_fresh.status_code == 200
        data_fresh = res_fresh.json()
        assert data_fresh["is_stale"] is True
        assert data_fresh["stale_checked_at"] is not None

        # Check repository was persisted with is_stale=True
        persisted = repo_repo.get_by_id(owner, "repo_gh_freshness")
        assert persisted is not None
        assert persisted.is_stale is True

        # 3. ZIP repository with check_freshness=true -> ignored, stays is_stale=None
        res_zip = test_client.get(
            "/api/v1/repositories/repo_zip_freshness?check_freshness=true",
            headers=auth_headers,
        )
        assert res_zip.status_code == 200
        data_zip = res_zip.json()
        assert data_zip["is_stale"] is None

        # 4. Disabled flag SOURCETRACE_ENABLE_STALE_CHECK=False -> no freshness check
        app.dependency_overrides[get_settings] = lambda: Settings(enable_stale_check=False)
        freshness_module.fetch_remote_branch_sha = lambda o, r, b, client=None, resolver=None: (
            pytest.fail("Should not call fetch_remote_branch_sha when stale check is disabled")
        )
        res_disabled = test_client.get(
            "/api/v1/repositories/repo_gh_freshness?check_freshness=true",
            headers=auth_headers,
        )
        assert res_disabled.status_code == 200

        # 5. Ownership / missing -> 404 Not Found
        res_404 = test_client.get(
            "/api/v1/repositories/repo_nonexistent?check_freshness=true",
            headers=auth_headers,
        )
        assert res_404.status_code == 404

    finally:
        freshness_module.fetch_remote_branch_sha = original_fetch
        freshness_module.get_global_ref_cache().clear()
        app.dependency_overrides.clear()


def test_get_repository_freshness_graceful_failure_on_network_error() -> None:
    now = datetime.now(UTC)
    owner = "sess_owner_888"

    github_repo = RepositoryRecord(
        repository_id="repo_gh_fail",
        owner_session_id=owner,
        name="Fail-Repo",
        source_type="github",
        github_url="https://github.com/octocat/Fail-Repo",
        status="ready",
        file_count=10,
        chunk_count=20,
        created_at=now,
        updated_at=now,
        indexed_branch="main",
        indexed_commit_sha="sha_fail_old",
    )

    repo_repo = FakeRepositoryRepository([github_repo])

    app.dependency_overrides[get_current_owner_id] = lambda: owner
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_settings] = lambda: Settings(enable_stale_check=True)

    import sourcetrace.ingestion.freshness as freshness_module

    freshness_module.get_global_ref_cache().clear()
    original_fetch = freshness_module.fetch_remote_branch_sha
    freshness_module.fetch_remote_branch_sha = lambda *args, **kwargs: None

    try:
        test_client = TestClient(app)
        res = test_client.get(
            "/api/v1/repositories/repo_gh_fail?check_freshness=true",
            headers={"Authorization": "Bearer fake-token"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["repository_id"] == "repo_gh_fail"
        assert data["is_stale"] is None  # Unknown, returns 200 OK without failing
    finally:
        freshness_module.fetch_remote_branch_sha = original_fetch
        freshness_module.get_global_ref_cache().clear()
        app.dependency_overrides.clear()
