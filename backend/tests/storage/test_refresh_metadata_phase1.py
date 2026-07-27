"""Focused unit tests for REPO-001 Phase 1 models, defaults, and BSON serialization."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from bson import ObjectId

from sourcetrace.api.schemas import job_record_to_schema, repository_record_to_schema
from sourcetrace.models.domain import CodeChunk, IndexingJobRecord, RepositoryRecord
from sourcetrace.storage.mongo_repositories import (
    MongoIndexingJobRepository,
    MongoRepositoryRepository,
    _chunk_to_doc,
    _doc_to_chunk,
    _doc_to_job,
    _doc_to_repository,
)


def test_repository_record_defaults() -> None:
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id="sess_1",
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
    )
    assert repo.active_generation_id is None
    assert repo.last_indexed_at is None
    assert repo.indexed_commit_sha is None
    assert repo.indexed_branch is None
    assert repo.parser_versions == ()
    assert repo.flow_evidence_complete is False
    assert repo.indexed_file_count == 0
    assert repo.indexed_chunk_count == 0
    assert repo.consecutive_refresh_failures == 0
    assert repo.is_stale is None
    assert repo.stale_checked_at is None


def test_indexing_job_record_defaults() -> None:
    now = datetime.now(UTC)
    job = IndexingJobRecord(
        job_id="job_1",
        repository_id="repo_1",
        owner_session_id="sess_1",
        status="queued",
        current_step="queued",
        created_at=now,
        updated_at=now,
    )
    assert job.job_type == "initial"


def test_code_chunk_generation_id_default() -> None:
    now = datetime.now(UTC)
    chunk = CodeChunk(
        chunk_id="chk_1",
        repository_id="repo_1",
        owner_session_id="sess_1",
        relative_path="main.py",
        language="python",
        symbol_name="main",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def main(): pass",
        content_hash="abc",
        parser_version="python-ast-v3",
        created_at=now,
    )
    assert chunk.generation_id is None


def test_repository_record_to_schema_conversion() -> None:
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id="sess_1",
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id="job_100",
        last_indexed_at=now,
        indexed_commit_sha="sha123",
        indexed_branch="main",
        parser_versions=("python-ast-v3", "js-ts-treesitter-v3"),
        flow_evidence_complete=True,
        indexed_file_count=5,
        indexed_chunk_count=12,
        consecutive_refresh_failures=1,
        is_stale=True,
        stale_checked_at=now,
    )
    schema = repository_record_to_schema(repo)
    assert schema.active_generation_id == "job_100"
    assert schema.last_indexed_at == now
    assert schema.indexed_commit_sha == "sha123"
    assert schema.indexed_branch == "main"
    assert schema.parser_versions == ["python-ast-v3", "js-ts-treesitter-v3"]
    assert schema.flow_evidence_complete is True
    assert schema.indexed_file_count == 5
    assert schema.indexed_chunk_count == 12
    assert schema.consecutive_refresh_failures == 1
    assert schema.is_stale is True
    assert schema.stale_checked_at == now


def test_job_record_to_schema_conversion() -> None:
    now = datetime.now(UTC)
    job = IndexingJobRecord(
        job_id="job_1",
        repository_id="repo_1",
        owner_session_id="sess_1",
        status="queued",
        current_step="queued",
        created_at=now,
        updated_at=now,
        job_type="refresh",
    )
    schema = job_record_to_schema(job)
    assert schema.job_type == "refresh"


def test_repository_bson_roundtrip_legacy_and_new() -> None:
    now = datetime.now(UTC)

    # Legacy doc (missing new fields)
    legacy_doc = {
        "_id": ObjectId(),
        "repository_id": "repo_1",
        "owner_session_id": "sess_1",
        "name": "test-repo",
        "source_type": "github",
        "status": "ready",
        "created_at": now,
        "updated_at": now,
    }
    repo_legacy = _doc_to_repository(legacy_doc)
    assert repo_legacy.active_generation_id is None
    assert repo_legacy.parser_versions == ()
    assert repo_legacy.flow_evidence_complete is False
    assert repo_legacy.is_stale is None

    # Full doc (all fields present)
    full_repo = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id="sess_1",
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id="job_999",
        last_indexed_at=now,
        indexed_commit_sha="def456",
        indexed_branch="main",
        parser_versions=("python-ast-v3",),
        flow_evidence_complete=True,
        indexed_file_count=10,
        indexed_chunk_count=20,
        consecutive_refresh_failures=0,
        is_stale=False,
        stale_checked_at=now,
    )
    mock_coll = MagicMock()
    repo_storage = MongoRepositoryRepository(collection=mock_coll)
    repo_storage.save(full_repo)

    mock_coll.replace_one.assert_called_once()
    saved_doc = mock_coll.replace_one.call_args[0][1]
    assert saved_doc["active_generation_id"] == "job_999"
    assert saved_doc["indexed_commit_sha"] == "def456"
    assert saved_doc["parser_versions"] == ["python-ast-v3"]
    assert saved_doc["flow_evidence_complete"] is True

    deserialized = _doc_to_repository(saved_doc)
    assert deserialized == full_repo


def test_job_bson_roundtrip_legacy_and_new() -> None:
    now = datetime.now(UTC)

    # Legacy doc without job_type
    legacy_doc = {
        "_id": ObjectId(),
        "job_id": "job_1",
        "repository_id": "repo_1",
        "owner_session_id": "sess_1",
        "status": "queued",
        "current_step": "queued",
        "created_at": now,
        "updated_at": now,
    }
    job_legacy = _doc_to_job(legacy_doc)
    assert job_legacy.job_type == "initial"

    # Save job with custom job_type
    job_refresh = IndexingJobRecord(
        job_id="job_2",
        repository_id="repo_1",
        owner_session_id="sess_1",
        status="queued",
        current_step="queued",
        created_at=now,
        updated_at=now,
        job_type="refresh",
    )
    mock_coll = MagicMock()
    job_storage = MongoIndexingJobRepository(collection=mock_coll)
    job_storage.save(job_refresh)

    mock_coll.replace_one.assert_called_once()
    saved_doc = mock_coll.replace_one.call_args[0][1]
    assert saved_doc["job_type"] == "refresh"

    deserialized = _doc_to_job(saved_doc)
    assert deserialized == job_refresh


def test_code_chunk_generation_id_bson_roundtrip() -> None:
    now = datetime.now(UTC)
    chunk = CodeChunk(
        chunk_id="chk_1",
        repository_id="repo_1",
        owner_session_id="sess_1",
        relative_path="main.py",
        language="python",
        symbol_name="main",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def main(): pass",
        content_hash="abc",
        parser_version="python-ast-v3",
        created_at=now,
        generation_id="job_abc",
    )
    doc = _chunk_to_doc(chunk)
    assert doc["generation_id"] == "job_abc"

    deserialized = _doc_to_chunk(doc)
    assert deserialized.generation_id == "job_abc"

    # Legacy doc without generation_id
    del doc["generation_id"]
    deserialized_legacy = _doc_to_chunk(doc)
    assert deserialized_legacy.generation_id is None
