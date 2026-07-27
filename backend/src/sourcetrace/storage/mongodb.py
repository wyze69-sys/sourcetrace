import threading
from collections.abc import Callable
from typing import Any, ClassVar

from pymongo import MongoClient
from pymongo.database import Database

from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import StorageConfigurationError, StorageOperationError

CANONICAL_COLLECTIONS: tuple[str, ...] = (
    "anonymous_sessions",
    "repositories",
    "indexing_jobs",
    "code_chunks",
    "conversations",
    "messages",
)

_INITIALIZED_TARGETS: set[tuple[str, Any, str]] = set()
_GLOBAL_INIT_LOCK: threading.Lock = threading.Lock()

_DEFAULT_STORAGE_MANAGER: "MongoStorageManager | None" = None
_DEFAULT_MANAGER_LOCK: threading.Lock = threading.Lock()


def get_default_storage_manager() -> "MongoStorageManager":
    """Return the process-wide shared default MongoStorageManager instance."""
    global _DEFAULT_STORAGE_MANAGER
    if _DEFAULT_STORAGE_MANAGER is not None:
        return _DEFAULT_STORAGE_MANAGER

    with _DEFAULT_MANAGER_LOCK:
        if _DEFAULT_STORAGE_MANAGER is None:
            _DEFAULT_STORAGE_MANAGER = MongoStorageManager()
        return _DEFAULT_STORAGE_MANAGER


def reset_storage_manager_state() -> None:
    """Reset default storage manager singleton and process index initialization cache."""
    global _DEFAULT_STORAGE_MANAGER
    with _DEFAULT_MANAGER_LOCK:
        if _DEFAULT_STORAGE_MANAGER is not None:
            _DEFAULT_STORAGE_MANAGER.close()
            _DEFAULT_STORAGE_MANAGER = None
    with _GLOBAL_INIT_LOCK:
        _INITIALIZED_TARGETS.clear()


def clear_initialized_indexes_cache() -> None:
    """Alias for reset_storage_manager_state for test fixture compatibility."""
    reset_storage_manager_state()


class MongoStorageManager:
    """Manages MongoDB client lifecycle and database handle access."""

    COLLECTIONS: ClassVar[tuple[str, ...]] = CANONICAL_COLLECTIONS

    def __init__(
        self,
        settings: Settings | None = None,
        client_factory: Callable[[str], MongoClient] | None = None,
        injected_client: MongoClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client_factory = client_factory or MongoClient
        self._injected_client: MongoClient | None = injected_client
        self._uses_injected_client: bool = (injected_client is not None) or (
            client_factory is not None
        )
        self._client: MongoClient | None = injected_client
        self._owns_client: bool = injected_client is None
        self._indexes_initialized: bool = False
        self._initialized_target_keys: set[tuple[str, Any, str]] = set()
        self._init_lock: threading.Lock = threading.Lock()

    def get_client(self) -> MongoClient:
        """Return connected or injected PyMongo client instance."""
        if self._client is not None:
            return self._client

        if not self._settings.mongodb_uri:
            raise StorageConfigurationError("MongoDB connection URI is not configured.")

        uri_str = self._settings.mongodb_uri.get_secret_value()
        self._client = self._client_factory(uri_str)
        self._owns_client = True
        return self._client

    def get_database(self) -> Database:
        """Return PyMongo database handle for configured database name."""
        client = self.get_client()
        return client[self._settings.mongodb_database_name]

    def close(self) -> None:
        """Close PyMongo client connection if owned and invalidate manager target keys."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

        with _GLOBAL_INIT_LOCK:
            for key in self._initialized_target_keys:
                _INITIALIZED_TARGETS.discard(key)
            self._initialized_target_keys.clear()

        self._indexes_initialized = False

    def ensure_indexes(self, db: Database | None = None) -> None:
        """Lazily initialize database indexes idempotently and thread-safely."""
        target_db = db if db is not None else self.get_database()
        target_key: tuple[str, Any, str]
        if self._uses_injected_client or db is not None:
            target_key = ("injected", id(target_db.client), target_db.name)
        else:
            target_key = ("default", target_db.name)

        if target_key in _INITIALIZED_TARGETS:
            self._indexes_initialized = True
            with _GLOBAL_INIT_LOCK:
                self._initialized_target_keys.add(target_key)
            return

        with _GLOBAL_INIT_LOCK:
            if target_key in _INITIALIZED_TARGETS:
                self._indexes_initialized = True
                self._initialized_target_keys.add(target_key)
                return
            try:
                from sourcetrace.storage.mongo_repositories import init_indexes

                init_indexes(target_db)
                _INITIALIZED_TARGETS.add(target_key)
                self._initialized_target_keys.add(target_key)
                self._indexes_initialized = True
            except (StorageConfigurationError, StorageOperationError):
                self._indexes_initialized = False
                raise
            except Exception:
                self._indexes_initialized = False
                raise StorageOperationError(
                    "Failed to initialize database indexes safely."
                ) from None

    def init_indexes(self) -> None:
        """Explicitly trigger index initialization."""
        self.ensure_indexes()
