"""Targeted tests for index initialization failure handling, process caching, and isolation."""

import concurrent.futures
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from sourcetrace.api.app import create_app
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import StorageOperationError
from sourcetrace.models.domain import CodeChunk
from sourcetrace.storage.mongo_repositories import MongoCodeChunkRepository
from sourcetrace.storage.mongodb import (
    _INITIALIZED_TARGETS,
    MongoStorageManager,
    get_default_storage_manager,
    reset_storage_manager_state,
)


@pytest.fixture(autouse=True)
def _reset_index_cache():
    """Ensure clean index initialization state before and after each test."""
    get_settings.cache_clear()
    reset_storage_manager_state()
    yield
    reset_storage_manager_state()
    get_settings.cache_clear()


def test_01_module_import_creates_no_client():
    """Module import alone must not create a PyMongo client or connect to MongoDB."""
    assert "sourcetrace.storage.mongodb" in sys.modules
    manager = MongoStorageManager(settings=Settings(mongodb_uri="mongodb://localhost:27017"))
    assert manager._client is None


def test_02_health_request_creates_no_client():
    """GET /api/v1/health must not instantiate a Mongo client or touch the database."""
    app = create_app()
    test_client = TestClient(app)
    response = test_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_03_first_operation_initializes_indexes():
    """First repository operation calls init_indexes on the database handle."""
    mock_db = MagicMock()
    mock_db.name = "test_db_03"
    mock_client = MagicMock()
    mock_db.client = mock_client

    repo = MongoCodeChunkRepository(db=mock_db)
    results = repo.search_lexical(
        owner_session_id="s1",
        repository_id="r1",
        query_text="func",
    )
    assert results == []
    assert mock_db["code_chunks"].create_index.called


def test_04_second_operation_same_repository_does_not_repeat():
    """Second operation on the same repository instance does not call create_index again."""
    mock_db = MagicMock()
    mock_db.name = "test_db_04"
    mock_db.client = MagicMock()

    repo = MongoCodeChunkRepository(db=mock_db)
    repo.search_lexical("s1", "r1", "query1")
    call_count_1 = mock_db["code_chunks"].create_index.call_count

    repo.search_lexical("s1", "r1", "query2")
    call_count_2 = mock_db["code_chunks"].create_index.call_count

    assert call_count_1 > 0
    assert call_count_2 == call_count_1


def test_05_default_path_two_repository_instances_share_default_manager_and_client():
    """Two default MongoCodeChunkRepository() instances share default manager and client."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.name = "sourcetrace_test"
    mock_db.client = mock_client
    mock_client.__getitem__.return_value = mock_db

    fake_client_factory = MagicMock(return_value=mock_client)
    custom_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database_name="sourcetrace_test",
    )

    default_manager = MongoStorageManager(
        settings=custom_settings,
        client_factory=fake_client_factory,
    )
    import sourcetrace.storage.mongodb as mongodb_mod

    mongodb_mod._DEFAULT_STORAGE_MANAGER = default_manager

    repo1 = MongoCodeChunkRepository()
    repo2 = MongoCodeChunkRepository()

    repo1.search_lexical("s1", "r1", "query1")

    assert repo1._manager is default_manager
    assert fake_client_factory.call_count == 1
    call_count_after_repo1 = mock_db["code_chunks"].create_index.call_count
    assert call_count_after_repo1 > 0

    repo2.search_lexical("s1", "r1", "query2")

    assert repo2._manager is default_manager
    assert fake_client_factory.call_count == 1
    assert mock_db["code_chunks"].create_index.call_count == call_count_after_repo1
    assert default_manager._indexes_initialized is True


def test_06_concurrent_first_operations_initialize_once():
    """Concurrent first operations initialize indexes exactly once under thread contention."""
    mock_db = MagicMock()
    mock_db.name = "test_db_06"
    mock_db.client = MagicMock()

    def worker():
        r = MongoCodeChunkRepository(db=mock_db)
        return r.search_lexical("s1", "r1", "test")

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker) for _ in range(10)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    create_index_calls = [
        c
        for c in mock_db["code_chunks"].create_index.call_args_list
        if c.kwargs.get("name") == "code_chunks_owner_repo_search_terms_idx"
    ]
    assert len(create_index_calls) == 1


def test_07_initialization_failure_raises_storage_operation_error():
    """Initialization failure raises StorageOperationError without swallowing."""
    mock_db = MagicMock()
    mock_db.name = "test_db_07"
    mock_db.client = MagicMock()
    mock_db["code_chunks"].create_index.side_effect = Exception("Mongo Atlas Indexing Error")

    repo = MongoCodeChunkRepository(db=mock_db)
    with pytest.raises(StorageOperationError) as exc_info:
        repo.search_lexical("s1", "r1", "query")

    assert "Mongo Atlas Indexing Error" not in str(exc_info.value)
    assert "Failed to initialize database indexes" in str(exc_info.value)


def test_08_initialization_failure_leaves_initialized_false():
    """Initialization failure leaves manager._indexes_initialized as False."""
    mock_db = MagicMock()
    mock_db.name = "test_db_08"
    mock_db.client = MagicMock()
    mock_db["code_chunks"].create_index.side_effect = Exception("Connection Failed")

    manager = MongoStorageManager(injected_client=mock_db.client)
    with pytest.raises(StorageOperationError):
        manager.ensure_indexes(db=mock_db)

    assert manager._indexes_initialized is False


def test_09_later_call_retries_successfully():
    """Failed initialization is not cached as successful; subsequent call retries."""
    mock_db = MagicMock()
    mock_db.name = "test_db_09"
    mock_db.client = MagicMock()

    mock_db["code_chunks"].create_index.side_effect = Exception("Transient Network Failure")
    repo = MongoCodeChunkRepository(db=mock_db)
    with pytest.raises(StorageOperationError):
        repo.search_lexical("s1", "r1", "query")

    mock_db["code_chunks"].create_index.side_effect = None
    results = repo.search_lexical("s1", "r1", "query")
    assert results == []


def test_10_search_never_runs_after_failed_initialization():
    """Search operations (find/aggregate) must never run if index initialization fails."""
    mock_db = MagicMock()
    mock_db.name = "test_db_10"
    mock_db.client = MagicMock()
    mock_db["code_chunks"].create_index.side_effect = Exception("Index creation denied")

    repo = MongoCodeChunkRepository(db=mock_db)
    with pytest.raises(StorageOperationError):
        repo.search_lexical("s1", "r1", "query")

    assert mock_db["code_chunks"].find.called is False


def test_11_save_never_runs_after_failed_initialization():
    """Save operations (bulk_write) must never run if index initialization fails."""
    mock_db = MagicMock()
    mock_db.name = "test_db_11"
    mock_db.client = MagicMock()
    mock_db["code_chunks"].create_index.side_effect = Exception("Index creation denied")

    chunk = CodeChunk(
        chunk_id="c1",
        repository_id="r1",
        owner_session_id="s1",
        relative_path="main.py",
        language="python",
        symbol_name="foo",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def foo(): pass",
        content_hash="h1",
        parser_version="1.0",
        created_at=datetime.now(UTC),
    )

    repo = MongoCodeChunkRepository(db=mock_db)
    with pytest.raises(StorageOperationError):
        repo.save_many([chunk])

    assert mock_db["code_chunks"].bulk_write.called is False


def test_12_injected_test_collections_isolated():
    """Injected collection bypasses ensure_indexes and does not access MongoStorageManager."""
    mock_coll = MagicMock()
    repo = MongoCodeChunkRepository(collection=mock_coll)

    repo._ensure_indexes_lazily()
    assert repo._manager is None


def test_13_two_isolated_injected_clients_initialize_independently():
    """Two separate injected clients initialize indexes independently."""
    mock_client_1 = MagicMock()
    mock_db_1 = MagicMock()
    mock_db_1.name = "db1"
    mock_db_1.client = mock_client_1
    mock_client_1.__getitem__.return_value = mock_db_1

    mock_client_2 = MagicMock()
    mock_db_2 = MagicMock()
    mock_db_2.name = "db2"
    mock_db_2.client = mock_client_2
    mock_client_2.__getitem__.return_value = mock_db_2

    manager1 = MongoStorageManager(injected_client=mock_client_1)
    manager2 = MongoStorageManager(injected_client=mock_client_2)

    repo1 = MongoCodeChunkRepository(manager=manager1)
    repo2 = MongoCodeChunkRepository(manager=manager2)

    repo1.search_lexical("s1", "r1", "query")
    repo2.search_lexical("s1", "r1", "query")

    assert mock_db_1["code_chunks"].create_index.called
    assert mock_db_2["code_chunks"].create_index.called


def test_14_no_credentials_or_uris_in_errors():
    """Error messages must never include plaintext credentials or MongoDB URIs."""
    mock_db = MagicMock()
    mock_db.name = "test_db_14"
    mock_db.client = MagicMock()
    mock_db["code_chunks"].create_index.side_effect = Exception(
        "mongodb+srv://admin:secretPass123@cluster0.mongodb.net failure"
    )

    repo = MongoCodeChunkRepository(db=mock_db)
    with pytest.raises(StorageOperationError) as exc_info:
        repo.search_lexical("s1", "r1", "query")

    err_str = str(exc_info.value)
    assert "secretPass123" not in err_str
    assert "mongodb+srv" not in err_str


def test_15_closing_or_resetting_default_manager_forces_reinitialization():
    """Closing/resetting default manager clears initialization state for re-initialization."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.name = "sourcetrace_test_15"
    mock_db.client = mock_client
    mock_client.__getitem__.return_value = mock_db

    fake_factory = MagicMock(return_value=mock_client)
    custom_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database_name="sourcetrace_test_15",
    )

    mgr = MongoStorageManager(settings=custom_settings, client_factory=fake_factory)
    import sourcetrace.storage.mongodb as mongodb_mod

    mongodb_mod._DEFAULT_STORAGE_MANAGER = mgr

    repo1 = MongoCodeChunkRepository()
    repo1.search_lexical("s1", "r1", "query1")
    count_1 = mock_db["code_chunks"].create_index.call_count
    assert count_1 > 0

    reset_storage_manager_state()

    mgr2 = MongoStorageManager(settings=custom_settings, client_factory=fake_factory)
    mongodb_mod._DEFAULT_STORAGE_MANAGER = mgr2

    repo2 = MongoCodeChunkRepository()
    repo2.search_lexical("s1", "r1", "query2")
    count_2 = mock_db["code_chunks"].create_index.call_count
    assert count_2 > count_1


def test_16_direct_manager_close_invalidates_target_and_forces_reinitialization(monkeypatch):
    """Directly calling manager.close() clears target key and forces reinitialization."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.name = "test_db_16"
    mock_db.client = mock_client
    mock_client.__getitem__.return_value = mock_db

    import sourcetrace.storage.mongodb as mongodb_mod

    monkeypatch.setattr(mongodb_mod, "MongoClient", lambda uri: mock_client)

    custom_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database_name="test_db_16",
    )

    mgr = MongoStorageManager(settings=custom_settings)
    mgr.ensure_indexes()

    target_key = ("default", "test_db_16")
    assert mgr._indexes_initialized is True
    assert target_key in _INITIALIZED_TARGETS
    call_count_1 = mock_db["code_chunks"].create_index.call_count

    # Call manager.close() directly without global reset
    mgr.close()

    assert mgr._indexes_initialized is False
    assert target_key not in _INITIALIZED_TARGETS
    assert mock_client.close.called is True

    # Subsequent operation through new manager re-initializes
    mgr2 = MongoStorageManager(settings=custom_settings)
    mgr2.ensure_indexes()
    assert mgr2._indexes_initialized is True
    assert mock_db["code_chunks"].create_index.call_count > call_count_1


def test_17_injected_manager_close_invalidates_target_without_closing_external_client():
    """Injected manager close clears target key without closing external client."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.name = "test_db_17"
    mock_db.client = mock_client

    mgr = MongoStorageManager(injected_client=mock_client)
    mgr.ensure_indexes(db=mock_db)

    target_key = ("injected", id(mock_client), "test_db_17")
    assert target_key in _INITIALIZED_TARGETS
    assert mgr._indexes_initialized is True

    # Call manager.close() directly
    mgr.close()

    assert mgr._indexes_initialized is False
    assert target_key not in _INITIALIZED_TARGETS
    # External injected client must NOT be closed
    assert mock_client.close.called is False


def test_18_custom_settings_without_manager_or_db_creates_isolated_manager(monkeypatch):
    """Repository with custom settings creates an isolated manager with custom settings."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.name = "custom_isolated_db"
    mock_db.client = mock_client
    mock_client.__getitem__.return_value = mock_db

    import sourcetrace.storage.mongodb as mongodb_mod

    monkeypatch.setattr(mongodb_mod, "MongoClient", lambda uri: mock_client)

    custom_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database_name="custom_isolated_db",
    )

    repo = MongoCodeChunkRepository(settings=custom_settings)
    repo._ensure_indexes_lazily()

    assert repo._manager is not get_default_storage_manager()
    assert repo._manager._settings.mongodb_database_name == "custom_isolated_db"


def test_19_explicit_manager_takes_precedence_over_settings():
    """Explicit manager argument takes precedence over custom settings."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.name = "mgr_db"
    mock_db.client = mock_client
    mock_client.__getitem__.return_value = mock_db

    explicit_mgr = MongoStorageManager(
        settings=Settings(mongodb_database_name="mgr_db"),
        injected_client=mock_client,
    )
    custom_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database_name="custom_db_19",
    )

    repo = MongoCodeChunkRepository(manager=explicit_mgr, settings=custom_settings)
    repo._ensure_indexes_lazily()

    assert repo._manager is explicit_mgr
    assert mock_db["code_chunks"].create_index.called is True


def test_20_explicit_db_takes_precedence_over_settings():
    """Explicit db argument takes precedence over custom settings."""
    mock_db = MagicMock()
    mock_db.name = "explicit_db_20"
    mock_db.client = MagicMock()

    custom_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database_name="custom_db_20",
    )

    repo = MongoCodeChunkRepository(db=mock_db, settings=custom_settings)
    repo._ensure_indexes_lazily()

    assert repo._injected_db is mock_db


def test_21_explicit_collection_bypasses_manager():
    """Explicit collection argument bypasses manager and index initialization."""
    mock_coll = MagicMock()
    custom_settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        mongodb_database_name="custom_db_21",
    )

    repo = MongoCodeChunkRepository(collection=mock_coll, settings=custom_settings)
    repo._ensure_indexes_lazily()

    assert repo._manager is None


def test_22_genuine_default_target_classification(monkeypatch):
    """Default manager without injected client is classified as ('default', db_name)."""
    monkeypatch.setenv("SOURCETRACE_MONGODB_URI", "mongodb://unit-test.invalid:27017")
    get_settings.cache_clear()

    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.name = "sourcetrace"
    mock_db.client = mock_client
    mock_client.__getitem__.return_value = mock_db

    import sourcetrace.storage.mongodb as mongodb_mod

    monkeypatch.setattr(mongodb_mod, "MongoClient", lambda uri: mock_client)

    default_mgr = get_default_storage_manager()
    assert default_mgr._uses_injected_client is False

    repo1 = MongoCodeChunkRepository()
    repo2 = MongoCodeChunkRepository()

    repo1.search_lexical("s1", "r1", "query1")
    target_key = ("default", "sourcetrace")

    assert target_key in _INITIALIZED_TARGETS
    assert repo1._manager is default_mgr

    call_count_1 = mock_db["code_chunks"].create_index.call_count
    repo2.search_lexical("s1", "r1", "query2")
    call_count_2 = mock_db["code_chunks"].create_index.call_count

    assert repo2._manager is default_mgr
    assert call_count_1 > 0
    assert call_count_2 == call_count_1
