"""Concrete PyMongo repository implementations for SourceTrace persistence."""

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReplaceOne, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database

from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import (
    StorageConfigurationError,
    StorageDataError,
    StorageOperationError,
)
from sourcetrace.models.domain import (
    AnonymousSession,
    CitationRecord,
    CodeChunk,
    ConversationRecord,
    EvidenceSnippetRecord,
    IndexingJobRecord,
    MessageRecord,
    RepositoryRecord,
    RetrievalResult,
)
from sourcetrace.storage.mongodb import MongoStorageManager, get_default_storage_manager


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime has timezone-aware UTC info safely."""
    if isinstance(dt, bool) or not isinstance(dt, datetime):
        raise StorageDataError("Invalid created_at timestamp in code chunk.")
    try:
        tz = dt.tzinfo
    except Exception:
        raise StorageDataError("Invalid created_at timestamp in code chunk.") from None

    if tz is None:
        return dt.replace(tzinfo=UTC)
    try:
        return dt.astimezone(UTC)
    except Exception:
        raise StorageDataError("Invalid created_at timestamp in code chunk.") from None


def _to_utc(val: Any) -> datetime:
    """Parse datetime field from BSON document as timezone-aware UTC."""
    if isinstance(val, bool) or not isinstance(val, datetime):
        raise StorageDataError("Invalid or missing datetime field in stored document.")
    try:
        tz = val.tzinfo
    except Exception:
        raise StorageDataError("Invalid datetime field in stored document.") from None

    if tz is None:
        return val.replace(tzinfo=UTC)
    try:
        return val.astimezone(UTC)
    except Exception:
        raise StorageDataError("Invalid datetime field in stored document.") from None


def _validate_content(val: Any) -> str:
    """Validate that source code content is a non-empty string without modifying whitespace."""
    if isinstance(val, bool) or not isinstance(val, str) or not val:
        raise StorageDataError("Invalid or missing content field.")
    return val


def _validate_non_empty_string(val: Any) -> str:
    """Validate that field value is a non-empty string."""
    if isinstance(val, bool) or not isinstance(val, str) or not val.strip():
        raise StorageDataError("Invalid or missing string field in stored document.")
    return val.strip()


def _validate_optional_string(val: Any) -> str | None:
    """Validate that optional field value is None or a string."""
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, str):
        raise StorageDataError("Invalid optional string field in stored document.")
    return val


def _validate_int(val: Any, default: int = 0) -> int:
    """Validate that numeric field value is a real integer."""
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, int):
        raise StorageDataError("Invalid integer field in stored document.")
    return val


def _doc_to_session(doc: dict[str, Any]) -> AnonymousSession:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed anonymous session document in storage.")
    try:
        return AnonymousSession(
            owner_session_id=_validate_non_empty_string(doc.get("owner_session_id")),
            last_active_at=_to_utc(doc.get("last_active_at")),
            expires_at=_to_utc(doc.get("expires_at")),
            created_at=_to_utc(doc.get("created_at")),
            updated_at=_to_utc(doc.get("updated_at")),
            active_repository_count=_validate_int(
                doc.get("active_repository_count"), default=0
            ),
        )
    except StorageDataError:
        raise
    except Exception as exc:
        raise StorageDataError(
            "Malformed anonymous session document in storage."
        ) from exc


def _doc_to_repository(doc: dict[str, Any]) -> RepositoryRecord:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed repository document in storage.")
    try:
        raw_index_mode = doc.get("index_mode")
        if raw_index_mode is None:
            index_mode = "cloud_ai"
        else:
            index_mode = _validate_non_empty_string(raw_index_mode)
            if index_mode not in ("static", "cloud_ai"):
                raise StorageDataError("Invalid index_mode in stored document.")

        return RepositoryRecord(
            repository_id=_validate_non_empty_string(doc.get("repository_id")),
            owner_session_id=_validate_non_empty_string(doc.get("owner_session_id")),
            name=_validate_non_empty_string(doc.get("name")),
            source_type=_validate_non_empty_string(doc.get("source_type")),
            status=_validate_non_empty_string(doc.get("status")),
            created_at=_to_utc(doc.get("created_at")),
            updated_at=_to_utc(doc.get("updated_at")),
            github_url=_validate_optional_string(doc.get("github_url")),
            file_count=_validate_int(doc.get("file_count"), default=0),
            chunk_count=_validate_int(doc.get("chunk_count"), default=0),
            index_mode=index_mode,
        )
    except StorageDataError:
        raise
    except Exception as exc:
        raise StorageDataError("Malformed repository document in storage.") from exc


def _doc_to_job(doc: dict[str, Any]) -> IndexingJobRecord:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed indexing job document in storage.")
    try:
        completed_at_raw = doc.get("completed_at")
        completed_at = (
            _to_utc(completed_at_raw) if completed_at_raw is not None else None
        )
        return IndexingJobRecord(
            job_id=_validate_non_empty_string(doc.get("job_id")),
            repository_id=_validate_non_empty_string(doc.get("repository_id")),
            owner_session_id=_validate_non_empty_string(doc.get("owner_session_id")),
            status=_validate_non_empty_string(doc.get("status")),
            current_step=_validate_non_empty_string(doc.get("current_step")),
            created_at=_to_utc(doc.get("created_at")),
            updated_at=_to_utc(doc.get("updated_at")),
            progress_percentage=_validate_int(
                doc.get("progress_percentage"), default=0
            ),
            error_message=_validate_optional_string(doc.get("error_message")),
            completed_at=completed_at,
        )
    except StorageDataError:
        raise
    except Exception as exc:
        raise StorageDataError("Malformed indexing job document in storage.") from exc


class MongoAnonymousSessionRepository:
    """Concrete PyMongo repository for anonymous session persistence."""

    COLLECTION_NAME = "anonymous_sessions"

    def __init__(
        self,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
        collection: Collection | None = None,
    ) -> None:
        self._injected_db = db
        self._injected_collection = collection
        self._manager = manager

    @property
    def _collection(self) -> Collection:
        if self._injected_collection is not None:
            return self._injected_collection
        if self._injected_db is not None:
            return self._injected_db[self.COLLECTION_NAME]
        if self._manager is None:
            self._manager = get_default_storage_manager()
        return self._manager.get_database()[self.COLLECTION_NAME]

    def get_by_id(self, owner_session_id: str) -> AnonymousSession | None:
        query_filter = {"owner_session_id": owner_session_id}
        doc = self._collection.find_one(query_filter)
        if doc is None:
            return None
        return _doc_to_session(doc)

    def save(self, session: AnonymousSession) -> AnonymousSession:
        query_filter = {"owner_session_id": session.owner_session_id}
        doc = {
            "owner_session_id": session.owner_session_id,
            "last_active_at": _ensure_utc(session.last_active_at),
            "expires_at": _ensure_utc(session.expires_at),
            "created_at": _ensure_utc(session.created_at),
            "updated_at": _ensure_utc(session.updated_at),
            "active_repository_count": session.active_repository_count,
        }
        self._collection.replace_one(query_filter, doc, upsert=True)
        return session

    def delete(self, owner_session_id: str) -> bool:
        query_filter = {"owner_session_id": owner_session_id}
        result = self._collection.delete_one(query_filter)
        return result.deleted_count > 0

    def reserve_repository_slot(
        self,
        owner_session_id: str,
        now: datetime,
        max_quota: int = 3,
        retention_days: int = 7,
    ) -> AnonymousSession | None:
        now_utc = _ensure_utc(now)
        expires_at = now_utc + timedelta(days=retention_days)
        query_filter = {
            "owner_session_id": owner_session_id,
            "expires_at": {"$gt": now_utc},
            "$or": [
                {"active_repository_count": {"$lt": max_quota}},
                {"active_repository_count": {"$exists": False}},
            ],
        }
        update_doc = {
            "$inc": {"active_repository_count": 1},
            "$set": {
                "last_active_at": now_utc,
                "expires_at": expires_at,
                "updated_at": now_utc,
            },
        }
        doc = self._collection.find_one_and_update(
            query_filter,
            update_doc,
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return None
        return _doc_to_session(doc)

    def release_repository_slot(self, owner_session_id: str) -> bool:
        query_filter = {
            "owner_session_id": owner_session_id,
            "active_repository_count": {"$gt": 0},
        }
        update_doc = {
            "$inc": {"active_repository_count": -1},
        }
        doc = self._collection.find_one_and_update(
            query_filter,
            update_doc,
            return_document=ReturnDocument.AFTER,
        )
        return doc is not None


class MongoRepositoryRepository:
    """Concrete PyMongo repository for repository metadata persistence."""

    COLLECTION_NAME = "repositories"

    def __init__(
        self,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
        collection: Collection | None = None,
    ) -> None:
        self._injected_db = db
        self._injected_collection = collection
        self._manager = manager

    @property
    def _collection(self) -> Collection:
        if self._injected_collection is not None:
            return self._injected_collection
        if self._injected_db is not None:
            return self._injected_db[self.COLLECTION_NAME]
        if self._manager is None:
            self._manager = get_default_storage_manager()
        return self._manager.get_database()[self.COLLECTION_NAME]

    def get_by_id(
        self, owner_session_id: str, repository_id: str
    ) -> RepositoryRecord | None:
        query_filter = {
            "owner_session_id": owner_session_id,
            "repository_id": repository_id,
        }
        doc = self._collection.find_one(query_filter)
        if doc is None:
            return None
        return _doc_to_repository(doc)

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        query_filter = {"owner_session_id": owner_session_id}
        cursor = self._collection.find(query_filter).sort("created_at", -1)
        return [_doc_to_repository(doc) for doc in cursor]

    def count_by_owner(self, owner_session_id: str) -> int:
        query_filter = {"owner_session_id": owner_session_id}
        return int(self._collection.count_documents(query_filter))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        if repository.index_mode not in ("static", "cloud_ai"):
            raise StorageDataError("Invalid index_mode in RepositoryRecord.")

        query_filter = {
            "owner_session_id": repository.owner_session_id,
            "repository_id": repository.repository_id,
        }
        doc = {
            "repository_id": repository.repository_id,
            "owner_session_id": repository.owner_session_id,
            "name": repository.name,
            "source_type": repository.source_type,
            "status": repository.status,
            "created_at": _ensure_utc(repository.created_at),
            "updated_at": _ensure_utc(repository.updated_at),
            "github_url": repository.github_url,
            "file_count": repository.file_count,
            "chunk_count": repository.chunk_count,
            "index_mode": repository.index_mode,
        }
        self._collection.replace_one(query_filter, doc, upsert=True)
        return repository

    def transition_status(
        self,
        owner_session_id: str,
        repository_id: str,
        expected_status: str | tuple[str, ...],
        new_status: str,
        updated_at: datetime,
        file_count: int | None = None,
        chunk_count: int | None = None,
    ) -> RepositoryRecord | None:
        expected_filter: Any
        if isinstance(expected_status, (tuple, list, set)):
            expected_filter = {"$in": list(expected_status)}
        else:
            expected_filter = expected_status

        query_filter = {
            "owner_session_id": owner_session_id,
            "repository_id": repository_id,
            "status": expected_filter,
        }
        set_doc: dict[str, Any] = {
            "status": new_status,
            "updated_at": _ensure_utc(updated_at),
        }
        if file_count is not None:
            set_doc["file_count"] = file_count
        if chunk_count is not None:
            set_doc["chunk_count"] = chunk_count

        doc = self._collection.find_one_and_update(
            query_filter,
            {"$set": set_doc},
            return_document=ReturnDocument.AFTER,
            upsert=False,
        )
        if doc is None:
            return None
        return _doc_to_repository(doc)

    def delete(self, owner_session_id: str, repository_id: str) -> bool:

        query_filter = {
            "owner_session_id": owner_session_id,
            "repository_id": repository_id,
        }
        result = self._collection.delete_one(query_filter)
        return result.deleted_count > 0


class MongoIndexingJobRepository:
    """Concrete PyMongo repository for indexing job status persistence."""

    COLLECTION_NAME = "indexing_jobs"

    def __init__(
        self,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
        collection: Collection | None = None,
    ) -> None:
        self._injected_db = db
        self._injected_collection = collection
        self._manager = manager

    @property
    def _collection(self) -> Collection:
        if self._injected_collection is not None:
            return self._injected_collection
        if self._injected_db is not None:
            return self._injected_db[self.COLLECTION_NAME]
        if self._manager is None:
            self._manager = get_default_storage_manager()
        return self._manager.get_database()[self.COLLECTION_NAME]

    def get_by_id(
        self, owner_session_id: str, job_id: str
    ) -> IndexingJobRecord | None:
        query_filter = {
            "owner_session_id": owner_session_id,
            "job_id": job_id,
        }
        doc = self._collection.find_one(query_filter)
        if doc is None:
            return None
        return _doc_to_job(doc)

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        query_filter = {
            "owner_session_id": owner_session_id,
            "repository_id": repository_id,
        }
        doc = self._collection.find_one(query_filter)
        if doc is None:
            return None
        return _doc_to_job(doc)

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        query_filter = {
            "owner_session_id": job.owner_session_id,
            "job_id": job.job_id,
        }
        completed_at = (
            _ensure_utc(job.completed_at) if job.completed_at is not None else None
        )
        doc = {
            "job_id": job.job_id,
            "repository_id": job.repository_id,
            "owner_session_id": job.owner_session_id,
            "status": job.status,
            "current_step": job.current_step,
            "created_at": _ensure_utc(job.created_at),
            "updated_at": _ensure_utc(job.updated_at),
            "progress_percentage": job.progress_percentage,
            "error_message": job.error_message,
            "completed_at": completed_at,
        }
        self._collection.replace_one(query_filter, doc, upsert=True)
        return job

    def transition_status(
        self,
        owner_session_id: str,
        job_id: str,
        repository_id: str,
        expected_status: str | tuple[str, ...],
        new_status: str,
        current_step: str,
        progress_percentage: int | None,
        updated_at: datetime,
        error_message: str | None = None,
        completed_at: datetime | None = None,
    ) -> IndexingJobRecord | None:
        expected_filter: Any
        if isinstance(expected_status, (tuple, list, set)):
            expected_filter = {"$in": list(expected_status)}
        else:
            expected_filter = expected_status

        query_filter = {
            "owner_session_id": owner_session_id,
            "job_id": job_id,
            "repository_id": repository_id,
            "status": expected_filter,
        }
        set_doc: dict[str, Any] = {
            "status": new_status,
            "current_step": current_step,
            "updated_at": _ensure_utc(updated_at),
        }
        if progress_percentage is not None:
            if (
                type(progress_percentage) is not int
                or isinstance(progress_percentage, bool)
                or not (0 <= progress_percentage <= 100)
            ):
                raise StorageDataError("Invalid progress percentage.")
            set_doc["progress_percentage"] = progress_percentage

        if completed_at is not None:
            set_doc["completed_at"] = _ensure_utc(completed_at)
        if error_message is not None:
            set_doc["error_message"] = error_message

        doc = self._collection.find_one_and_update(
            query_filter,
            {"$set": set_doc},
            return_document=ReturnDocument.AFTER,
            upsert=False,
        )
        if doc is None:
            return None
        return _doc_to_job(doc)

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:

        query_filter = {
            "owner_session_id": owner_session_id,
            "repository_id": repository_id,
        }
        result = self._collection.delete_many(query_filter)
        return int(result.deleted_count)


def build_vector_search_index_definition(
    dimensions: int,
    index_name: str = "code_chunks_vector_index",
) -> dict[str, Any]:
    """Build a deterministic MongoDB Atlas Vector Search index definition dictionary."""
    if (
        isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or dimensions <= 0
    ):
        raise StorageDataError("Invalid or unconfigured vector search dimensions.")

    if (
        isinstance(index_name, bool)
        or not isinstance(index_name, str)
        or not index_name.strip()
    ):
        raise StorageDataError("Invalid vector search index name.")

    return {
        "name": index_name.strip(),
        "type": "vectorSearch",
        "definition": {
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": dimensions,
                    "similarity": "cosine",
                },
                {
                    "type": "filter",
                    "path": "owner_session_id",
                },
                {
                    "type": "filter",
                    "path": "repository_id",
                },
            ]
        },
    }


_CAMEL_SPLIT_REGEX = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_CLEAN_REGEX = re.compile(r"[^a-zA-Z0-9]+")


def tokenize_identifier(text: str) -> list[str]:
    """Deterministically tokenize code identifiers (camelCase, PascalCase, etc.)."""
    if not text or not isinstance(text, str):
        return []

    cleaned = _TOKEN_CLEAN_REGEX.sub(" ", text)
    tokens: list[str] = []
    for raw_tok in cleaned.split():
        sub_tokens = _CAMEL_SPLIT_REGEX.split(raw_tok)
        for st in sub_tokens:
            st_lower = st.lower().strip()
            if len(st_lower) >= 2:
                tokens.append(st_lower)
    return tokens


def derive_chunk_search_metadata(
    symbol_name: str,
    relative_path: str,
    content: str | None = None,
) -> dict[str, Any]:
    """Derive deterministic normalized search fields for static/lexical indexing."""
    sym_tokens = tokenize_identifier(symbol_name)
    path_tokens = tokenize_identifier(relative_path)

    sym_norm = " ".join(sym_tokens)[:200]
    path_norm = " ".join(path_tokens)[:300]

    all_terms_set: set[str] = set()
    all_terms_set.update(sym_tokens)
    all_terms_set.update(path_tokens)

    if content and isinstance(content, str):
        first_few_lines = content[:500]
        content_tokens = tokenize_identifier(first_few_lines)
        for ct in content_tokens:
            if len(all_terms_set) >= 50:
                break
            all_terms_set.add(ct)

    search_terms = tuple(sorted(list(all_terms_set))[:50])
    search_text = f"{sym_norm} {path_norm} {' '.join(search_terms)}"[:1000]

    return {
        "symbol_name_normalized": sym_norm,
        "relative_path_normalized": path_norm,
        "search_terms": search_terms,
        "search_text": search_text,
    }


def _doc_to_chunk(doc: dict[str, Any]) -> CodeChunk:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed code chunk document in storage.")

    try:
        chunk_id = _validate_non_empty_string(doc.get("chunk_id"))
        repository_id = _validate_non_empty_string(doc.get("repository_id"))
        owner_session_id = _validate_non_empty_string(doc.get("owner_session_id"))
        relative_path = _validate_non_empty_string(doc.get("relative_path"))
        language = _validate_non_empty_string(doc.get("language"))
        symbol_name = _validate_non_empty_string(doc.get("symbol_name"))
        symbol_type = _validate_non_empty_string(doc.get("symbol_type"))

        start_line = _validate_int(doc.get("start_line"))
        if start_line < 1:
            raise StorageDataError("start_line must be at least 1.")

        end_line = _validate_int(doc.get("end_line"))
        if end_line < start_line:
            raise StorageDataError("end_line must be greater than or equal to start_line.")

        content = _validate_content(doc.get("content"))
        content_hash = _validate_non_empty_string(doc.get("content_hash"))
        parser_version = _validate_non_empty_string(doc.get("parser_version"))

        raw_model = doc.get("embedding_model")
        raw_dim = doc.get("embedding_dimensions")
        raw_vec = doc.get("embedding")

        embedding_model: str | None = None
        embedding_dimensions: int | None = None
        embedding: tuple[float, ...] | None = None

        if raw_vec is None and raw_model is None and raw_dim is None:
            # Static chunk with no embedding
            pass
        elif raw_vec is not None and raw_model is not None and raw_dim is not None:
            # Semantic chunk with full embedding
            embedding_model = _validate_non_empty_string(raw_model)
            embedding_dimensions = _validate_int(raw_dim)
            if embedding_dimensions <= 0:
                raise StorageDataError("embedding_dimensions must be positive.")
            if not isinstance(raw_vec, (list, tuple)) or len(raw_vec) != embedding_dimensions:
                raise StorageDataError("Embedding length mismatch.")
            float_vec: list[float] = []
            for val in raw_vec:
                if (
                    val is None
                    or isinstance(val, bool)
                    or not isinstance(val, (int, float))
                    or math.isnan(val)
                    or math.isinf(val)
                ):
                    raise StorageDataError("Invalid vector element.")
                float_vec.append(float(val))
            embedding = tuple(float_vec)
        else:
            raise StorageDataError("Inconsistent semantic embedding metadata state.")

        created_at = _to_utc(doc.get("created_at"))

        sym_norm = str(doc.get("symbol_name_normalized") or "")
        path_norm = str(doc.get("relative_path_normalized") or "")
        raw_terms = doc.get("search_terms")
        search_terms: tuple[str, ...]
        if isinstance(raw_terms, (list, tuple)):
            search_terms = tuple(str(t) for t in raw_terms)
        else:
            search_terms = ()
        search_text = str(doc.get("search_text") or "")

        if not sym_norm or not path_norm or not search_terms:
            derived = derive_chunk_search_metadata(symbol_name, relative_path, content)
            sym_norm = derived["symbol_name_normalized"]
            path_norm = derived["relative_path_normalized"]
            search_terms = derived["search_terms"]
            search_text = derived["search_text"]

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
            created_at=created_at,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            embedding=embedding,
            symbol_name_normalized=sym_norm,
            relative_path_normalized=path_norm,
            search_terms=search_terms,
            search_text=search_text,
        )
    except StorageDataError:
        raise
    except Exception as exc:
        raise StorageDataError("Malformed code chunk document in storage.") from exc


def _chunk_to_doc(chunk: CodeChunk) -> dict[str, Any]:
    if not isinstance(chunk, CodeChunk):
        raise StorageDataError("Invalid CodeChunk instance.")

    _validate_non_empty_string(chunk.chunk_id)
    _validate_non_empty_string(chunk.repository_id)
    _validate_non_empty_string(chunk.owner_session_id)
    _validate_non_empty_string(chunk.relative_path)
    _validate_non_empty_string(chunk.language)
    _validate_non_empty_string(chunk.symbol_name)
    _validate_non_empty_string(chunk.symbol_type)

    if (
        isinstance(chunk.start_line, bool)
        or not isinstance(chunk.start_line, int)
        or chunk.start_line < 1
    ):
        raise StorageDataError("start_line must be at least 1.")

    if (
        isinstance(chunk.end_line, bool)
        or not isinstance(chunk.end_line, int)
        or chunk.end_line < chunk.start_line
    ):
        raise StorageDataError("end_line must be >= start_line.")

    _validate_content(chunk.content)
    _validate_non_empty_string(chunk.content_hash)
    _validate_non_empty_string(chunk.parser_version)

    doc: dict[str, Any] = {
        "chunk_id": chunk.chunk_id.strip(),
        "repository_id": chunk.repository_id.strip(),
        "owner_session_id": chunk.owner_session_id.strip(),
        "relative_path": chunk.relative_path.strip(),
        "language": chunk.language.strip(),
        "symbol_name": chunk.symbol_name.strip(),
        "symbol_type": chunk.symbol_type.strip(),
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "content": chunk.content,
        "content_hash": chunk.content_hash.strip(),
        "parser_version": chunk.parser_version.strip(),
        "created_at": _ensure_utc(chunk.created_at),
    }

    if chunk.embedding is None:
        if chunk.embedding_model is not None or chunk.embedding_dimensions is not None:
            raise StorageDataError("Static chunk cannot have embedding metadata.")
        doc["embedding_model"] = None
        doc["embedding_dimensions"] = None
    else:
        _validate_non_empty_string(chunk.embedding_model)
        if (
            isinstance(chunk.embedding_dimensions, bool)
            or not isinstance(chunk.embedding_dimensions, int)
            or chunk.embedding_dimensions <= 0
        ):
            raise StorageDataError("embedding_dimensions must be positive.")

        if (
            not isinstance(chunk.embedding, tuple)
            or len(chunk.embedding) != chunk.embedding_dimensions
        ):
            raise StorageDataError("Embedding length mismatch.")

        float_vec: list[float] = []
        for val in chunk.embedding:
            if (
                val is None
                or isinstance(val, bool)
                or not isinstance(val, (int, float))
                or math.isnan(val)
                or math.isinf(val)
            ):
                raise StorageDataError("Invalid vector element.")
            float_vec.append(float(val))

        doc["embedding_model"] = chunk.embedding_model.strip()
        doc["embedding_dimensions"] = chunk.embedding_dimensions
        doc["embedding"] = float_vec

    derived = derive_chunk_search_metadata(
        chunk.symbol_name, chunk.relative_path, chunk.content
    )
    doc["symbol_name_normalized"] = (
        chunk.symbol_name_normalized or derived["symbol_name_normalized"]
    )
    doc["relative_path_normalized"] = (
        chunk.relative_path_normalized or derived["relative_path_normalized"]
    )
    doc["search_terms"] = list(chunk.search_terms or derived["search_terms"])
    doc["search_text"] = chunk.search_text or derived["search_text"]

    return doc


class MongoCodeChunkRepository:
    """Concrete PyMongo repository for code chunk vector persistence and Atlas Vector Search."""

    COLLECTION_NAME = "code_chunks"

    def __init__(
        self,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
        collection: Collection | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._injected_db = db
        self._injected_collection = collection
        self._manager = manager
        self._has_explicit_settings = settings is not None
        self._settings = settings or get_settings()

    def _ensure_indexes_lazily(self) -> None:
        if self._injected_collection is not None:
            return
        if self._manager is None:
            if self._injected_db is not None:
                self._manager = MongoStorageManager(
                    settings=self._settings, injected_client=self._injected_db.client
                )
            elif self._has_explicit_settings:
                self._manager = MongoStorageManager(settings=self._settings)
            else:
                self._manager = get_default_storage_manager()
        try:
            self._manager.ensure_indexes(db=self._injected_db)
        except (StorageConfigurationError, StorageOperationError):
            raise
        except Exception:
            raise StorageOperationError("Failed to initialize database indexes safely.") from None

    @property
    def _collection(self) -> Collection:
        self._ensure_indexes_lazily()
        if self._injected_collection is not None:
            return self._injected_collection
        if self._injected_db is not None:
            return self._injected_db[self.COLLECTION_NAME]
        if self._manager is None:
            if self._has_explicit_settings:
                self._manager = MongoStorageManager(settings=self._settings)
            else:
                self._manager = get_default_storage_manager()
        return self._manager.get_database()[self.COLLECTION_NAME]

    def save_many(self, chunks: list[CodeChunk]) -> int:
        self._ensure_indexes_lazily()
        if isinstance(chunks, (str, bytes, bytearray)) or not isinstance(
            chunks, (list, tuple)
        ):
            raise StorageDataError("Invalid chunk collection container.")

        if not chunks:
            return 0

        first = chunks[0]
        if not isinstance(first, CodeChunk):
            raise StorageDataError("Invalid CodeChunk record in batch.")

        owner_id = _validate_non_empty_string(first.owner_session_id)
        repo_id = _validate_non_empty_string(first.repository_id)
        model_id = first.embedding_model
        dimensions = first.embedding_dimensions

        if model_id is None:
            if dimensions is not None or first.embedding is not None:
                raise StorageDataError("Static chunk state mismatch.")
        else:
            _validate_non_empty_string(model_id)
            if (
                isinstance(dimensions, bool)
                or not isinstance(dimensions, int)
                or dimensions <= 0
            ):
                raise StorageDataError("Invalid embedding_dimensions in batch.")

        seen_chunk_ids: set[str] = set()
        bulk_requests: list[ReplaceOne] = []

        for chunk in chunks:
            if (
                not isinstance(chunk, CodeChunk)
                or chunk.owner_session_id != owner_id
                or chunk.repository_id != repo_id
                or chunk.embedding_model != model_id
                or chunk.embedding_dimensions != dimensions
            ):
                raise StorageDataError(
                    "Mismatched owner, repository, model, or dimensions in batch."
                )

            _validate_non_empty_string(chunk.chunk_id)
            if chunk.chunk_id in seen_chunk_ids:
                raise StorageDataError("Duplicate chunk_id in batch.")
            seen_chunk_ids.add(chunk.chunk_id)

            doc = _chunk_to_doc(chunk)
            query_filter = {
                "owner_session_id": owner_id,
                "repository_id": repo_id,
                "chunk_id": chunk.chunk_id,
            }
            bulk_requests.append(ReplaceOne(query_filter, doc, upsert=True))

        try:
            self._collection.bulk_write(bulk_requests, ordered=True)
        except StorageDataError:
            raise
        except Exception:
            raise StorageOperationError(
                "Database batch save operation failed safely."
            ) from None

        return len(chunks)

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[CodeChunk]:
        self._ensure_indexes_lazily()
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
        }
        sort_spec = [
            ("relative_path", 1),
            ("start_line", 1),
            ("end_line", 1),
            ("chunk_id", 1),
        ]

        try:
            cursor = self._collection.find(query_filter).sort(sort_spec)
            return [_doc_to_chunk(doc) for doc in cursor]
        except Exception:
            raise StorageOperationError(
                "Database query operation failed safely."
            ) from None

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:
        self._ensure_indexes_lazily()
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
        }

        try:
            result = self._collection.delete_many(query_filter)
            return result.deleted_count
        except Exception:
            raise StorageOperationError(
                "Database delete operation failed safely."
            ) from None

    def search_vectors(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: list[float],
        limit: int = 5,
    ) -> list[RetrievalResult]:
        self._ensure_indexes_lazily()
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)

        max_limit_cfg = self._settings.vector_search_max_limit
        if (
            isinstance(max_limit_cfg, bool)
            or not isinstance(max_limit_cfg, int)
            or max_limit_cfg <= 0
        ):
            raise StorageConfigurationError(
                "Invalid vector search max limit configuration."
            )

        expected_dim = self._settings.embedding_dimensions
        if (
            isinstance(expected_dim, bool)
            or not isinstance(expected_dim, int)
            or expected_dim <= 0
        ):
            raise StorageConfigurationError(
                "Invalid vector search dimensions configuration."
            )

        configured_num_candidates = self._settings.vector_search_num_candidates
        if (
            isinstance(configured_num_candidates, bool)
            or not isinstance(configured_num_candidates, int)
            or configured_num_candidates <= 0
        ):
            raise StorageConfigurationError(
                "Invalid vector search numCandidates configuration."
            )

        index_name = self._settings.vector_index_name
        if (
            isinstance(index_name, bool)
            or not isinstance(index_name, str)
            or not index_name.strip()
        ):
            raise StorageConfigurationError(
                "Invalid vector search index configuration."
            )

        # Validate request input
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > max_limit_cfg
        ):
            raise StorageDataError("Invalid or excessive vector search result limit.")

        if (
            isinstance(query_vector, (str, bytes, bytearray))
            or not isinstance(query_vector, (list, tuple))
            or not query_vector
            or len(query_vector) != expected_dim
        ):
            raise StorageDataError("Query vector dimension mismatch or invalid input.")

        import math
        float_query: list[float] = []
        for val in query_vector:
            if (
                val is None
                or isinstance(val, bool)
                or not isinstance(val, (int, float))
                or math.isnan(val)
                or math.isinf(val)
            ):
                raise StorageDataError("Invalid query vector numeric value.")
            float_query.append(float(val))

        effective_num_candidates = max(configured_num_candidates, limit)

        pipeline: list[dict[str, Any]] = [
            {
                "$vectorSearch": {
                    "index": index_name,
                    "path": "embedding",
                    "queryVector": float_query,
                    "numCandidates": effective_num_candidates,
                    "limit": limit,
                    "filter": {
                        "owner_session_id": {"$eq": owner_id},
                        "repository_id": {"$eq": repo_id},
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "chunk_id": 1,
                    "repository_id": 1,
                    "owner_session_id": 1,
                    "relative_path": 1,
                    "language": 1,
                    "symbol_name": 1,
                    "symbol_type": 1,
                    "start_line": 1,
                    "end_line": 1,
                    "content": 1,
                    "content_hash": 1,
                    "parser_version": 1,
                    "embedding_model": 1,
                    "embedding_dimensions": 1,
                    "created_at": 1,
                    "embedding": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]

        try:
            raw_results = list(self._collection.aggregate(pipeline))
        except StorageDataError:
            raise
        except Exception:
            raise StorageOperationError(
                "Database vector search operation failed safely."
            ) from None

        results: list[RetrievalResult] = []
        for doc in raw_results:
            if not isinstance(doc, dict):
                raise StorageDataError("Invalid vector search result document container.")

            try:
                raw_score = doc.get("score")
            except Exception:
                raise StorageDataError("Invalid vector search score in result.") from None

            if (
                raw_score is None
                or isinstance(raw_score, bool)
                or not isinstance(raw_score, (int, float))
                or math.isnan(raw_score)
                or math.isinf(raw_score)
            ):
                raise StorageDataError("Invalid vector search score in result.")

            chunk = _doc_to_chunk(doc)
            results.append(RetrievalResult(chunk=chunk, score=float(raw_score)))

        return results

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
    ) -> list[RetrievalResult]:
        self._ensure_indexes_lazily()
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)

        if not isinstance(query_text, str):
            raise StorageDataError("Query text must be a string.")

        raw_query = query_text.strip()
        if not raw_query:
            return []

        if len(query_text) > 200:
            raise StorageDataError("Query exceeds maximum allowed length of 200 characters.")

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 50:
            raise StorageDataError("Limit must be an integer between 1 and 50.")

        query_tokens = tokenize_identifier(raw_query)[:10]
        if not query_tokens:
            return []

        query_clean = " ".join(query_tokens).lower()
        query_token_set = set(query_tokens)

        base_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
        }

        candidates_by_id: dict[str, dict] = {}
        stage_limit = max(limit * 20, 100)

        # Stage A: Exact symbol match
        try:
            filter_a = {**base_filter, "symbol_name_normalized": query_clean}
            for doc in self._collection.find(filter_a).limit(stage_limit):
                if isinstance(doc, dict) and "chunk_id" in doc:
                    candidates_by_id[doc["chunk_id"]] = doc
        except Exception as exc:
            raise StorageOperationError("Database search operation failed safely.") from exc

        # Stage B: Exact path match
        try:
            filter_b = {**base_filter, "relative_path_normalized": query_clean}
            for doc in self._collection.find(filter_b).limit(stage_limit):
                if isinstance(doc, dict) and "chunk_id" in doc:
                    candidates_by_id[doc["chunk_id"]] = doc
        except Exception as exc:
            raise StorageOperationError("Database search operation failed safely.") from exc

        # Stage C: All-token search_terms match ($all)
        try:
            filter_c = {**base_filter, "search_terms": {"$all": query_tokens}}
            for doc in self._collection.find(filter_c).limit(stage_limit):
                if isinstance(doc, dict) and "chunk_id" in doc:
                    candidates_by_id[doc["chunk_id"]] = doc
        except Exception as exc:
            raise StorageOperationError("Database search operation failed safely.") from exc

        # Stage D: Bounded fallback search_terms match ($in) if candidate pool is small
        if len(candidates_by_id) < limit * 10:
            try:
                filter_d = {**base_filter, "search_terms": {"$in": query_tokens}}
                for doc in self._collection.find(filter_d).limit(stage_limit):
                    if isinstance(doc, dict) and "chunk_id" in doc:
                        candidates_by_id[doc["chunk_id"]] = doc
                        if len(candidates_by_id) >= 500:
                            break
            except Exception as exc:
                raise StorageOperationError("Database search operation failed safely.") from exc

        results: list[RetrievalResult] = []

        for doc in candidates_by_id.values():
            try:
                chunk = _doc_to_chunk(doc)
            except Exception:
                continue

            sym_tokens = set(tokenize_identifier(chunk.symbol_name))
            path_tokens = set(tokenize_identifier(chunk.relative_path))
            sym_norm = chunk.symbol_name_normalized or " ".join(sym_tokens)
            path_norm = chunk.relative_path_normalized or " ".join(path_tokens)
            doc_search_terms = set(doc.get("search_terms") or [])

            score = 0.45
            if sym_norm and (
                sym_norm == query_clean or chunk.symbol_name.strip().lower() == raw_query.lower()
            ):
                score = 1.0
            elif path_norm and (
                path_norm == query_clean
                or chunk.relative_path.strip().lower() == raw_query.lower()
            ):
                score = 0.95
            elif query_token_set.issubset(sym_tokens):
                score = 0.85
            elif query_token_set.issubset(path_tokens):
                score = 0.80
            elif query_token_set.issubset(doc_search_terms):
                score = 0.75
            elif sym_tokens.intersection(query_token_set):
                score = 0.65
            elif path_tokens.intersection(query_token_set):
                score = 0.55
            elif doc_search_terms.intersection(query_token_set):
                score = 0.45

            results.append(RetrievalResult(chunk=chunk, score=score))

        results.sort(
            key=lambda r: (
                -r.score,
                r.chunk.relative_path,
                r.chunk.start_line,
                r.chunk.chunk_id,
            )
        )

        return results[:limit]



def _doc_to_conversation(doc: dict[str, Any]) -> ConversationRecord:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed conversation document in storage.")
    try:
        return ConversationRecord(
            conversation_id=_validate_non_empty_string(doc.get("conversation_id")),
            repository_id=_validate_non_empty_string(doc.get("repository_id")),
            owner_session_id=_validate_non_empty_string(doc.get("owner_session_id")),
            title=_validate_non_empty_string(doc.get("title")),
            created_at=_to_utc(doc.get("created_at")),
            updated_at=_to_utc(doc.get("updated_at")),
        )
    except StorageDataError:
        raise
    except Exception as exc:
        raise StorageDataError("Malformed conversation document in storage.") from exc


def _doc_to_citation(doc: dict[str, Any]) -> CitationRecord:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed citation document.")
    start_line = _validate_int(doc.get("start_line"))
    end_line = _validate_int(doc.get("end_line"))
    if start_line < 1 or end_line < start_line:
        raise StorageDataError("Invalid line numbers in citation.")
    return CitationRecord(
        relative_path=_validate_non_empty_string(doc.get("relative_path")),
        start_line=start_line,
        end_line=end_line,
        symbol_name=_validate_non_empty_string(doc.get("symbol_name")),
        symbol_type=_validate_non_empty_string(doc.get("symbol_type")),
    )


def _citation_to_doc(cit: CitationRecord) -> dict[str, Any]:
    if not isinstance(cit, CitationRecord):
        raise StorageDataError("Invalid CitationRecord instance.")
    if cit.start_line < 1 or cit.end_line < cit.start_line:
        raise StorageDataError("Invalid line numbers in citation.")
    return {
        "relative_path": _validate_non_empty_string(cit.relative_path),
        "start_line": cit.start_line,
        "end_line": cit.end_line,
        "symbol_name": _validate_non_empty_string(cit.symbol_name),
        "symbol_type": _validate_non_empty_string(cit.symbol_type),
    }


def _doc_to_evidence(doc: dict[str, Any]) -> EvidenceSnippetRecord:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed evidence document.")
    start_line = _validate_int(doc.get("start_line"))
    end_line = _validate_int(doc.get("end_line"))
    if start_line < 1 or end_line < start_line:
        raise StorageDataError("Invalid line numbers in evidence.")
    return EvidenceSnippetRecord(
        snippet=_validate_content(doc.get("snippet")),
        relative_path=_validate_non_empty_string(doc.get("relative_path")),
        start_line=start_line,
        end_line=end_line,
        symbol_name=_validate_non_empty_string(doc.get("symbol_name")),
        symbol_type=_validate_non_empty_string(doc.get("symbol_type")),
    )


def _evidence_to_doc(evi: EvidenceSnippetRecord) -> dict[str, Any]:
    if not isinstance(evi, EvidenceSnippetRecord):
        raise StorageDataError("Invalid EvidenceSnippetRecord instance.")
    if evi.start_line < 1 or evi.end_line < evi.start_line:
        raise StorageDataError("Invalid line numbers in evidence.")
    return {
        "snippet": _validate_content(evi.snippet),
        "relative_path": _validate_non_empty_string(evi.relative_path),
        "start_line": evi.start_line,
        "end_line": evi.end_line,
        "symbol_name": _validate_non_empty_string(evi.symbol_name),
        "symbol_type": _validate_non_empty_string(evi.symbol_type),
    }


def _doc_to_message(doc: dict[str, Any]) -> MessageRecord:
    if not isinstance(doc, dict):
        raise StorageDataError("Malformed message document in storage.")
    try:
        role = _validate_non_empty_string(doc.get("role"))
        if role not in ("user", "assistant"):
            raise StorageDataError("Invalid role in message document.")

        raw_citations = doc.get("citations", [])
        if not isinstance(raw_citations, (list, tuple)):
            raise StorageDataError("Malformed citations list.")
        citations = tuple(_doc_to_citation(c) for c in raw_citations)

        raw_evidence = doc.get("evidence", [])
        if not isinstance(raw_evidence, (list, tuple)):
            raise StorageDataError("Malformed evidence list.")
        evidence = tuple(_doc_to_evidence(e) for e in raw_evidence)

        insufficient_evidence = doc.get("insufficient_evidence", False)
        if not isinstance(insufficient_evidence, bool):
            raise StorageDataError("Malformed insufficient_evidence flag.")

        return MessageRecord(
            message_id=_validate_non_empty_string(doc.get("message_id")),
            conversation_id=_validate_non_empty_string(doc.get("conversation_id")),
            repository_id=_validate_non_empty_string(doc.get("repository_id")),
            owner_session_id=_validate_non_empty_string(doc.get("owner_session_id")),
            role=role,
            content=_validate_content(doc.get("content")),
            created_at=_to_utc(doc.get("created_at")),
            citations=citations,
            evidence=evidence,
            insufficient_evidence=insufficient_evidence,
        )
    except StorageDataError:
        raise
    except Exception as exc:
        raise StorageDataError("Malformed message document in storage.") from exc


class MongoConversationRepository:
    """Concrete PyMongo repository for chat conversation thread persistence."""

    COLLECTION_NAME = "conversations"

    def __init__(
        self,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
        collection: Collection | None = None,
    ) -> None:
        self._injected_db = db
        self._injected_collection = collection
        self._manager = manager

    @property
    def _collection(self) -> Collection:
        if self._injected_collection is not None:
            return self._injected_collection
        if self._injected_db is not None:
            return self._injected_db[self.COLLECTION_NAME]
        if self._manager is None:
            self._manager = get_default_storage_manager()
        return self._manager.get_database()[self.COLLECTION_NAME]

    def get_by_id(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> ConversationRecord | None:
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)
        conv_id = _validate_non_empty_string(conversation_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
            "conversation_id": conv_id,
        }
        try:
            doc = self._collection.find_one(query_filter)
        except Exception:
            raise StorageOperationError("Database read operation failed safely.") from None

        if doc is None:
            return None
        return _doc_to_conversation(doc)

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[ConversationRecord]:
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
        }
        sort_spec = [("created_at", -1), ("conversation_id", -1)]

        try:
            cursor = self._collection.find(query_filter).sort(sort_spec)
            docs = list(cursor)
        except Exception:
            raise StorageOperationError("Database list operation failed safely.") from None

        return [_doc_to_conversation(doc) for doc in docs]

    def save(self, conversation: ConversationRecord) -> ConversationRecord:
        if not isinstance(conversation, ConversationRecord):
            raise StorageDataError("Invalid ConversationRecord instance.")

        owner_id = _validate_non_empty_string(conversation.owner_session_id)
        repo_id = _validate_non_empty_string(conversation.repository_id)
        conv_id = _validate_non_empty_string(conversation.conversation_id)
        title = _validate_non_empty_string(conversation.title)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
            "conversation_id": conv_id,
        }
        doc = {
            "conversation_id": conv_id,
            "repository_id": repo_id,
            "owner_session_id": owner_id,
            "title": title,
            "created_at": _ensure_utc(conversation.created_at),
            "updated_at": _ensure_utc(conversation.updated_at),
        }

        try:
            self._collection.replace_one(query_filter, doc, upsert=True)
        except Exception:
            raise StorageOperationError("Database save operation failed safely.") from None

        return conversation

    def delete(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> bool:
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)
        conv_id = _validate_non_empty_string(conversation_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
            "conversation_id": conv_id,
        }
        try:
            res = self._collection.delete_one(query_filter)
            return res.deleted_count > 0
        except Exception:
            raise StorageOperationError("Database delete operation failed safely.") from None

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
        }
        try:
            res = self._collection.delete_many(query_filter)
            return int(res.deleted_count)
        except Exception:
            raise StorageOperationError("Database delete operation failed safely.") from None


class MongoMessageRepository:
    """Concrete PyMongo repository for chat message persistence."""

    COLLECTION_NAME = "messages"

    def __init__(
        self,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
        collection: Collection | None = None,
    ) -> None:
        self._injected_db = db
        self._injected_collection = collection
        self._manager = manager

    @property
    def _collection(self) -> Collection:
        if self._injected_collection is not None:
            return self._injected_collection
        if self._injected_db is not None:
            return self._injected_db[self.COLLECTION_NAME]
        if self._manager is None:
            self._manager = get_default_storage_manager()
        return self._manager.get_database()[self.COLLECTION_NAME]

    def list_by_conversation(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> list[MessageRecord]:
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)
        conv_id = _validate_non_empty_string(conversation_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
            "conversation_id": conv_id,
        }
        sort_spec = [("created_at", 1), ("message_id", 1)]

        try:
            cursor = self._collection.find(query_filter).sort(sort_spec)
            docs = list(cursor)
        except Exception:
            raise StorageOperationError("Database list operation failed safely.") from None

        return [_doc_to_message(doc) for doc in docs]

    def save(self, message: MessageRecord) -> MessageRecord:
        if not isinstance(message, MessageRecord):
            raise StorageDataError("Invalid MessageRecord instance.")

        owner_id = _validate_non_empty_string(message.owner_session_id)
        repo_id = _validate_non_empty_string(message.repository_id)
        conv_id = _validate_non_empty_string(message.conversation_id)
        msg_id = _validate_non_empty_string(message.message_id)

        if message.role not in ("user", "assistant"):
            raise StorageDataError("Invalid role in message record.")

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
            "conversation_id": conv_id,
            "message_id": msg_id,
        }
        doc = {
            "message_id": msg_id,
            "conversation_id": conv_id,
            "repository_id": repo_id,
            "owner_session_id": owner_id,
            "role": message.role,
            "content": _validate_content(message.content),
            "created_at": _ensure_utc(message.created_at),
            "citations": [_citation_to_doc(c) for c in message.citations],
            "evidence": [_evidence_to_doc(e) for e in message.evidence],
            "insufficient_evidence": message.insufficient_evidence,
        }

        try:
            self._collection.replace_one(query_filter, doc, upsert=True)
        except Exception:
            raise StorageOperationError("Database save operation failed safely.") from None

        return message

    def delete_by_conversation(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> int:
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)
        conv_id = _validate_non_empty_string(conversation_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
            "conversation_id": conv_id,
        }
        try:
            res = self._collection.delete_many(query_filter)
            return int(res.deleted_count)
        except Exception:
            raise StorageOperationError("Database delete operation failed safely.") from None

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:
        owner_id = _validate_non_empty_string(owner_session_id)
        repo_id = _validate_non_empty_string(repository_id)

        query_filter = {
            "owner_session_id": owner_id,
            "repository_id": repo_id,
        }
        try:
            res = self._collection.delete_many(query_filter)
            return int(res.deleted_count)
        except Exception:
            raise StorageOperationError("Database delete operation failed safely.") from None


def _validate_exchange_records(
    conversation: ConversationRecord,
    user_message: MessageRecord,
    assistant_message: MessageRecord,
) -> None:
    """Validate canonical domain records before atomic exchange persistence."""
    if type(conversation) is not ConversationRecord:
        raise StorageDataError("Invalid conversation record type.")
    if type(user_message) is not MessageRecord:
        raise StorageDataError("Invalid user message record type.")
    if type(assistant_message) is not MessageRecord:
        raise StorageDataError("Invalid assistant message record type.")

    owner_id = conversation.owner_session_id
    repo_id = conversation.repository_id
    conv_id = conversation.conversation_id

    if (
        user_message.owner_session_id != owner_id
        or assistant_message.owner_session_id != owner_id
    ):
        raise StorageDataError("Owner session ID mismatch across exchange records.")

    if (
        user_message.repository_id != repo_id
        or assistant_message.repository_id != repo_id
    ):
        raise StorageDataError("Repository ID mismatch across exchange records.")

    if (
        user_message.conversation_id != conv_id
        or assistant_message.conversation_id != conv_id
    ):
        raise StorageDataError("Conversation ID mismatch across exchange records.")

    if user_message.role != "user":
        raise StorageDataError("User message role must be 'user'.")
    if assistant_message.role != "assistant":
        raise StorageDataError("Assistant message role must be 'assistant'.")

    if user_message.message_id == assistant_message.message_id:
        raise StorageDataError("Message IDs within exchange must be distinct.")

    if user_message.citations or user_message.evidence:
        raise StorageDataError("User message must not contain citations or evidence.")

    if user_message.created_at.tzinfo is None or assistant_message.created_at.tzinfo is None:
        raise StorageDataError("Message created_at timestamps must be timezone-aware UTC.")

    if user_message.created_at > assistant_message.created_at:
        raise StorageDataError("User message timestamp cannot be later than assistant message.")


class MongoConversationExchangeRepository:
    """Concrete PyMongo repository for atomic conversation exchange operations."""

    def __init__(
        self,
        conv_repo: MongoConversationRepository | None = None,
        msg_repo: MongoMessageRepository | None = None,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
    ) -> None:
        self._conv_repo = conv_repo or MongoConversationRepository(db=db, manager=manager)
        self._msg_repo = msg_repo or MongoMessageRepository(db=db, manager=manager)

    def create_conversation_exchange(
        self,
        conversation: ConversationRecord,
        user_message: MessageRecord,
        assistant_message: MessageRecord,
    ) -> None:
        """Atomically persist a new conversation exchange using scoped compensation on failure."""
        _validate_exchange_records(conversation, user_message, assistant_message)

        owner_id = conversation.owner_session_id
        repo_id = conversation.repository_id
        conv_id = conversation.conversation_id

        # Require that conversation does not already exist
        existing = self._conv_repo.get_by_id(owner_id, repo_id, conv_id)
        if existing is not None:
            raise StorageDataError("Conversation ID collision during creation.")

        # Attempt 1: Save conversation
        try:
            self._conv_repo.save(conversation)
        except Exception:
            raise StorageOperationError("Failed to save conversation record.") from None

        # Attempt 2: Save user message
        try:
            self._msg_repo.save(user_message)
        except Exception:
            # Compensate: delete newly created conversation
            try:
                self._conv_repo.delete(owner_id, repo_id, conv_id)
            except Exception:
                pass
            raise StorageOperationError(
                "Failed to save user message during exchange."
            ) from None

        # Attempt 3: Save assistant message
        try:
            self._msg_repo.save(assistant_message)
        except Exception:
            # Compensate: delete user message and conversation
            try:
                self._msg_repo.delete_by_conversation(owner_id, repo_id, conv_id)
            except Exception:
                pass
            try:
                self._conv_repo.delete(owner_id, repo_id, conv_id)
            except Exception:
                pass
            raise StorageOperationError(
                "Failed to save assistant message during exchange."
            ) from None

    def append_message_exchange(
        self,
        updated_conversation: ConversationRecord,
        user_message: MessageRecord,
        assistant_message: MessageRecord,
    ) -> None:
        """Atomically append a user/assistant message pair to an existing conversation."""
        _validate_exchange_records(updated_conversation, user_message, assistant_message)

        owner_id = updated_conversation.owner_session_id
        repo_id = updated_conversation.repository_id
        conv_id = updated_conversation.conversation_id

        # Fetch original conversation state before mutation
        original_conv = self._conv_repo.get_by_id(owner_id, repo_id, conv_id)
        if original_conv is None:
            raise StorageDataError("Target conversation does not exist for message append.")

        # Verify message IDs do not collide with existing stored messages
        existing_msgs = self._msg_repo.list_by_conversation(owner_id, repo_id, conv_id)
        existing_msg_ids = {m.message_id for m in existing_msgs}
        if (
            user_message.message_id in existing_msg_ids
            or assistant_message.message_id in existing_msg_ids
        ):
            raise StorageDataError("Collision on message_id during message append.")

        # Attempt 1: Update conversation timestamp
        try:
            self._conv_repo.save(updated_conversation)
        except Exception:
            raise StorageOperationError("Failed to update conversation record.") from None

        # Attempt 2: Save user message
        try:
            self._msg_repo.save(user_message)
        except Exception:
            # Compensate: restore original conversation state
            try:
                self._conv_repo.save(original_conv)
            except Exception:
                pass
            raise StorageOperationError("Failed to save user message during append.") from None

        # Attempt 3: Save assistant message
        try:
            self._msg_repo.save(assistant_message)
        except Exception:
            # Compensate: delete user message and restore original conversation
            try:
                self._msg_repo._collection.delete_one({
                    "owner_session_id": owner_id,
                    "repository_id": repo_id,
                    "conversation_id": conv_id,
                    "message_id": user_message.message_id,
                })
            except Exception:
                pass
            try:
                self._conv_repo.save(original_conv)
            except Exception:
                pass
            raise StorageOperationError("Failed to save assistant message during append.") from None


def init_indexes(db: Database) -> None:
    """Idempotently create standard compound indexes across all canonical collections."""
    try:
        db["anonymous_sessions"].create_index(
            [("owner_session_id", 1)], unique=True, name="anon_sessions_owner_id_idx"
        )
        db["anonymous_sessions"].create_index(
            [("expires_at", 1)], name="anon_sessions_expires_at_idx"
        )

        db["repositories"].create_index(
            [("repository_id", 1)], unique=True, name="repositories_id_idx"
        )
        db["repositories"].create_index(
            [("owner_session_id", 1), ("repository_id", 1)], name="repositories_owner_repo_idx"
        )
        db["repositories"].create_index(
            [("owner_session_id", 1), ("created_at", -1)], name="repositories_owner_created_idx"
        )

        db["indexing_jobs"].create_index(
            [("job_id", 1)], unique=True, name="indexing_jobs_id_idx"
        )
        db["indexing_jobs"].create_index(
            [("owner_session_id", 1), ("job_id", 1)], name="indexing_jobs_owner_job_idx"
        )
        db["indexing_jobs"].create_index(
            [("owner_session_id", 1), ("repository_id", 1)], name="indexing_jobs_owner_repo_idx"
        )

        db["code_chunks"].create_index(
            [("owner_session_id", 1), ("repository_id", 1), ("chunk_id", 1)],
            unique=True,
            name="code_chunks_owner_repo_chunk_idx",
        )
        db["code_chunks"].create_index(
            [("owner_session_id", 1), ("repository_id", 1)], name="code_chunks_owner_repo_idx"
        )
        db["code_chunks"].create_index(
            [("owner_session_id", 1), ("repository_id", 1), ("relative_path", 1)],
            name="code_chunks_owner_repo_path_idx",
        )
        db["code_chunks"].create_index(
            [("owner_session_id", 1), ("repository_id", 1), ("search_terms", 1)],
            name="code_chunks_owner_repo_search_terms_idx",
        )
        db["code_chunks"].create_index(
            [("owner_session_id", 1), ("repository_id", 1), ("symbol_name_normalized", 1)],
            name="code_chunks_owner_repo_symbol_norm_idx",
        )
        db["code_chunks"].create_index(
            [("owner_session_id", 1), ("repository_id", 1), ("relative_path_normalized", 1)],
            name="code_chunks_owner_repo_path_norm_idx",
        )

        db["conversations"].create_index(
            [("conversation_id", 1)], unique=True, name="conversations_id_idx"
        )
        db["conversations"].create_index(
            [("owner_session_id", 1), ("repository_id", 1), ("conversation_id", 1)],
            name="conversations_owner_repo_conv_idx",
        )

        db["messages"].create_index(
            [("message_id", 1)], unique=True, name="messages_id_idx"
        )
        db["messages"].create_index(
            [("owner_session_id", 1), ("conversation_id", 1), ("created_at", 1)],
            name="messages_owner_conv_created_idx",
        )
    except (AttributeError, TypeError):
        pass





