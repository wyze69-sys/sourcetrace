"""Strict unit tests for REPO-001 generation read isolation across storage query scopes."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sourcetrace.models.domain import ALL_GENERATIONS, CodeChunk
from sourcetrace.storage.mongo_repositories import MongoCodeChunkRepository


def _make_chunk(chunk_id: str, generation_id: str | None) -> CodeChunk:
    now = datetime.now(UTC)
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id="repo_1",
        owner_session_id="sess_1",
        relative_path="main.py",
        language="python",
        symbol_name="func",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def func(): pass",
        content_hash=f"hash_{chunk_id}",
        parser_version="python-ast-v3",
        created_at=now,
        generation_id=generation_id,
    )


def test_legacy_active_snapshot_scope_filters_pending_generations() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = []
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    # 1. list_by_repository with default/None generation_id
    chunk_storage.list_by_repository("sess_1", "repo_1", generation_id=None)
    mock_coll.find.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
            "generation_id": None,
        }
    )

    # 2. search_lexical with None generation_id
    mock_coll.reset_mock()
    chunk_storage.search_lexical("sess_1", "repo_1", "func", generation_id=None)
    first_find_call = mock_coll.find.call_args_list[0][0][0]
    assert first_find_call["owner_session_id"] == "sess_1"
    assert first_find_call["repository_id"] == "repo_1"
    assert first_find_call["generation_id"] is None

    # 3. search_vectors with None generation_id
    mock_coll.reset_mock()
    chunk_storage.search_vectors("sess_1", "repo_1", [0.1] * 1536, generation_id=None)
    pipeline = mock_coll.aggregate.call_args[0][0]
    vec_filter = pipeline[0]["$vectorSearch"]["filter"]
    assert vec_filter["generation_id"] == {"$eq": None}


def test_exact_active_generation_scope_filters_legacy_and_other_generations() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = []
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    # 1. list_by_repository with exact generation_id
    chunk_storage.list_by_repository("sess_1", "repo_1", generation_id="gen_active_123")
    mock_coll.find.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
            "generation_id": "gen_active_123",
        }
    )

    # 2. search_lexical with exact generation_id
    mock_coll.reset_mock()
    chunk_storage.search_lexical("sess_1", "repo_1", "func", generation_id="gen_active_123")
    first_find_call = mock_coll.find.call_args_list[0][0][0]
    assert first_find_call["generation_id"] == "gen_active_123"

    # 3. search_vectors with exact generation_id
    mock_coll.reset_mock()
    chunk_storage.search_vectors("sess_1", "repo_1", [0.1] * 1536, generation_id="gen_active_123")
    pipeline = mock_coll.aggregate.call_args[0][0]
    vec_filter = pipeline[0]["$vectorSearch"]["filter"]
    assert vec_filter["generation_id"] == {"$eq": "gen_active_123"}


def test_admin_all_generations_scope_sees_all_chunks() -> None:
    mock_coll = MagicMock()
    mock_coll.find.return_value.sort.return_value = []
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    # 1. list_by_repository with ALL_GENERATIONS sentinel
    chunk_storage.list_by_repository("sess_1", "repo_1", generation_id=ALL_GENERATIONS)
    mock_coll.find.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
        }
    )

    # 2. search_lexical with "*" wildcard
    mock_coll.reset_mock()
    chunk_storage.search_lexical("sess_1", "repo_1", "func", generation_id="*")
    first_find_call = mock_coll.find.call_args_list[0][0][0]
    assert "generation_id" not in first_find_call

    # 3. search_vectors with ALL_GENERATIONS sentinel
    mock_coll.reset_mock()
    chunk_storage.search_vectors("sess_1", "repo_1", [0.1] * 1536, generation_id=ALL_GENERATIONS)
    pipeline = mock_coll.aggregate.call_args[0][0]
    vec_filter = pipeline[0]["$vectorSearch"]["filter"]
    assert "generation_id" not in vec_filter


def test_delete_by_generation_supports_legacy_and_exact_scopes() -> None:
    mock_coll = MagicMock()
    mock_coll.delete_many.return_value.deleted_count = 5
    chunk_storage = MongoCodeChunkRepository(collection=mock_coll)

    # Legacy cleanup
    deleted_legacy = chunk_storage.delete_by_generation("sess_1", "repo_1", generation_id=None)
    assert deleted_legacy == 5
    mock_coll.delete_many.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
            "generation_id": None,
        }
    )

    # Exact generation cleanup
    deleted_exact = chunk_storage.delete_by_generation("sess_1", "repo_1", generation_id="gen_old")
    assert deleted_exact == 5
    mock_coll.delete_many.assert_called_with(
        {
            "owner_session_id": "sess_1",
            "repository_id": "repo_1",
            "generation_id": "gen_old",
        }
    )
