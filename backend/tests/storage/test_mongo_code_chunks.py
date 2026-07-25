"""Offline unit tests for MongoCodeChunkRepository and Atlas Vector Search operations."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pymongo.errors import PyMongoError

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import (
    StorageConfigurationError,
    StorageDataError,
    StorageOperationError,
)
from sourcetrace.models.domain import CodeChunk, RetrievalResult
from sourcetrace.storage.mongo_repositories import (
    MongoCodeChunkRepository,
    build_vector_search_index_definition,
)

_DEFAULT_CREATED_AT = object()


def _make_code_chunk(
    chunk_id: str = "chunk_101",
    repository_id: str = "repo_001",
    owner_session_id: str = "owner_001",
    relative_path: str = "src/main.py",
    language: str = "python",
    symbol_name: str = "main",
    symbol_type: str = "function",
    start_line: int = 1,
    end_line: int = 10,
    content: str = "def main(): pass",
    content_hash: str = "hash_123",
    parser_version: str = "python-ast-v1",
    embedding_model: str = "text-embedding-3-small",
    embedding_dimensions: int = 3,
    embedding: tuple[float, ...] = (0.1, 0.2, 0.3),
    created_at: Any = _DEFAULT_CREATED_AT,
) -> CodeChunk:
    if created_at is _DEFAULT_CREATED_AT:
        dt = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    else:
        dt = created_at
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        relative_path=relative_path,
        language=language,
        symbol_name=symbol_name,
        symbol_type=symbol_type,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash=content_hash,
        parser_version=parser_version,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        created_at=dt,
        embedding=embedding,
    )


def _valid_doc() -> dict[str, Any]:
    return {
        "_id": "60f7a7b8e13d2c0001f891a0",
        "chunk_id": "chunk_101",
        "repository_id": "repo_001",
        "owner_session_id": "owner_001",
        "relative_path": "src/main.py",
        "language": "python",
        "symbol_name": "main",
        "symbol_type": "function",
        "start_line": 1,
        "end_line": 10,
        "content": "def main(): pass",
        "content_hash": "hash_123",
        "parser_version": "python-ast-v1",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 3,
        "embedding": [0.1, 0.2, 0.3],
        "created_at": datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC),
    }


# ---------------------------------------------------------------------------
# 1. BSON Mapping Tests
# ---------------------------------------------------------------------------


def test_valid_document_maps_into_code_chunk() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [_valid_doc()]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    chunks = repo.list_by_repository("owner_001", "repo_001")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunk_id == "chunk_101"
    assert c.embedding == (0.1, 0.2, 0.3)
    assert isinstance(c.embedding, tuple)
    assert c.created_at.tzinfo == UTC


def test_id_field_ignored_and_never_exposed() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [_valid_doc()]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    chunks = repo.list_by_repository("owner_001", "repo_001")
    assert not hasattr(chunks[0], "_id")
    assert not hasattr(chunks[0], "id")


def test_naive_bson_datetime_normalized_to_utc() -> None:
    doc = _valid_doc()
    doc["created_at"] = datetime(2026, 7, 24, 12, 0, 0)  # Naive
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    chunks = repo.list_by_repository("owner_001", "repo_001")
    assert chunks[0].created_at.tzinfo == UTC


@pytest.mark.parametrize(
    "missing_field", ["chunk_id", "repository_id", "owner_session_id", "content"]
)
def test_missing_required_string_fields_rejected(missing_field: str) -> None:
    doc = _valid_doc()
    del doc[missing_field]
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


@pytest.mark.parametrize("empty_field", ["chunk_id", "repository_id", "owner_session_id"])
def test_empty_required_strings_rejected(empty_field: str) -> None:
    doc = _valid_doc()
    doc[empty_field] = "   "
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


@pytest.mark.parametrize("bad_int", [True, False, "10", 10.5])
def test_boolean_integers_rejected(bad_int: Any) -> None:
    doc = _valid_doc()
    doc["start_line"] = bad_int
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


@pytest.mark.parametrize("bad_start,bad_end", [(0, 5), (-1, 5), (10, 5)])
def test_invalid_line_ranges_rejected(bad_start: int, bad_end: int) -> None:
    doc = _valid_doc()
    doc["start_line"] = bad_start
    doc["end_line"] = bad_end
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


@pytest.mark.parametrize("bad_dim", [0, -1, True])
def test_zero_or_negative_dimensions_rejected(bad_dim: Any) -> None:
    doc = _valid_doc()
    doc["embedding_dimensions"] = bad_dim
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


def test_embedding_length_mismatch_rejected() -> None:
    doc = _valid_doc()
    doc["embedding_dimensions"] = 3
    doc["embedding"] = [0.1, 0.2]  # len 2 vs 3
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


@pytest.mark.parametrize("bad_elem", [True, False, "0.1", float("nan"), float("inf"), None])
def test_boolean_string_nan_inf_vector_elements_rejected(bad_elem: Any) -> None:
    doc = _valid_doc()
    doc["embedding"] = [0.1, bad_elem, 0.3]
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


def test_raw_values_and_ids_do_not_appear_in_errors() -> None:
    doc = _valid_doc()
    doc["chunk_id"] = "secret_chunk_999"
    doc["embedding"] = [0.1, float("nan"), 0.3]
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)) as exc_info:
        repo.list_by_repository("owner_001", "repo_001")
    assert "secret_chunk_999" not in str(exc_info.value)
    assert "0.1" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Batch Saving Tests
# ---------------------------------------------------------------------------


def test_empty_batch_performs_no_write() -> None:
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    res = repo.save_many([])
    assert res == 0
    assert mock_coll.bulk_write.call_count == 0


def test_mixed_owner_ids_rejected_before_writing() -> None:
    c1 = _make_code_chunk(chunk_id="c1", owner_session_id="owner_A")
    c2 = _make_code_chunk(chunk_id="c2", owner_session_id="owner_B")
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.save_many([c1, c2])
    assert mock_coll.bulk_write.call_count == 0


def test_mixed_repository_ids_rejected_before_writing() -> None:
    c1 = _make_code_chunk(chunk_id="c1", repository_id="repo_A")
    c2 = _make_code_chunk(chunk_id="c2", repository_id="repo_B")
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.save_many([c1, c2])
    assert mock_coll.bulk_write.call_count == 0


def test_mixed_models_or_dimensions_rejected() -> None:
    c1 = _make_code_chunk(chunk_id="c1", embedding_model="m1", embedding_dimensions=3)
    c2 = _make_code_chunk(chunk_id="c2", embedding_model="m2", embedding_dimensions=3)
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.save_many([c1, c2])
    assert mock_coll.bulk_write.call_count == 0


def test_duplicate_chunk_ids_in_batch_rejected() -> None:
    c1 = _make_code_chunk(chunk_id="c1")
    c2 = _make_code_chunk(chunk_id="c1")
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.save_many([c1, c2])
    assert mock_coll.bulk_write.call_count == 0


def test_invalid_item_causes_no_partial_database_call() -> None:
    c1 = _make_code_chunk(chunk_id="c1")
    c2 = _make_code_chunk(chunk_id="c2", start_line=0)  # Invalid line range
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.save_many([c1, c2])
    assert mock_coll.bulk_write.call_count == 0


def test_every_upsert_filter_includes_owner_repo_chunk_ids() -> None:
    c1 = _make_code_chunk(chunk_id="c1", owner_session_id="o1", repository_id="r1")
    c2 = _make_code_chunk(chunk_id="c2", owner_session_id="o1", repository_id="r1")
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    repo.save_many([c1, c2])
    assert mock_coll.bulk_write.call_count == 1
    requests = mock_coll.bulk_write.call_args[0][0]
    assert len(requests) == 2
    for req in requests:
        flt = req._filter
        assert flt["owner_session_id"] == "o1"
        assert flt["repository_id"] == "r1"
        assert "chunk_id" in flt
        assert "_id" not in flt


def test_bson_embeddings_are_float_lists() -> None:
    c1 = _make_code_chunk(embedding=(0.1, 0.2, 0.3))
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    repo.save_many([c1])
    requests = mock_coll.bulk_write.call_args[0][0]
    doc = requests[0]._doc
    assert isinstance(doc["embedding"], list)
    assert doc["embedding"] == [0.1, 0.2, 0.3]


def test_identical_accepted_chunks_count_input_when_mongo_reports_zero_modifications() -> None:
    c1 = _make_code_chunk(chunk_id="c1")
    c2 = _make_code_chunk(chunk_id="c2")
    mock_coll = MagicMock()
    mock_result = MagicMock()
    mock_result.modified_count = 0
    mock_result.upserted_count = 0
    mock_coll.bulk_write.return_value = mock_result
    repo = MongoCodeChunkRepository(collection=mock_coll)
    res = repo.save_many([c1, c2])
    assert res == 2


def test_pymongo_errors_masked_safely() -> None:
    c1 = _make_code_chunk()
    mock_coll = MagicMock()
    mock_coll.bulk_write.side_effect = PyMongoError("Mongo write error secret_key=sk-123")
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)) as exc_info:
        repo.save_many([c1])
    assert "sk-123" not in str(exc_info.value)
    assert "Mongo write error" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. Listing and Deletion Tests
# ---------------------------------------------------------------------------


def test_listing_filter_includes_owner_and_repo_ids() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = []
    repo = MongoCodeChunkRepository(collection=mock_coll)
    repo.list_by_repository("owner_123", "repo_456")
    expected_flt = {"owner_session_id": "owner_123", "repository_id": "repo_456"}
    mock_coll.find.assert_called_once_with(expected_flt)


def test_deterministic_sort_order_enforced() -> None:
    mock_coll = MagicMock()
    mock_cursor = MagicMock()
    mock_coll.find.return_value = mock_cursor
    mock_cursor.sort.return_value = []
    repo = MongoCodeChunkRepository(collection=mock_coll)
    repo.list_by_repository("owner_123", "repo_456")
    mock_cursor.sort.assert_called_once_with(
        [("relative_path", 1), ("start_line", 1), ("end_line", 1), ("chunk_id", 1)]
    )


def test_deletion_filter_includes_owner_and_repo_ids() -> None:
    mock_coll = MagicMock()
    mock_result = MagicMock()
    mock_result.deleted_count = 5
    mock_coll.delete_many.return_value = mock_result
    repo = MongoCodeChunkRepository(collection=mock_coll)
    cnt = repo.delete_by_repository("owner_123", "repo_456")
    assert cnt == 5
    expected_flt = {"owner_session_id": "owner_123", "repository_id": "repo_456"}
    mock_coll.delete_many.assert_called_once_with(expected_flt)


@pytest.mark.parametrize("blank_id", ["", "   "])
def test_blank_ids_cause_no_database_call(blank_id: str) -> None:
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository(blank_id, "repo_1")
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.delete_by_repository("owner_1", blank_id)
    assert mock_coll.find.call_count == 0
    assert mock_coll.delete_many.call_count == 0


# ---------------------------------------------------------------------------
# 4. Vector Search Tests
# ---------------------------------------------------------------------------


def test_vector_search_filter_contains_both_owner_and_repo_ids() -> None:
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = []
    settings = Settings(embedding_dimensions=3)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=5)
    mock_coll.aggregate.assert_called_once()
    pipeline = mock_coll.aggregate.call_args[0][0]
    vector_stage = pipeline[0]["$vectorSearch"]
    assert vector_stage["filter"]["owner_session_id"] == {"$eq": "owner_123"}
    assert vector_stage["filter"]["repository_id"] == {"$eq": "repo_456"}


def test_index_name_comes_from_trusted_configuration() -> None:
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = []
    settings = Settings(embedding_dimensions=3, vector_index_name="custom_idx_name")
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=5)
    pipeline = mock_coll.aggregate.call_args[0][0]
    assert pipeline[0]["$vectorSearch"]["index"] == "custom_idx_name"


def test_wrong_query_dimensions_cause_no_aggregate_call() -> None:
    mock_coll = MagicMock()
    settings = Settings(embedding_dimensions=3)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.search_vectors("owner_123", "repo_456", [0.1, 0.2], limit=5)  # len 2 vs 3
    assert mock_coll.aggregate.call_count == 0


@pytest.mark.parametrize(
    "invalid_query",
    [
        "source code",
        b"vector bytes",
        [0.1, "0.2", 0.3],
        [0.1, True, 0.3],
        [0.1, float("nan"), 0.3],
        [0.1, float("inf"), 0.3],
    ],
)
def test_scalar_strings_booleans_nan_inf_rejected(invalid_query: Any) -> None:
    mock_coll = MagicMock()
    settings = Settings(embedding_dimensions=3)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.search_vectors("owner_123", "repo_456", invalid_query, limit=5)
    assert mock_coll.aggregate.call_count == 0


@pytest.mark.parametrize("bad_limit", [0, -1, True, 9999])
def test_invalid_or_excessive_limits_rejected(bad_limit: Any) -> None:
    mock_coll = MagicMock()
    settings = Settings(embedding_dimensions=3, vector_search_max_limit=50)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=bad_limit)
    assert mock_coll.aggregate.call_count == 0


def test_num_candidates_at_least_limit() -> None:
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = []
    settings = Settings(embedding_dimensions=3, vector_search_num_candidates=10)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=20)
    pipeline = mock_coll.aggregate.call_args[0][0]
    vector_stage = pipeline[0]["$vectorSearch"]
    assert vector_stage["numCandidates"] == 20  # max(10, 20)


def test_scores_are_finite_floats_and_retrieval_results_returned() -> None:
    doc = _valid_doc()
    doc["score"] = 0.95
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = [doc]
    settings = Settings(embedding_dimensions=3)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    results = repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=5)
    assert len(results) == 1
    assert isinstance(results[0], RetrievalResult)
    assert results[0].score == 0.95
    assert isinstance(results[0].score, float)
    assert results[0].chunk.chunk_id == "chunk_101"


@pytest.mark.parametrize("bad_score", [None, True, "0.95", float("nan"), float("inf")])
def test_missing_or_malformed_scores_rejected(bad_score: Any) -> None:
    doc = _valid_doc()
    doc["score"] = bad_score
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = [doc]
    settings = Settings(embedding_dimensions=3)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=5)


# ---------------------------------------------------------------------------
# 5. Atlas Index-Definition Behavior
# ---------------------------------------------------------------------------


def test_index_definition_uses_cosine_similarity() -> None:
    idx_def = build_vector_search_index_definition(dimensions=1536)
    fields = idx_def["definition"]["fields"]
    vec_field = next(f for f in fields if f.get("type") == "vector")
    assert vec_field["similarity"] == "cosine"
    assert vec_field["numDimensions"] == 1536


def test_index_definition_includes_both_ownership_filter_fields() -> None:
    idx_def = build_vector_search_index_definition(dimensions=1536)
    fields = idx_def["definition"]["fields"]
    filter_paths = [f["path"] for f in fields if f.get("type") == "filter"]
    assert "owner_session_id" in filter_paths
    assert "repository_id" in filter_paths


@pytest.mark.parametrize("bad_dim", [0, -5, True, False, 3.14])
def test_index_definition_rejects_zero_or_unconfigured_dimensions(bad_dim: Any) -> None:
    with pytest.raises((StorageDataError, StorageOperationError)):
        build_vector_search_index_definition(dimensions=bad_dim)


# ---------------------------------------------------------------------------
# 6. Part 3 Correction Regression Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_dim", [True, False, "3", 3.14, 0, -3])
def test_embedding_dimensions_configuration_rejected_before_aggregate(
    bad_dim: Any,
) -> None:
    mock_coll = MagicMock()
    settings = MagicMock()
    settings.embedding_dimensions = bad_dim
    settings.vector_search_max_limit = 50
    settings.vector_search_num_candidates = 100
    settings.vector_index_name = "code_chunks_vector_index"
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError, StorageConfigurationError)):
        repo.search_vectors("owner_1", "repo_1", [0.1, 0.2, 0.3], limit=5)
    assert mock_coll.aggregate.call_count == 0


@pytest.mark.parametrize("bad_limit", [True, False, "5", 5.5, 0, -5])
def test_vector_search_max_limit_config_rejected(bad_limit: Any) -> None:
    mock_coll = MagicMock()
    settings = MagicMock()
    settings.embedding_dimensions = 3
    settings.vector_search_max_limit = bad_limit
    settings.vector_search_num_candidates = 100
    settings.vector_index_name = "code_chunks_vector_index"
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError, StorageConfigurationError)):
        repo.search_vectors("owner_1", "repo_1", [0.1, 0.2, 0.3], limit=5)
    assert mock_coll.aggregate.call_count == 0


@pytest.mark.parametrize("bad_cand", [True, False, "100", 100.5, 0, -100])
def test_vector_search_num_candidates_config_rejected(bad_cand: Any) -> None:
    mock_coll = MagicMock()
    settings = MagicMock()
    settings.embedding_dimensions = 3
    settings.vector_search_max_limit = 50
    settings.vector_search_num_candidates = bad_cand
    settings.vector_index_name = "code_chunks_vector_index"
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError, StorageConfigurationError)):
        repo.search_vectors("owner_1", "repo_1", [0.1, 0.2, 0.3], limit=5)
    assert mock_coll.aggregate.call_count == 0


def test_effective_num_candidates_at_least_limit() -> None:
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = []
    settings = MagicMock()
    settings.embedding_dimensions = 3
    settings.vector_search_max_limit = 50
    settings.vector_search_num_candidates = 10
    settings.vector_index_name = "code_chunks_vector_index"
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    repo.search_vectors("owner_1", "repo_1", [0.1, 0.2, 0.3], limit=25)
    pipeline = mock_coll.aggregate.call_args[0][0]
    num_cand = pipeline[0]["$vectorSearch"]["numCandidates"]
    assert num_cand == 25
    assert isinstance(num_cand, int)
    assert not isinstance(num_cand, bool)


@pytest.mark.parametrize("bad_idx", ["", "   ", True, None])
def test_invalid_index_name_prevents_aggregate(bad_idx: Any) -> None:
    mock_coll = MagicMock()
    settings = MagicMock()
    settings.embedding_dimensions = 3
    settings.vector_search_max_limit = 50
    settings.vector_search_num_candidates = 100
    settings.vector_index_name = bad_idx
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError, StorageConfigurationError)):
        repo.search_vectors("owner_1", "repo_1", [0.1, 0.2, 0.3], limit=5)
    assert mock_coll.aggregate.call_count == 0


class _ExplodingDatetime:
    @property
    def tzinfo(self) -> Any:
        raise RuntimeError("Exploding tzinfo secret_key=sk-12345")


@pytest.mark.parametrize(
    "bad_created_at",
    [
        None,
        "2026-07-24T12:00:00Z",
        1234567890,
        True,
        False,
        _ExplodingDatetime(),
    ],
)
def test_invalid_outgoing_created_at_variants_prevent_bulk_write(
    bad_created_at: Any,
) -> None:
    c = _make_code_chunk(created_at=bad_created_at)  # type: ignore[arg-type]
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)) as exc_info:
        repo.save_many([c])
    assert mock_coll.bulk_write.call_count == 0
    assert "sk-12345" not in str(exc_info.value)
    assert "Exploding" not in str(exc_info.value)


def test_empty_outgoing_content_prevents_bulk_write() -> None:
    c = _make_code_chunk(content="")
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.save_many([c])
    assert mock_coll.bulk_write.call_count == 0


def test_empty_stored_content_fails_strict_mapping() -> None:
    doc = _valid_doc()
    doc["content"] = ""
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = [doc]
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.list_by_repository("owner_001", "repo_001")


def test_valid_source_content_preserves_exact_whitespace_and_line_endings() -> None:
    exact_code = "\n    def main():\n        return 'hello'\n\n"
    c = _make_code_chunk(content=exact_code)
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    repo.save_many([c])
    requests = mock_coll.bulk_write.call_args[0][0]
    saved_doc = requests[0]._doc
    assert saved_doc["content"] == exact_code


@pytest.mark.parametrize("non_dict_result", ["not a dict", 12345, [1, 2, 3], None, True])
def test_non_dictionary_vector_search_result_fails_safely(non_dict_result: Any) -> None:
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = [non_dict_result]
    settings = Settings(embedding_dimensions=3)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError)) as exc_info:
        repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=5)
    assert "not a dict" not in str(exc_info.value)
    assert "12345" not in str(exc_info.value)


def test_valid_result_followed_by_malformed_result_returns_no_partial_list() -> None:
    valid_d = _valid_doc()
    valid_d["score"] = 0.9
    malformed_d = _valid_doc()
    malformed_d["score"] = float("nan")

    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = [valid_d, malformed_d]
    settings = Settings(embedding_dimensions=3)
    repo = MongoCodeChunkRepository(collection=mock_coll, settings=settings)
    with pytest.raises((StorageDataError, StorageOperationError)):
        repo.search_vectors("owner_123", "repo_456", [0.1, 0.2, 0.3], limit=5)


class _ExplodingNumeric:
    def __float__(self) -> float:
        raise RuntimeError("Secret numeric conversion error sk-numeric-key")


def test_unusual_numeric_objects_cannot_leak_raw_exceptions() -> None:
    c = _make_code_chunk(start_line=_ExplodingNumeric())  # type: ignore[arg-type]
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)
    with pytest.raises((StorageDataError, StorageOperationError)) as exc_info:
        repo.save_many([c])
    assert "sk-numeric-key" not in str(exc_info.value)


def test_database_contract_includes_unique_owner_repo_chunk_index() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    contract_path = repo_root / "docs" / "database" / "0001-mongodb-resource-contracts.md"
    with open(contract_path, encoding="utf-8") as f:
        content = f.read()
    assert "owner_session_id" in content
    assert "repository_id" in content
    assert "chunk_id" in content
    assert "unique" in content

