"""Integration tests verifying zero-token static mode repository indexing and citation retrieval."""

from datetime import UTC, datetime
from pathlib import Path

from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.archive import ExtractionManifest
from sourcetrace.ingestion.indexing import RepositoryIndexingService
from sourcetrace.models.domain import CodeChunk, RepositoryRecord
from sourcetrace.retrieval.service import SemanticRetrievalService


class InMemoryCodeChunkRepository:
    """Strict fake repository for testing static mode code chunk persistence."""

    def __init__(self) -> None:
        self.chunks: list[CodeChunk] = []

    def save_many(self, chunks: list[CodeChunk]) -> int:
        for chunk in chunks:
            assert chunk.embedding_dimensions is None
            assert chunk.embedding is None
            assert chunk.embedding_model is None
            self.chunks.append(chunk)
        return len(chunks)

    def list_by_repository(self, owner_session_id: str, repository_id: str) -> list[CodeChunk]:
        return [
            c for c in self.chunks
            if c.owner_session_id == owner_session_id and c.repository_id == repository_id
        ]

    def search_vectors(self, owner_session_id: str, repository_id: str, query_vector: list[float], limit: int = 5):
        return []

    def search_lexical(self, owner_session_id: str, repository_id: str, query_text: str, limit: int = 5):
        from sourcetrace.models.domain import RetrievalResult
        results = []
        term = query_text.lower()
        for c in self.chunks:
            if c.owner_session_id == owner_session_id and c.repository_id == repository_id:
                if term in c.symbol_name.lower() or term in c.content.lower() or term in c.relative_path.lower():
                    results.append(RetrievalResult(chunk=c, score=1.0))
        return results[:limit]

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        initial = len(self.chunks)
        self.chunks = [
            c for c in self.chunks
            if not (c.owner_session_id == owner_session_id and c.repository_id == repository_id)
        ]
        return initial - len(self.chunks)


class InMemoryRepositoryRepository:
    """Fake repository metadata storage."""

    def __init__(self) -> None:
        self.repos: dict[tuple[str, str], RepositoryRecord] = {}

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.repos.get((owner_session_id, repository_id))

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for r in self.repos.values() if r.owner_session_id == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        self.repos[(repository.owner_session_id, repository.repository_id)] = repository
        return repository

    def transition_status(self, owner_session_id, repository_id, expected_status, new_status, updated_at, file_count=None, chunk_count=None):
        key = (owner_session_id, repository_id)
        repo = self.repos.get(key)
        if repo is None:
            return None
        new_repo = RepositoryRecord(
            repository_id=repo.repository_id,
            owner_session_id=repo.owner_session_id,
            name=repo.name,
            source_type=repo.source_type,
            status=new_status,
            created_at=repo.created_at,
            updated_at=updated_at,
            github_url=repo.github_url,
            file_count=file_count if file_count is not None else repo.file_count,
            chunk_count=chunk_count if chunk_count is not None else repo.chunk_count,
            index_mode=repo.index_mode,
        )
        self.repos[key] = new_repo
        return new_repo

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        return self.repos.pop((owner_session_id, repository_id), None) is not None


def test_static_mode_indexing_and_lexical_retrieval(tmp_path: Path):
    """Verify end-to-end repository indexing in static mode without AI keys."""
    source_file = tmp_path / "sample.py"
    source_file.write_text(
        "def calculate_total(items):\n    return sum(items)\n\ndef main():\n    print(calculate_total([1, 2, 3]))\n",
        encoding="utf-8",
    )

    manifest = ExtractionManifest(
        file_count=1,
        total_extracted_bytes=100,
        relative_paths=("sample.py",),
    )

    acquired_source = AcquiredSource(
        extraction_root=tmp_path,
        manifest=manifest,
        source_type="github",
    )

    owner_session_id = "test_owner_123"
    repository_id = "repo_static_456"

    repo_repo = InMemoryRepositoryRepository()
    repo_repo.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="static-test-repo",
            source_type="github",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            index_mode="static",
        )
    )

    code_chunk_repo = InMemoryCodeChunkRepository()

    service = RepositoryIndexingService(
        code_chunk_repo=code_chunk_repo,
        provider=None,
    )

    res = service.index_acquired_source(
        acquired_source=acquired_source,
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        index_mode="static",
    )

    assert res.parsed_file_count == 1
    assert res.chunk_count == 2
    assert len(code_chunk_repo.chunks) == 2

    for chunk in code_chunk_repo.chunks:
        assert chunk.embedding_dimensions is None
        assert chunk.embedding is None
        assert chunk.embedding_model is None

    retrieval_service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=code_chunk_repo,
        embedding_provider=None,
    )

    evidence = retrieval_service.retrieve(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        query="calculate_total",
    )

    assert evidence.total_retrieved >= 1
    first_item = evidence.items[0]
    assert first_item.citation.symbol_name == "calculate_total"
    assert first_item.citation.relative_path == "sample.py"
