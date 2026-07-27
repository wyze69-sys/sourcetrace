"""Focused unit tests for REPO-001 Phase 2 index migration and generation-aware chunk storage."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from sourcetrace.core.exceptions import StorageDataError
from sourcetrace.models.domain import ALL_GENERATIONS, CodeChunk, RepositoryRecord
from sourcetrace.storage.mongo_repositories import (
    MongoCodeChunkRepository,
    MongoRepositoryRepository,
    build_vector_search_index_definition,
    init_indexes,
)


def test_init_indexes_migration_creates_gen_chunk_idx_and_drops_legacy() -> None:
    mock_db = MagicMock()
    init_indexes(mock_db)

    create_calls = mock_db["code_chunks"].create_index.call_args_list
    gen_idx_call = next(
        (c for c in create_calls if c.kwargs.get("name") == "code_chunks_owner_repo_gen_chunk_idx"),
        None,
    )
    assert gen_idx_call is not None
    assert gen_idx_call.kwargs.get("unique") is True
    assert gen_idx_call.args[0] == [
        ("owner_session_id", 1),
        ("repository_id", 1),
        ("generation_id", 1),
        ("chunk_id", 1),
    ]

    mock_db["code_chunks"].drop_index.assert_called_once_with("code_chunks_owner_repo_chunk_idx")


def test_build_vector_search_index_definition_includes_generation_id() -> None:
    def_dict = build_vector_search_index_definition(dimensions=1536)
    fields = def_dict["definition"]["fields"]
    gen_filter = next((f for f in fields if f.get("path") == "generation_id"), None)
    assert gen_filter is not None
    assert gen_filter["type"] == "filter"


def test_update_active_generation_success_and_missing() -> None:
    now = datetime.now(UTC)
    mock_coll = MagicMock()
    repo_storage = MongoRepositoryRepository(collection=mock_coll)

    # Found case
    mock_coll.find_one_and_update.return_value = {
        "repository_id": "repo_1",
        "owner_session_id": "sess_1",
        "name": "test-repo",
        "source_type": "github",
        "status": "ready",
        "created_at": now,
        "updated_at": now,
        "active_generation_id": "job_gen_2",
        "last_indexed_at": now,
    }

    res = repo_storage.update_active_generation("sess_1", "repo_1", "job_gen_2", now)
    assert isinstance(res, RepositoryRecord)
    assert res.active_generation_id == "job_gen_2"
    assert res.last_indexed_at == now

    # Missing case
    mock_coll.find_one_and_update.return_value = None
    res_missing = repo_storage.update_active_generation("sess_1", "repo_999", "job_gen_2", now)
    assert res_missing is None


def test_save_many_batch_generation_id_validation() -> None:
    now = datetime.now(UTC)
    chunk1 = CodeChunk(
        chunk_id="chk_1",
        repository_id="repo_1",
        owner_session_id="sess_1",
        relative_path="a.py",
        language="python",
        symbol_name="a",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def a(): pass",
        content_hash="h1",
        parser_version="p1",
        created_at=now,
        generation_id="gen_1",
    )
    chunk2_mismatched_gen = CodeChunk(
        chunk_id="chk_2",
        repository_id="repo_1",
        owner_session_id="sess_1",
        relative_path="b.py",
        language="python",
        symbol_name="b",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def b(): pass",
        content_hash="h2",
        parser_version="p1",
        created_at=now,
        generation_id="gen_2",
    )

    mock_coll = MagicMock()
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    with pytest.raises(StorageDataError, match="Mismatched owner, repository, generation"):
        chunk_storage.save_many([chunk1, chunk2_mismatched_gen])


def test_list_by_repository_generation_scopes() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = []
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    # Legacy active snapshot: generation_id is None
    chunk_storage.list_by_repository("sess_1", "repo_1", generation_id=None)
    mock_coll.find.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
            "generation_id": None,
        }
    )

    # All generations admin scope: generation_id is ALL_GENERATIONS
    chunk_storage.list_by_repository("sess_1", "repo_1", generation_id=ALL_GENERATIONS)
    mock_coll.find.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
        }
    )

    # Generation-aware: generation_id is passed
    chunk_storage.list_by_repository("sess_1", "repo_1", generation_id="gen_100")
    mock_coll.find.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
            "generation_id": "gen_100",
        }
    )


def test_delete_by_generation() -> None:
    mock_coll = MagicMock()
    mock_coll.delete_many.return_value.deleted_count = 15
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    deleted = chunk_storage.delete_by_generation("sess_1", "repo_1", "gen_old")
    assert deleted == 15
    mock_coll.delete_many.assert_called_once_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
            "generation_id": "gen_old",
        }
    )


def test_search_lexical_with_generation_id_filter() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.limit.return_value = []
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    res = chunk_storage.search_lexical("sess_1", "repo_1", "func", generation_id="gen_xyz")
    assert res == []

    first_find_call = mock_coll.find.call_args_list[0][0][0]
    assert first_find_call["owner_session_id"] == "sess_1"
    assert first_find_call["repository_id"] == "repo_1"
    assert first_find_call["generation_id"] == "gen_xyz"


def test_search_vectors_with_generation_id_filter() -> None:
    mock_coll = MagicMock()
    mock_coll.aggregate.return_value = []
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    query_vec = [0.1] * 1536
    res = chunk_storage.search_vectors("sess_1", "repo_1", query_vec, generation_id="gen_xyz")
    assert res == []

    pipeline = mock_coll.aggregate.call_args[0][0]
    vec_search_stage = pipeline[0]["$vectorSearch"]
    assert vec_search_stage["filter"] == {
        "owner_session_id": {"$eq": "sess_1"},
        "repository_id": {"$eq": "repo_1"},
        "generation_id": {"$eq": "gen_xyz"},
    }
