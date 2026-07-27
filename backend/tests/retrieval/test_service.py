"""Offline unit tests for SemanticRetrievalService."""

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from sourcetrace.core.exceptions import (
    RetrievalError,
    RetrievalValidationError,
    StorageError,
)
from sourcetrace.models.domain import (
    CodeChunk,
    RepositoryRecord,
    RetrievalResult,
    RetrievedEvidence,
)
from sourcetrace.retrieval.service import SemanticRetrievalService


class FakeRepositoryRepository:
    """Offline fake repository repository for testing readiness validation."""

    def __init__(self, record: RepositoryRecord | Any | None = None) -> None:
        self.record = record
        self.get_by_id_calls: list[tuple[str, str]] = []

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        self.get_by_id_calls.append((owner_session_id, repository_id))
        if isinstance(self.record, BaseException):
            raise self.record
        return self.record


class FakeCodeChunkRepository:
    """Offline fake code chunk repository for testing vector search."""

    def __init__(self, results: list[Any] | BaseException | None = None) -> None:
        self.results = results if results is not None else []
        self.search_calls: list[dict[str, Any]] = []

    def search_vectors(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: list[float],
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]:
        self.search_calls.append(
            {
                "owner_session_id": owner_session_id,
                "repository_id": repository_id,
                "query_vector": query_vector,
                "limit": limit,
            }
        )
        if isinstance(self.results, BaseException):
            raise self.results
        return self.results


class FakeEmbeddingProvider:
    """Offline fake embedding provider for testing query embedding."""

    def __init__(
        self,
        vector: tuple[float, ...] | BaseException | Any = (0.1,) * 1536,
        model_id: str = "text-embedding-3-small",
        dimensions: int = 1536,
    ) -> None:
        self._vector = vector
        self._model_id = model_id
        self._dimensions = dimensions
        self.embed_calls: list[tuple[str, ...]] = []

    @property
    def model_identifier(self) -> str:
        if isinstance(self._model_id, BaseException):
            raise self._model_id
        return self._model_id

    @property
    def embedding_dimensions(self) -> int:
        if isinstance(self._dimensions, BaseException):
            raise self._dimensions
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.embed_calls.append(tuple(texts))
        if isinstance(self._vector, BaseException):
            raise self._vector
        if isinstance(self._vector, tuple) and self._vector and isinstance(self._vector[0], tuple):
            return self._vector
        return (self._vector,)


def _make_ready_repo(
    owner_session_id: str = "sess_001", repository_id: str = "repo_001"
) -> RepositoryRecord:
    now = datetime.now(UTC)
    return RepositoryRecord(
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        name="test-repo",
        source_type="zip",
        status="ready",
        created_at=now,
        updated_at=now,
    )


def _make_chunk(
    chunk_id: str = "chunk_001",
    owner_session_id: str = "sess_001",
    repository_id: str = "repo_001",
    relative_path: str = "sourcetrace/core/config.py",
    symbol_name: str = "Settings",
    symbol_type: str = "class",
    start_line: int = 10,
    end_line: int = 40,
    content: str = "class Settings:\n    pass",
) -> CodeChunk:
    now = datetime.now(UTC)
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        relative_path=relative_path,
        language="python",
        symbol_name=symbol_name,
        symbol_type=symbol_type,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash="hash123",
        parser_version="1.0.0",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        created_at=now,
        embedding=(0.1,) * 1536,
    )


# ---------------------------------------------------------------------------
# 1. Query Validation Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_query",
    [
        None,
        123,
        12.34,
        True,
        False,
        b"query",
        ["query"],
        {"query": "text"},
        type("HostileStr", (str,), {})("hostile"),
    ],
)
def test_query_missing_or_invalid_types(invalid_query: Any) -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalValidationError) as exc_info:
        service.retrieve("sess_001", "repo_001", invalid_query)

    assert str(exc_info.value) == "Retrieval request is invalid."
    assert len(provider.embed_calls) == 0


@pytest.mark.parametrize(
    "empty_query",
    ["", "   ", "\t\n   ", " \r\n "],
)
def test_query_empty_or_whitespace_only(empty_query: str) -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalValidationError) as exc_info:
        service.retrieve("sess_001", "repo_001", empty_query)

    assert str(exc_info.value) == "Retrieval request is invalid."
    assert len(provider.embed_calls) == 0


def test_query_nul_character() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalValidationError) as exc_info:
        service.retrieve("sess_001", "repo_001", "find\x00symbol")

    assert str(exc_info.value) == "Retrieval request is invalid."
    assert len(provider.embed_calls) == 0


@pytest.mark.parametrize(
    "control_query",
    ["find\x07bell", "find\x1bescape", "find\x7fdel"],
)
def test_query_control_characters(control_query: str) -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalValidationError) as exc_info:
        service.retrieve("sess_001", "repo_001", control_query)

    assert str(exc_info.value) == "Retrieval request is invalid."
    assert len(provider.embed_calls) == 0


def test_query_too_long() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalValidationError) as exc_info:
        service.retrieve("sess_001", "repo_001", "a" * 4001)

    assert str(exc_info.value) == "Retrieval request is invalid."
    assert len(provider.embed_calls) == 0


def test_query_exact_4000_chars() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    res = service.retrieve("sess_001", "repo_001", "a" * 4000)
    assert res.total_retrieved == 0
    assert len(provider.embed_calls) == 1


# ---------------------------------------------------------------------------
# 2. Repository Readiness Tests
# ---------------------------------------------------------------------------


def test_readiness_missing_repository() -> None:
    repo_repo = FakeRepositoryRepository(None)
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."
    assert len(provider.embed_calls) == 0
    assert len(chunk_repo.search_calls) == 0


def test_readiness_wrong_owner() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo(owner_session_id="other_owner"))
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."
    assert len(provider.embed_calls) == 0


def test_readiness_wrong_repository_id() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo(repository_id="other_repo"))
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."
    assert len(provider.embed_calls) == 0


@pytest.mark.parametrize("bad_status", ["pending", "indexing", "failed"])
def test_readiness_non_ready_status(bad_status: str) -> None:
    now = datetime.now(UTC)
    record = RepositoryRecord(
        repository_id="repo_001",
        owner_session_id="sess_001",
        name="test-repo",
        source_type="zip",
        status=bad_status,
        created_at=now,
        updated_at=now,
    )
    repo_repo = FakeRepositoryRepository(record)
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."
    assert len(provider.embed_calls) == 0


def test_readiness_malformed_record() -> None:
    repo_repo = FakeRepositoryRepository({"status": "ready"})  # Dict, not RepositoryRecord
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."
    assert len(provider.embed_calls) == 0


# ---------------------------------------------------------------------------
# 3. Provider Validation Tests
# ---------------------------------------------------------------------------


def test_provider_called_exactly_once() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    service.retrieve("sess_001", "repo_001", "  search query  ")
    assert provider.embed_calls == [("search query",)]


def test_provider_wrong_output_container() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=[(0.1,) * 1536])  # List instead of tuple
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_provider_zero_vectors() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=())
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_provider_multiple_vectors() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=((0.1,) * 1536, (0.2,) * 1536))
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_provider_wrong_dimensions() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=((0.1,) * 128,))  # 128 instead of 1536
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_provider_scalar_vector() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=(0.1,))  # Scalar tuple instead of tuple of tuples
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_provider_boolean_values() -> None:
    bad_vec = list((0.1,) * 1535) + [True]
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=(tuple(bad_vec),))
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


@pytest.mark.parametrize("invalid_float", [math.nan, math.inf, -math.inf])
def test_provider_nan_or_inf(invalid_float: float) -> None:
    bad_vec = list((0.1,) * 1535) + [invalid_float]
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=(tuple(bad_vec),))
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_provider_empty_model_identifier() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(model_id="")
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_provider_exception_with_secret() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=RuntimeError("secret_api_key_sk_123456789"))
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."
    assert "secret_api_key" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Vector Search Tests
# ---------------------------------------------------------------------------


def test_vector_search_exact_args() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    service.retrieve("sess_001", "repo_001", "query", limit=3)

    assert len(chunk_repo.search_calls) == 1
    call = chunk_repo.search_calls[0]
    assert call["owner_session_id"] == "sess_001"
    assert call["repository_id"] == "repo_001"
    assert len(call["query_vector"]) == 1536
    assert isinstance(call["query_vector"], list)
    assert call["limit"] == 3


@pytest.mark.parametrize("invalid_limit", [0, -1, True, False, 51, "5"])
def test_vector_search_invalid_limit(invalid_limit: Any) -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider, vector_search_max_limit=50)

    with pytest.raises(RetrievalValidationError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query", limit=invalid_limit)

    assert str(exc_info.value) == "Retrieval request is invalid."


def test_vector_search_repository_storage_exception() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository(StorageError("DB failed"))
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_vector_search_empty_result() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    res = service.retrieve("sess_001", "repo_001", "valid query")
    assert res.total_retrieved == 0
    assert res.items == ()


# ---------------------------------------------------------------------------
# 5. Result Validation Tests
# ---------------------------------------------------------------------------


def test_result_wrong_container() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository({"results": []})  # Dict instead of list/tuple
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_wrong_item_type() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([object()])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_subclass_item() -> None:
    class SubRetrievalResult(RetrievalResult):
        pass

    chunk = _make_chunk()
    sub_res = SubRetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([sub_res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_wrong_chunk_type() -> None:
    class SubCodeChunk(CodeChunk):
        pass

    now = datetime.now(UTC)
    sub_chunk = SubCodeChunk(
        chunk_id="chunk_001",
        repository_id="repo_001",
        owner_session_id="sess_001",
        relative_path="file.py",
        language="python",
        symbol_name="sym",
        symbol_type="func",
        start_line=1,
        end_line=2,
        content="def sym(): pass",
        content_hash="h",
        parser_version="1",
        embedding_model="m",
        embedding_dimensions=1536,
        created_at=now,
    )
    res = RetrievalResult(chunk=sub_chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_wrong_owner() -> None:
    chunk = _make_chunk(owner_session_id="other_sess")
    res = RetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_wrong_repository() -> None:
    chunk = _make_chunk(repository_id="other_repo")
    res = RetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_duplicate_chunk_ids() -> None:
    chunk1 = _make_chunk(chunk_id="dup_id")
    chunk2 = _make_chunk(chunk_id="dup_id")
    res1 = RetrievalResult(chunk=chunk1, score=0.9)
    res2 = RetrievalResult(chunk=chunk2, score=0.8)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res1, res2])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_empty_content() -> None:
    chunk = _make_chunk(content="")
    res = RetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


@pytest.mark.parametrize(
    "start,end",
    [
        (0, 10),
        (-1, 5),
        (10, 9),
    ],
)
def test_result_invalid_line_range(start: int, end: int) -> None:
    chunk = _make_chunk(start_line=start, end_line=end)
    res = RetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


@pytest.mark.parametrize("invalid_score", [math.nan, math.inf, -math.inf, True, "0.9"])
def test_result_invalid_score(invalid_score: Any) -> None:
    chunk = _make_chunk()
    res = RetrievalResult(chunk=chunk, score=invalid_score)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


@pytest.mark.parametrize(
    "bad_path",
    [
        "/abs/path.py",
        "C:\\Windows\\file.py",
        "../traversal.py",
        "folder/../file.py",
        "folder/./file.py",
        "",
        "   ",
    ],
)
def test_result_invalid_path(bad_path: str) -> None:
    chunk = _make_chunk(relative_path=bad_path)
    res = RetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


def test_result_entire_result_set_rejected() -> None:
    valid_res = RetrievalResult(chunk=_make_chunk(chunk_id="valid_1"), score=0.9)
    bad_res = RetrievalResult(chunk=_make_chunk(chunk_id="bad_1", start_line=0), score=0.8)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([valid_res, bad_res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(RetrievalError) as exc_info:
        service.retrieve("sess_001", "repo_001", "valid query")

    assert str(exc_info.value) == "Retrieval failed safely."


# ---------------------------------------------------------------------------
# 6. Deterministic Evidence & Ranking Tests
# ---------------------------------------------------------------------------


def test_evidence_ranking_tie_breaker() -> None:
    # 5 items to test score desc -> path asc -> start_line asc -> end_line asc -> chunk_id asc
    c1 = _make_chunk(chunk_id="chunk_e", relative_path="b.py", start_line=10, end_line=20)
    c2 = _make_chunk(chunk_id="chunk_a", relative_path="a.py", start_line=10, end_line=20)
    c3 = _make_chunk(chunk_id="chunk_b", relative_path="a.py", start_line=5, end_line=20)
    c4 = _make_chunk(chunk_id="chunk_c", relative_path="a.py", start_line=10, end_line=15)
    c5 = _make_chunk(chunk_id="chunk_d", relative_path="a.py", start_line=10, end_line=20)

    # Scores: c1 (0.9), c2..c5 (0.8)
    r1 = RetrievalResult(chunk=c1, score=0.9)
    r2 = RetrievalResult(chunk=c2, score=0.8)
    r3 = RetrievalResult(chunk=c3, score=0.8)
    r4 = RetrievalResult(chunk=c4, score=0.8)
    r5 = RetrievalResult(chunk=c5, score=0.8)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([r1, r2, r3, r4, r5])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    evidence = service.retrieve("sess_001", "repo_001", "query")

    assert evidence.total_retrieved == 5
    ids = [item.chunk_id for item in evidence.items]
    # Expected order:
    # 1. c1 (score 0.9) -> chunk_e
    # 2. c3 (score 0.8, path a.py, start 5) -> chunk_b
    # 3. c4 (score 0.8, path a.py, start 10, end 15) -> chunk_c
    # 4. c2 (score 0.8, path a.py, start 10, end 20, id chunk_a) -> chunk_a
    # 5. c5 (score 0.8, path a.py, start 10, end 20, id chunk_d) -> chunk_d
    assert ids == ["chunk_e", "chunk_b", "chunk_c", "chunk_a", "chunk_d"]


def test_evidence_citation_fields() -> None:
    chunk = _make_chunk(
        relative_path="src/main.py",
        symbol_name="main",
        symbol_type="function",
        start_line=15,
        end_line=25,
    )
    res = RetrievalResult(chunk=chunk, score=0.95)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    evidence = service.retrieve("sess_001", "repo_001", "query")

    assert evidence.total_retrieved == 1
    item = evidence.items[0]
    assert item.chunk_id == "chunk_001"
    assert item.score == 0.95
    assert item.citation.relative_path == "src/main.py"
    assert item.citation.start_line == 15
    assert item.citation.end_line == 25
    assert item.citation.symbol_name == "main"
    assert item.citation.symbol_type == "function"

    assert item.snippet.snippet == "class Settings:\n    pass"
    assert item.snippet.relative_path == "src/main.py"
    assert item.snippet.start_line == 15
    assert item.snippet.end_line == 25


def test_evidence_snippet_truncation() -> None:
    long_content = "def test():\n" + ("    print('line')\n" * 200)
    chunk = _make_chunk(content=long_content)
    res = RetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider, max_snippet_chars=100)

    evidence = service.retrieve("sess_001", "repo_001", "query")

    assert evidence.total_retrieved == 1
    item = evidence.items[0]
    assert len(item.snippet.snippet) <= 100
    assert item.snippet.snippet.endswith("\n[truncated...]")
    # Line metadata remains unchanged!
    assert item.citation.start_line == chunk.start_line
    assert item.citation.end_line == chunk.end_line


def test_evidence_total_size_limit() -> None:
    c1 = _make_chunk(chunk_id="c1", content="A" * 100)
    c2 = _make_chunk(chunk_id="c2", content="B" * 100)
    c3 = _make_chunk(chunk_id="c3", content="C" * 100)
    r1 = RetrievalResult(chunk=c1, score=0.9)
    r2 = RetrievalResult(chunk=c2, score=0.8)
    r3 = RetrievalResult(chunk=c3, score=0.7)

    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([r1, r2, r3])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(
        repo_repo, chunk_repo, provider, max_total_evidence_chars=150
    )

    evidence = service.retrieve("sess_001", "repo_001", "query")

    # c1 = 100 chars, c2 truncated to fit 50 remaining budget
    assert evidence.total_retrieved == 2
    assert evidence.items[0].chunk_id == "c1"
    assert evidence.items[1].chunk_id == "c2"
    total_chars = sum(len(i.snippet.snippet) for i in evidence.items)
    assert total_chars <= 150


def test_evidence_output_vector_free_and_anonymous() -> None:
    chunk = _make_chunk(owner_session_id="secret_session_id")
    res = RetrievalResult(chunk=chunk, score=0.9)

    repo_repo = FakeRepositoryRepository(_make_ready_repo(owner_session_id="secret_session_id"))
    chunk_repo = FakeCodeChunkRepository([res])
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    evidence = service.retrieve("secret_session_id", "repo_001", "query")

    item = evidence.items[0]
    assert type(item) is RetrievedEvidence
    assert not hasattr(item, "owner_session_id")
    assert not hasattr(item, "embedding")
    assert not hasattr(item, "_id")


# ---------------------------------------------------------------------------
# 7. Process Control Pass-Through Tests
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_pass_through_from_provider() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository([])
    provider = FakeEmbeddingProvider(vector=KeyboardInterrupt())
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(KeyboardInterrupt):
        service.retrieve("sess_001", "repo_001", "valid query")


def test_system_exit_pass_through_from_storage() -> None:
    repo_repo = FakeRepositoryRepository(_make_ready_repo())
    chunk_repo = FakeCodeChunkRepository(SystemExit(1))
    provider = FakeEmbeddingProvider()
    service = SemanticRetrievalService(repo_repo, chunk_repo, provider)

    with pytest.raises(SystemExit):
        service.retrieve("sess_001", "repo_001", "valid query")
