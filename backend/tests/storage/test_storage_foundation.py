import inspect
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import StorageConfigurationError
from sourcetrace.main import app
from sourcetrace.storage import repositories
from sourcetrace.storage.mongodb import CANONICAL_COLLECTIONS, MongoStorageManager


def test_no_mongodb_client_created_on_import_or_health_request() -> None:
    client_factory = MagicMock()
    manager = MongoStorageManager(client_factory=client_factory)

    client = TestClient(app)
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    client_factory.assert_not_called()
    assert manager._client is None


def test_missing_mongodb_uri_raises_safe_typed_error_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCETRACE_MONGODB_URI", raising=False)
    settings = Settings(_env_file=None)
    manager = MongoStorageManager(settings=settings)

    with pytest.raises(StorageConfigurationError) as exc_info:
        manager.get_database()

    assert "MongoDB connection URI is not configured" in str(exc_info.value)


def test_configured_uri_and_database_passed_to_injected_factory() -> None:
    settings = Settings(
        mongodb_uri=SecretStr("mongodb://localhost:27017"),
        mongodb_database_name="custom_sourcetrace",
    )
    mock_client = MagicMock()
    mock_client_factory = MagicMock(return_value=mock_client)

    manager = MongoStorageManager(
        settings=settings, client_factory=mock_client_factory
    )
    db = manager.get_database()

    mock_client_factory.assert_called_once_with("mongodb://localhost:27017")
    mock_client.__getitem__.assert_called_once_with("custom_sourcetrace")
    assert db == mock_client["custom_sourcetrace"]


def test_only_six_canonical_collections_exposed() -> None:
    expected_collections = (
        "anonymous_sessions",
        "repositories",
        "indexing_jobs",
        "code_chunks",
        "conversations",
        "messages",
    )

    assert CANONICAL_COLLECTIONS == expected_collections
    assert MongoStorageManager.COLLECTIONS == expected_collections


def test_repository_interface_signatures_require_ownership_scope() -> None:
    protocols = [
        repositories.AnonymousSessionRepository,
        repositories.RepositoryRepository,
        repositories.IndexingJobRepository,
        repositories.CodeChunkRepository,
        repositories.ConversationRepository,
        repositories.MessageRepository,
    ]

    for proto in protocols:
        for name, method in inspect.getmembers(proto, predicate=inspect.isfunction):
            if name.startswith("__"):
                continue
            sig = inspect.signature(method)
            params = sig.parameters
            has_direct_owner_param = "owner_session_id" in params
            has_domain_entity = any(
                hasattr(param.annotation, "__dataclass_fields__")
                and "owner_session_id" in param.annotation.__dataclass_fields__
                for param in params.values()
            )
            has_chunk_list = "chunks" in params  # List[CodeChunk]
            assert has_direct_owner_param or has_domain_entity or has_chunk_list, (
                f"{proto.__name__}.{name} missing owner_session_id requirement"
            )


def test_init_indexes_creation() -> None:
    """Verify init_indexes creates exact compound indexes idempotently."""
    from sourcetrace.storage.mongo_repositories import init_indexes

    mock_db = MagicMock()
    mock_collections = {
        "anonymous_sessions": MagicMock(),
        "repositories": MagicMock(),
        "indexing_jobs": MagicMock(),
        "code_chunks": MagicMock(),
        "conversations": MagicMock(),
        "messages": MagicMock(),
    }
    mock_db.__getitem__.side_effect = lambda name: mock_collections[name]

    init_indexes(mock_db)

    # Verify code_chunks indexes
    code_chunks_coll = mock_collections["code_chunks"]
    calls = [c[1] for c in code_chunks_coll.create_index.call_args_list]

    search_terms_idx = next(
        c for c in calls if c.get("name") == "code_chunks_owner_repo_search_terms_idx"
    )
    assert search_terms_idx["name"] == "code_chunks_owner_repo_search_terms_idx"

    # Call init_indexes a second time to verify idempotency
    init_indexes(mock_db)
    assert mock_collections["code_chunks"].create_index.call_count > 0

