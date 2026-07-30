"""Unit tests for search_lexical in MongoCodeChunkRepository and retrieval service."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sourcetrace.models.domain import CodeChunk, RepositoryRecord
from sourcetrace.retrieval.service import SemanticRetrievalService
from sourcetrace.storage.mongo_repositories import (
    MongoCodeChunkRepository,
)


def test_mongo_code_chunk_repository_search_lexical_filter():
    """Verify that search_lexical builds an indexed $in query on search_terms."""
    mock_collection = MagicMock()
    mock_collection.find.return_value.limit.return_value = [
        {
            "chunk_id": "chunk_1",
            "repository_id": "repo_1",
            "owner_session_id": "owner_1",
            "relative_path": "src/auth.py",
            "language": "python",
            "symbol_name": "authenticate_user",
            "symbol_type": "function",
            "start_line": 1,
            "end_line": 10,
            "content": "def authenticate_user(): pass",
            "content_hash": "hash1",
            "parser_version": "1.0",
            "created_at": datetime.now(UTC),
            "symbol_name_normalized": "authenticate user",
            "relative_path_normalized": "src auth py",
            "search_terms": ["authenticate", "user", "src", "auth", "py"],
            "search_text": "authenticate user src auth py",
        }
    ]

    repo = MongoCodeChunkRepository(collection=mock_collection)
    results = repo.search_lexical(
        owner_session_id="owner_1",
        repository_id="repo_1",
        query_text="authenticateUser",
        limit=5,
    )

    # Inspect exact Mongo filter arguments across stages
    assert mock_collection.find.call_count == 4
    called_filters = [c[0][0] for c in mock_collection.find.call_args_list]

    assert called_filters[0]["symbol_name_normalized"] == "authenticate user"
    assert called_filters[1]["relative_path_normalized"] == "authenticate user"
    assert called_filters[2]["search_terms"] == {"$all": ["authenticate", "user"]}
    assert called_filters[3]["search_terms"] == {"$in": ["authenticate", "user"]}
    assert "$regex" not in str(called_filters)

    assert len(results) == 1
    assert results[0].chunk.symbol_name == "authenticate_user"
    assert results[0].score == 1.0


def test_search_lexical_empty_and_token_caps():
    """Verify empty or non-alphanumeric queries return empty results without querying DB."""
    mock_collection = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_collection)

    assert repo.search_lexical("owner_1", "repo_1", "") == []
    assert repo.search_lexical("owner_1", "repo_1", "   ") == []
    assert repo.search_lexical("owner_1", "repo_1", "!@#$%^&*()") == []
    mock_collection.find.assert_not_called()


def test_semantic_retrieval_service_static_integration():
    """Verify SemanticRetrievalService delegates to search_lexical for static mode repositories."""
    chunk1 = CodeChunk(
        chunk_id="chunk_1",
        repository_id="repo_1",
        owner_session_id="owner_1",
        relative_path="src/utils.py",
        language="python",
        symbol_name="format_user_name",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def format_user_name(first, last):\n    return f'{first} {last}'\n",
        content_hash="hash1",
        parser_version="1.0",
        created_at=datetime.now(UTC),
        symbol_name_normalized="format user name",
        relative_path_normalized="src utils py",
        search_terms=("format", "user", "name", "src", "utils", "py"),
        search_text="format user name src utils py",
    )

    mock_chunk_repo = MagicMock()
    from sourcetrace.models.domain import RetrievalResult

    mock_chunk_repo.search_lexical.return_value = [RetrievalResult(chunk=chunk1, score=1.0)]

    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id="owner_1",
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        index_mode="static",
        active_generation_id="gen_static_1",
    )

    service = SemanticRetrievalService(
        repository_repo=mock_repo_repo,
        code_chunk_repo=mock_chunk_repo,
        embedding_provider=None,
    )

    result = service.retrieve(
        owner_session_id="owner_1",
        repository_id="repo_1",
        query="format_user_name",
    )

    mock_chunk_repo.search_lexical.assert_called_once_with(
        owner_session_id="owner_1",
        repository_id="repo_1",
        query_text="format_user_name",
        limit=5,
        generation_id="gen_static_1",
    )
    assert result.total_retrieved == 1
    assert result.items[0].citation.symbol_name == "format_user_name"


def test_lexical_candidate_ranking_exact_symbol_first_with_many_generic_matches():
    """Regression test: Generic matches must not displace exact WorkoutCard symbol."""

    now = datetime.now(UTC)
    mock_collection = MagicMock()

    exact_symbol_doc = {
        "chunk_id": "exact_workout_card",
        "repository_id": "repo_1",
        "owner_session_id": "owner_1",
        "relative_path": "src/components/WorkoutCard.tsx",
        "language": "typescript",
        "symbol_name": "WorkoutCard",
        "symbol_type": "component",
        "start_line": 1,
        "end_line": 15,
        "content": "export const WorkoutCard = () => {}",
        "content_hash": "hash_exact",
        "parser_version": "1.0",
        "created_at": now,
        "symbol_name_normalized": "workout card",
        "relative_path_normalized": "src components workoutcard tsx",
        "search_terms": ["workout", "card", "src", "components", "tsx"],
        "search_text": "workout card src components tsx",
    }

    generic_workout_docs = [
        {
            "chunk_id": f"generic_workout_{i}",
            "repository_id": "repo_1",
            "owner_session_id": "owner_1",
            "relative_path": f"src/workouts/item_{i}.ts",
            "language": "typescript",
            "symbol_name": f"workoutItem_{i}",
            "symbol_type": "function",
            "start_line": 1,
            "end_line": 10,
            "content": f"// generic workout logic {i}",
            "content_hash": f"hash_{i}",
            "parser_version": "1.0",
            "created_at": now,
            "symbol_name_normalized": f"workout item {i}",
            "relative_path_normalized": f"src workouts item {i} ts",
            "search_terms": ["workout", "item", str(i)],
            "search_text": f"workout item {i}",
        }
        for i in range(100)
    ]

    # Stage A (symbol_name_normalized: "workout card") returns exact match
    # Stage D (search_terms: {"$in": ...}) returns 100 generic matches
    def mock_find(query_filter: dict):
        mock_cursor = MagicMock()
        if "symbol_name_normalized" in query_filter:
            mock_cursor.limit.return_value = [exact_symbol_doc]
        elif "search_terms" in query_filter:
            mock_cursor.limit.return_value = generic_workout_docs
        else:
            mock_cursor.limit.return_value = []
        return mock_cursor

    mock_collection.find.side_effect = mock_find

    repo = MongoCodeChunkRepository(collection=mock_collection)
    results = repo.search_lexical(
        owner_session_id="owner_1",
        repository_id="repo_1",
        query_text="WorkoutCard",
        limit=5,
    )

    assert len(results) > 0
    assert results[0].chunk.symbol_name == "WorkoutCard"
    assert results[0].score == 1.0


def test_search_lexical_query_and_limit_bounds():
    """Verify query length and limit boundary validations in MongoCodeChunkRepository."""
    from sourcetrace.core.exceptions import StorageDataError

    mock_collection = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_collection)

    # 200 characters accepted
    query_200 = "a" * 200
    mock_collection.find.return_value.limit.return_value = []
    assert repo.search_lexical("owner_1", "repo_1", query_200, limit=5) == []

    # 201 characters rejected
    query_201 = "a" * 201
    import pytest

    with pytest.raises(StorageDataError) as exc_info:
        repo.search_lexical("owner_1", "repo_1", query_201, limit=5)
    assert "200 characters" in str(exc_info.value)

    # limit 0 rejected
    with pytest.raises(StorageDataError) as exc_info:
        repo.search_lexical("owner_1", "repo_1", "workout", limit=0)
    assert "between 1 and 50" in str(exc_info.value)

    # limit 51 rejected
    with pytest.raises(StorageDataError) as exc_info:
        repo.search_lexical("owner_1", "repo_1", "workout", limit=51)
    assert "between 1 and 50" in str(exc_info.value)


def test_mongo_code_chunk_repository_filters_stop_words_and_enforces_scope():
    """Verify MongoCodeChunkRepository query construction filters stop words and scopes queries."""
    mock_collection = MagicMock()
    mock_collection.find.return_value.limit.return_value = []

    repo = MongoCodeChunkRepository(collection=mock_collection)
    repo.search_lexical(
        owner_session_id="owner_sess_abc",
        repository_id="repo_xyz",
        query_text="Where does the application start?",
        limit=5,
        generation_id="gen_v42",
    )

    assert mock_collection.find.call_count >= 3
    called_filters = [c[0][0] for c in mock_collection.find.call_args_list]

    # Every single query filter MUST enforce owner, repository, and generation scoping
    for f in called_filters:
        assert f["owner_session_id"] == "owner_sess_abc"
        assert f["repository_id"] == "repo_xyz"
        assert f["generation_id"] == "gen_v42"

    # Stage C & Stage D MUST filter out stop words ('where', 'does', 'the')
    stage_c_all = called_filters[2]["search_terms"]["$all"]
    stage_d_in = called_filters[3]["search_terms"]["$in"]

    assert "where" not in stage_c_all
    assert "does" not in stage_c_all
    assert "the" not in stage_c_all
    assert "application" in stage_c_all
    assert "start" in stage_c_all

    assert "where" not in stage_d_in
    assert "does" not in stage_d_in
    assert "the" not in stage_d_in
    assert "application" in stage_d_in
    assert "start" in stage_d_in
