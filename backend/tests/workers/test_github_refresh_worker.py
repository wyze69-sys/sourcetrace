"""Unit tests for background GitHub refresh worker (run_github_refresh)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from sourcetrace.models.domain import (
    ALL_GENERATIONS,
    CodeChunk,
    IndexingJobRecord,
    RepositoryRecord,
)
from sourcetrace.storage.repositories import (
    AnonymousSessionRepository,
    CodeChunkRepository,
    IndexingJobRepository,
    RepositoryRepository,
)
from sourcetrace.workers.github_refresh import run_github_refresh


class FakeRepositoryRepo(RepositoryRepository):
    def __init__(self) -> None:
        self.repos: dict[tuple[str, str], RepositoryRecord] = {}

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.repos.get((owner_session_id, repository_id))

    def save(self, record: RepositoryRecord) -> None:
        self.repos[(record.owner_session_id, record.repository_id)] = record

    def transition_status(
        self, owner_session_id: str, repository_id: str, **kwargs: Any
    ) -> RepositoryRecord | None:
        key = (owner_session_id, repository_id)
        if key in self.repos:
            repo = self.repos[key]
            updated = RepositoryRecord(
                repository_id=repo.repository_id,
                owner_session_id=repo.owner_session_id,
                name=repo.name,
                source_type=repo.source_type,
                github_url=repo.github_url,
                status=kwargs.get("new_status", repo.status),
                file_count=kwargs.get("file_count", repo.file_count),
                chunk_count=kwargs.get("chunk_count", repo.chunk_count),
                created_at=repo.created_at,
                updated_at=kwargs.get("updated_at", repo.updated_at),
                index_mode=repo.index_mode,
                active_generation_id=repo.active_generation_id,
                last_indexed_at=kwargs.get("last_indexed_at", repo.last_indexed_at),
                indexed_commit_sha=kwargs.get("indexed_commit_sha", repo.indexed_commit_sha),
                indexed_branch=kwargs.get("indexed_branch", repo.indexed_branch),
                parser_versions=kwargs.get("parser_versions", repo.parser_versions),
                flow_evidence_complete=kwargs.get(
                    "flow_evidence_complete", repo.flow_evidence_complete
                ),
                indexed_file_count=kwargs.get("indexed_file_count", repo.indexed_file_count),
                indexed_chunk_count=kwargs.get("indexed_chunk_count", repo.indexed_chunk_count),
                consecutive_refresh_failures=kwargs.get(
                    "consecutive_refresh_failures", repo.consecutive_refresh_failures
                ),
                is_stale=kwargs.get("is_stale", repo.is_stale),
            )
            self.repos[key] = updated
            return updated
        return None

    def update_active_generation(
        self,
        owner_session_id: str,
        repository_id: str,
        active_generation_id: str,
        updated_at: datetime,
        **kwargs: Any,
    ) -> RepositoryRecord | None:
        key = (owner_session_id, repository_id)
        if key in self.repos:
            repo = self.repos[key]
            updated = RepositoryRecord(
                repository_id=repo.repository_id,
                owner_session_id=repo.owner_session_id,
                name=repo.name,
                source_type=repo.source_type,
                github_url=repo.github_url,
                status=repo.status,
                file_count=kwargs.get("file_count", repo.file_count),
                chunk_count=kwargs.get("chunk_count", repo.chunk_count),
                created_at=repo.created_at,
                updated_at=updated_at,
                index_mode=repo.index_mode,
                active_generation_id=active_generation_id,
                last_indexed_at=updated_at,
                indexed_commit_sha=kwargs.get("indexed_commit_sha", repo.indexed_commit_sha),
                indexed_branch=kwargs.get("indexed_branch", repo.indexed_branch),
                parser_versions=kwargs.get("parser_versions", repo.parser_versions),
                flow_evidence_complete=kwargs.get(
                    "flow_evidence_complete", repo.flow_evidence_complete
                ),
                indexed_file_count=kwargs.get("indexed_file_count", repo.indexed_file_count),
                indexed_chunk_count=kwargs.get("indexed_chunk_count", repo.indexed_chunk_count),
                consecutive_refresh_failures=kwargs.get("consecutive_refresh_failures", 0),
                is_stale=kwargs.get("is_stale", False),
            )
            self.repos[key] = updated
            return updated
        return None


class FakeIndexingJobRepo(IndexingJobRepository):
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
            updated = IndexingJobRecord(
                job_id=job.job_id,
                repository_id=job.repository_id,
                owner_session_id=job.owner_session_id,
                status=kwargs.get("new_status", job.status),
                job_type=job.job_type,
                current_step=kwargs.get("current_step", job.current_step),
                progress_percentage=kwargs.get("progress_percentage", job.progress_percentage),
                error_message=kwargs.get("error_message", job.error_message),
                created_at=job.created_at,
                updated_at=kwargs.get("updated_at", job.updated_at),
                completed_at=kwargs.get("completed_at", job.completed_at),
            )
            self.jobs[key] = updated
            return updated
        return None


class FakeCodeChunkRepo(CodeChunkRepository):
    def __init__(self) -> None:
        self.chunks: list[CodeChunk] = []
        self.deleted_generations: list[str | None] = []

    def save_many(self, chunks: list[CodeChunk]) -> int:
        self.chunks.extend(chunks)
        return len(chunks)

    def list_by_repository(
        self,
        owner_session_id: str,
        repository_id: str,
        generation_id: str | None | object = None,
    ) -> list[CodeChunk]:
        results: list[CodeChunk] = []
        for c in self.chunks:
            if c.owner_session_id != owner_session_id or c.repository_id != repository_id:
                continue
            if generation_id is ALL_GENERATIONS or generation_id == "*":
                results.append(c)
            elif generation_id is None:
                if c.generation_id is None:
                    results.append(c)
            else:
                if c.generation_id == generation_id:
                    results.append(c)
        return results

    def delete_by_generation(
        self, owner_session_id: str, repository_id: str, generation_id: str | None
    ) -> int:
        self.deleted_generations.append(generation_id)
        before = len(self.chunks)
        if generation_id is None:
            self.chunks = [
                c
                for c in self.chunks
                if not (
                    c.owner_session_id == owner_session_id
                    and c.repository_id == repository_id
                    and c.generation_id is None
                )
            ]
        else:
            self.chunks = [
                c
                for c in self.chunks
                if not (
                    c.owner_session_id == owner_session_id
                    and c.repository_id == repository_id
                    and c.generation_id == generation_id
                )
            ]
        return before - len(self.chunks)


class FakeSessionRepo(AnonymousSessionRepository):
    def __init__(self) -> None:
        self.reserves: list[tuple[str, datetime]] = []

    def reserve_repository_slot(self, owner_session_id: str, now: datetime) -> Any:
        self.reserves.append((owner_session_id, now))


def test_worker_successful_refresh_pointer_swap_and_gc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    now = datetime.now(UTC)
    repo_repo = FakeRepositoryRepo()
    job_repo = FakeIndexingJobRepo()
    chunk_repo = FakeCodeChunkRepo()
    session_repo = FakeSessionRepo()

    # Initial legacy repo with consecutive failures = 2
    repo_rec = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id="owner_1",
        name="test-repo",
        source_type="github",
        github_url="https://github.com/test/repo",
        status="ready",
        file_count=1,
        chunk_count=1,
        created_at=now,
        updated_at=now,
        active_generation_id=None,
        consecutive_refresh_failures=2,
        index_mode="static",
    )
    repo_repo.save(repo_rec)

    # Legacy chunk in storage (generation_id=None)
    legacy_chunk = CodeChunk(
        chunk_id="chunk_legacy",
        repository_id="repo_1",
        owner_session_id="owner_1",
        relative_path="main.py",
        language="python",
        symbol_name="foo",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def foo(): pass",
        content_hash="h1",
        parser_version="python-ast-v3",
        created_at=now,
        generation_id=None,
    )
    chunk_repo.save_many([legacy_chunk])

    job_rec = IndexingJobRecord(
        job_id="job_ref_100",
        repository_id="repo_1",
        owner_session_id="owner_1",
        status="queued",
        job_type="refresh",
        current_step="Queued repository refresh",
        progress_percentage=0,
        created_at=now,
        updated_at=now,
    )
    job_repo.save(job_rec)

    from sourcetrace.ingestion.acquisition import AcquiredSource
    from sourcetrace.ingestion.archive import ExtractionManifest

    manifest = ExtractionManifest(
        file_count=1,
        total_extracted_bytes=20,
        relative_paths=("main.py",),
    )

    def mock_acquire(url: str, consumer: Any, **kwargs: Any) -> None:
        acq = AcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="github",
            resolved_branch="main",
            resolved_commit_sha="a1b2c3d4e5f6",
        )
        consumer(acq)

    monkeypatch.setattr(
        "sourcetrace.workers.github_refresh.acquire_github_source",
        mock_acquire,
    )

    # Run background refresh worker
    run_github_refresh(
        owner_session_id="owner_1",
        repository_id="repo_1",
        job_id="job_ref_100",
        repository_repo=repo_repo,
        job_repo=job_repo,
        code_chunk_repo=chunk_repo,
        session_repo=session_repo,
        now=now,
    )

    # 1. Verify job completed
    final_job = job_repo.get_by_repository("owner_1", "repo_1")
    assert final_job is not None
    assert final_job.status == "ready"
    assert final_job.progress_percentage == 100

    # 2. Verify active generation pointer switched atomically to job_ref_100
    updated_repo = repo_repo.get_by_id("owner_1", "repo_1")
    assert updated_repo is not None
    assert updated_repo.active_generation_id == "job_ref_100"
    assert updated_repo.consecutive_refresh_failures == 0
    assert updated_repo.is_stale is False
    assert updated_repo.indexed_commit_sha == "a1b2c3d4e5f6"
    assert updated_repo.indexed_branch == "main"

    # 3. Verify GC was called for old generation (None) AFTER pointer switch
    assert None in chunk_repo.deleted_generations

    # 4. Verify old legacy chunk deleted and only new generation chunks exist
    legacy_remaining = chunk_repo.list_by_repository("owner_1", "repo_1", generation_id=None)
    assert len(legacy_remaining) == 0

    # 5. Verify session TTL was extended
    assert len(session_repo.reserves) == 1


def test_worker_refresh_failure_rollback_and_failure_count(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    repo_repo = FakeRepositoryRepo()
    job_repo = FakeIndexingJobRepo()
    chunk_repo = FakeCodeChunkRepo()
    session_repo = FakeSessionRepo()

    repo_rec = RepositoryRecord(
        repository_id="repo_fail",
        owner_session_id="owner_1",
        name="test-repo",
        source_type="github",
        github_url="https://github.com/test/repo",
        status="ready",
        file_count=1,
        chunk_count=1,
        created_at=now,
        updated_at=now,
        active_generation_id="gen_old_123",
        consecutive_refresh_failures=1,
        index_mode="static",
    )
    repo_repo.save(repo_rec)

    job_rec = IndexingJobRecord(
        job_id="job_ref_fail",
        repository_id="repo_fail",
        owner_session_id="owner_1",
        status="queued",
        job_type="refresh",
        current_step="Queued repository refresh",
        progress_percentage=0,
        created_at=now,
        updated_at=now,
    )
    job_repo.save(job_rec)

    # Mock download to raise network error
    def mock_download_error(url: str, consumer: Any, **kwargs: Any) -> None:
        raise RuntimeError("GitHub connection timeout")

    monkeypatch.setattr(
        "sourcetrace.workers.github_refresh.acquire_github_source",
        mock_download_error,
    )

    run_github_refresh(
        owner_session_id="owner_1",
        repository_id="repo_fail",
        job_id="job_ref_fail",
        repository_repo=repo_repo,
        job_repo=job_repo,
        code_chunk_repo=chunk_repo,
        session_repo=session_repo,
        now=now,
    )

    # 1. Job marked failed
    final_job = job_repo.get_by_repository("owner_1", "repo_fail")
    assert final_job is not None
    assert final_job.status == "failed"
    assert "GitHub connection timeout" in str(final_job.error_message)

    # 2. Active generation pointer untouched (remains gen_old_123)
    final_repo = repo_repo.get_by_id("owner_1", "repo_fail")
    assert final_repo is not None
    assert final_repo.active_generation_id == "gen_old_123"
    assert final_repo.status == "ready"
    assert final_repo.consecutive_refresh_failures == 2

    # 3. Garbage collection called for orphaned new generation job_ref_fail
    assert "job_ref_fail" in chunk_repo.deleted_generations
