"""Unit & integration tests for systemic active generation recovery and reindex_required state."""

from datetime import UTC, datetime

import pytest

from sourcetrace.core.exceptions import RetrievalError
from sourcetrace.generation.client import GenerationMessage
from sourcetrace.generation.service import GroundedAnswerService
from sourcetrace.models.domain import CodeChunk, IndexingJobRecord, RepositoryRecord
from sourcetrace.retrieval.service import SemanticRetrievalService


class InMemoryRepositoryRepo:
    def __init__(
        self, repos: list[RepositoryRecord] | None = None, fail_update: bool = False
    ) -> None:
        self.repos = {r.repository_id: r for r in (repos or [])}
        self.fail_update = fail_update

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        r = self.repos.get(repository_id)
        if r and r.owner_session_id == owner_session_id:
            return r
        return None

    def update_active_generation(
        self,
        owner_session_id: str,
        repository_id: str,
        active_generation_id: str,
        updated_at: datetime,
        **kwargs,
    ) -> RepositoryRecord | None:
        if self.fail_update:
            return None
        r = self.get_by_id(owner_session_id, repository_id)
        if r is None:
            return None
        updated = RepositoryRecord(
            repository_id=r.repository_id,
            owner_session_id=r.owner_session_id,
            name=r.name,
            source_type=r.source_type,
            status=r.status,
            created_at=r.created_at,
            updated_at=updated_at,
            active_generation_id=active_generation_id,
            file_count=r.file_count,
            chunk_count=r.chunk_count,
        )
        self.repos[repository_id] = updated
        return updated


class InMemoryIndexingJobRepo:
    def __init__(self, jobs: list[IndexingJobRecord] | None = None) -> None:
        self.jobs = list(jobs or [])

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[IndexingJobRecord]:
        return [
            j
            for j in self.jobs
            if j.owner_session_id == owner_session_id and j.repository_id == repository_id
        ]


class InMemoryCodeChunkRepo:
    def __init__(self, chunks: list[CodeChunk] | None = None) -> None:
        self.chunks = list(chunks or [])

    def list_by_repository(
        self,
        owner_session_id: str,
        repository_id: str,
        generation_id: str | None | object = None,
        limit: int | None = None,
    ) -> list[CodeChunk]:
        results = []
        for c in self.chunks:
            if c.owner_session_id != owner_session_id or c.repository_id != repository_id:
                continue
            if generation_id is None:
                if c.generation_id is None:
                    results.append(c)
            elif str(generation_id) in ("*", "__ALL_GENERATIONS__", "ALL_GENERATIONS"):
                results.append(c)
            elif c.generation_id == generation_id:
                results.append(c)
        if limit is not None and limit > 0:
            return results[:limit]
        return results

    def migrate_legacy_generation(
        self, owner_session_id: str, repository_id: str, target_generation_id: str
    ) -> int:
        count = 0
        updated_chunks = []
        for c in self.chunks:
            match_owner = c.owner_session_id == owner_session_id
            match_repo = c.repository_id == repository_id
            if match_owner and match_repo and c.generation_id is None:
                updated_chunks.append(
                    CodeChunk(
                        chunk_id=c.chunk_id,
                        repository_id=c.repository_id,
                        owner_session_id=c.owner_session_id,
                        relative_path=c.relative_path,
                        language=c.language,
                        symbol_name=c.symbol_name,
                        symbol_type=c.symbol_type,
                        start_line=c.start_line,
                        end_line=c.end_line,
                        content=c.content,
                        content_hash=c.content_hash,
                        parser_version=c.parser_version,
                        created_at=c.created_at,
                        generation_id=target_generation_id,
                    )
                )
                count += 1
            else:
                updated_chunks.append(c)
        self.chunks = updated_chunks
        return count

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
        generation_id: str | None | object = None,
    ):
        from sourcetrace.models.domain import RetrievalResult

        matching_chunks = self.list_by_repository(owner_session_id, repository_id, generation_id)
        return [RetrievalResult(chunk=c, score=0.9) for c in matching_chunks[:limit]]


class MockGenerationProvider:
    @property
    def model_identifier(self) -> str:
        return "mock-provider"

    def generate(self, messages: list[GenerationMessage]) -> str:
        return "This repository starts in main.py [E1]."


def make_chunk(
    owner_id: str, repo_id: str, gen_id: str | None, chunk_id: str, path: str = "app.py"
) -> CodeChunk:
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path=path,
        language="python",
        symbol_name="main",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def main(): pass",
        content_hash="abc",
        parser_version="py-v1",
        created_at=datetime.now(UTC),
        generation_id=gen_id,
    )


def test_legacy_none_chunks_migrated_and_persisted():
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_legacy",
        owner_session_id="owner_1",
        name="LegacyRepo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    r_repo = InMemoryRepositoryRepo([repo])
    c_repo = InMemoryCodeChunkRepo(
        [
            make_chunk("owner_1", "repo_legacy", None, "c1"),
            make_chunk("owner_1", "repo_legacy", None, "c2"),
        ]
    )

    svc = SemanticRetrievalService(repository_repo=r_repo, code_chunk_repo=c_repo)
    res = svc.retrieve("owner_1", "repo_legacy", "where does app start?")

    assert res.total_retrieved == 2
    updated_repo = r_repo.get_by_id("owner_1", "repo_legacy")
    assert updated_repo.active_generation_id == "job_ref_legacy_repo_legacy"
    assert c_repo.chunks[0].generation_id == "job_ref_legacy_repo_legacy"


def test_recovery_idempotence():
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_idem",
        owner_session_id="owner_1",
        name="IdemRepo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    r_repo = InMemoryRepositoryRepo([repo])
    c_repo = InMemoryCodeChunkRepo([make_chunk("owner_1", "repo_idem", None, "c1")])

    svc = SemanticRetrievalService(repository_repo=r_repo, code_chunk_repo=c_repo)

    res1 = svc.retrieve("owner_1", "repo_idem", "where is main?")
    assert res1.total_retrieved == 1
    gen1 = r_repo.get_by_id("owner_1", "repo_idem").active_generation_id

    res2 = svc.retrieve("owner_1", "repo_idem", "where is main?")
    assert res2.total_retrieved == 1
    gen2 = r_repo.get_by_id("owner_1", "repo_idem").active_generation_id

    assert gen1 == gen2 == "job_ref_legacy_repo_idem"


def test_owner_isolation_during_recovery():
    now = datetime.now(UTC)
    repo_a = RepositoryRecord(
        repository_id="repo_shared",
        owner_session_id="owner_a",
        name="RepoA",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    repo_b = RepositoryRecord(
        repository_id="repo_shared",
        owner_session_id="owner_b",
        name="RepoB",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    r_repo = InMemoryRepositoryRepo([repo_a, repo_b])
    c_repo = InMemoryCodeChunkRepo(
        [
            make_chunk("owner_a", "repo_shared", None, "ca_1"),
        ]
    )

    svc = SemanticRetrievalService(repository_repo=r_repo, code_chunk_repo=c_repo)

    res_b = svc.retrieve("owner_b", "repo_shared", "where is main?")
    assert res_b.total_retrieved == 0
    assert res_b.reindex_required is True
    assert r_repo.get_by_id("owner_b", "repo_shared").active_generation_id is None


def test_newest_successful_completed_generation_wins():
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_modern",
        owner_session_id="owner_1",
        name="ModernRepo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    job_old = IndexingJobRecord(
        job_id="job_old_100_chunks",
        repository_id="repo_modern",
        owner_session_id="owner_1",
        status="ready",
        current_step="Ready",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    job_new = IndexingJobRecord(
        job_id="job_new_10_chunks",
        repository_id="repo_modern",
        owner_session_id="owner_1",
        status="ready",
        current_step="Ready",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        completed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    r_repo = InMemoryRepositoryRepo([repo])
    j_repo = InMemoryIndexingJobRepo([job_old, job_new])
    old_chunks = [
        make_chunk("owner_1", "repo_modern", "job_old_100_chunks", f"c_old_{i}") for i in range(100)
    ]
    new_chunks = [
        make_chunk("owner_1", "repo_modern", "job_new_10_chunks", f"c_new_{i}") for i in range(10)
    ]
    c_repo = InMemoryCodeChunkRepo(old_chunks + new_chunks)

    svc = SemanticRetrievalService(
        repository_repo=r_repo, code_chunk_repo=c_repo, indexing_job_repo=j_repo
    )
    res = svc.retrieve("owner_1", "repo_modern", "where is main?")

    assert res.total_retrieved == 5
    assert r_repo.get_by_id("owner_1", "repo_modern").active_generation_id == "job_new_10_chunks"


def test_failed_cancelled_indexing_empty_jobs_excluded():
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_jobs",
        owner_session_id="owner_1",
        name="JobsRepo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    job_failed = IndexingJobRecord(
        job_id="job_failed",
        repository_id="repo_jobs",
        owner_session_id="owner_1",
        status="failed",
        current_step="Failed",
        created_at=now,
        updated_at=now,
        completed_at=now,
    )
    job_indexing = IndexingJobRecord(
        job_id="job_indexing",
        repository_id="repo_jobs",
        owner_session_id="owner_1",
        status="parsing",
        current_step="Parsing",
        created_at=now,
        updated_at=now,
    )
    job_empty = IndexingJobRecord(
        job_id="job_empty",
        repository_id="repo_jobs",
        owner_session_id="owner_1",
        status="ready",
        current_step="Ready",
        created_at=now,
        updated_at=now,
        completed_at=now,
    )

    r_repo = InMemoryRepositoryRepo([repo])
    j_repo = InMemoryIndexingJobRepo([job_failed, job_indexing, job_empty])
    c_repo = InMemoryCodeChunkRepo(
        [
            make_chunk("owner_1", "repo_jobs", "job_failed", "c_failed"),
        ]
    )

    svc = SemanticRetrievalService(
        repository_repo=r_repo, code_chunk_repo=c_repo, indexing_job_repo=j_repo
    )
    res = svc.retrieve("owner_1", "repo_jobs", "where is main?")

    assert res.total_retrieved == 0
    assert res.reindex_required is True
    assert r_repo.get_by_id("owner_1", "repo_jobs").active_generation_id is None


def test_persistence_failure_handled_explicitly():
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_fail_persist",
        owner_session_id="owner_1",
        name="FailPersist",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    r_repo = InMemoryRepositoryRepo([repo], fail_update=True)
    c_repo = InMemoryCodeChunkRepo([make_chunk("owner_1", "repo_fail_persist", None, "c1")])

    svc = SemanticRetrievalService(repository_repo=r_repo, code_chunk_repo=c_repo)

    with pytest.raises(RetrievalError, match="Failed to persist resolved active generation safely"):
        svc.retrieve("owner_1", "repo_fail_persist", "where is main?")


def test_existing_valid_active_generation_unchanged():
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_valid",
        owner_session_id="owner_1",
        name="ValidRepo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id="job_valid_123",
    )
    r_repo = InMemoryRepositoryRepo([repo])
    c_repo = InMemoryCodeChunkRepo([make_chunk("owner_1", "repo_valid", "job_valid_123", "c1")])

    svc = SemanticRetrievalService(repository_repo=r_repo, code_chunk_repo=c_repo)
    res = svc.retrieve("owner_1", "repo_valid", "where is main?")

    assert res.total_retrieved == 1
    assert r_repo.get_by_id("owner_1", "repo_valid").active_generation_id == "job_valid_123"


def test_no_valid_source_returns_reindex_required():
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_no_source",
        owner_session_id="owner_1",
        name="NoSourceRepo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
    )
    r_repo = InMemoryRepositoryRepo([repo])
    c_repo = InMemoryCodeChunkRepo([])
    ans_svc = GroundedAnswerService(
        retrieval_service=SemanticRetrievalService(repository_repo=r_repo, code_chunk_repo=c_repo),
        generation_provider=MockGenerationProvider(),
    )

    res = ans_svc.generate_answer(
        owner_session_id="owner_1", repository_id="repo_no_source", question="What does this do?"
    )

    assert res.answer_mode == "reindex_required"
    assert res.insufficient_evidence is True
    assert "Re-indexing required" in res.answer
