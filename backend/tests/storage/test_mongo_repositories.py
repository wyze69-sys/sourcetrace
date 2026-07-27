"""Offline unit tests for PyMongo concrete repository implementations."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from sourcetrace.core.exceptions import StorageDataError
from sourcetrace.models.domain import (
    AnonymousSession,
    IndexingJobRecord,
    RepositoryRecord,
)
from sourcetrace.storage.mongo_repositories import (
    MongoAnonymousSessionRepository,
    MongoIndexingJobRepository,
    MongoRepositoryRepository,
)


@pytest.fixture
def mock_collection() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_db(mock_collection: MagicMock) -> MagicMock:
    db = MagicMock()
    db.__getitem__.return_value = mock_collection
    return db


def test_no_live_mongodb_connection_on_import_or_instantiation() -> None:
    session_repo = MongoAnonymousSessionRepository()
    repo_repo = MongoRepositoryRepository()
    job_repo = MongoIndexingJobRepository()

    assert session_repo._manager is None
    assert repo_repo._manager is None
    assert job_repo._manager is None


def test_session_repository_save_get_delete_ownership_scoping(
    mock_collection: MagicMock,
) -> None:
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    now = datetime.now(UTC)
    session = AnonymousSession(
        owner_session_id="sess_abc123",
        last_active_at=now,
        expires_at=now,
        created_at=now,
        updated_at=now,
    )

    # Save assertion
    saved = repo.save(session)
    assert saved == session
    mock_collection.replace_one.assert_called_once()
    call_args = mock_collection.replace_one.call_args
    assert call_args[0][0] == {"owner_session_id": "sess_abc123"}
    assert call_args[0][1]["owner_session_id"] == "sess_abc123"
    assert call_args[1] == {"upsert": True}

    # Get by ID assertion
    mock_collection.find_one.return_value = {
        "_id": ObjectId(),
        "owner_session_id": "sess_abc123",
        "last_active_at": now,
        "expires_at": now,
        "created_at": now,
        "updated_at": now,
    }
    retrieved = repo.get_by_id("sess_abc123")
    mock_collection.find_one.assert_called_with({"owner_session_id": "sess_abc123"})
    assert retrieved is not None
    assert retrieved.owner_session_id == "sess_abc123"

    # Delete assertion
    mock_collection.delete_one.return_value.deleted_count = 1
    result = repo.delete("sess_abc123")
    mock_collection.delete_one.assert_called_with({"owner_session_id": "sess_abc123"})
    assert result is True


def test_repository_repository_save_get_delete_scoping(
    mock_collection: MagicMock,
) -> None:
    repo = MongoRepositoryRepository(collection=mock_collection)
    now = datetime.now(UTC)
    repo_record = RepositoryRecord(
        repository_id="repo_xyz",
        owner_session_id="sess_abc123",
        name="test-repo",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/org/test-repo",
        file_count=10,
        chunk_count=20,
    )

    # Save assertion
    repo.save(repo_record)
    mock_collection.replace_one.assert_called_once()
    call_args = mock_collection.replace_one.call_args
    assert call_args[0][0] == {
        "owner_session_id": "sess_abc123",
        "repository_id": "repo_xyz",
    }
    assert call_args[0][1]["index_mode"] == "static"
    assert call_args[1] == {"upsert": True}

    # Get by ID assertion (historical document without index_mode -> defaults to cloud_ai)
    mock_collection.find_one.return_value = {
        "_id": ObjectId(),
        "repository_id": "repo_xyz",
        "owner_session_id": "sess_abc123",
        "name": "test-repo",
        "source_type": "github",
        "status": "ready",
        "created_at": now,
        "updated_at": now,
        "github_url": "https://github.com/org/test-repo",
        "file_count": 10,
        "chunk_count": 20,
    }
    retrieved = repo.get_by_id("sess_abc123", "repo_xyz")
    mock_collection.find_one.assert_called_with(
        {"owner_session_id": "sess_abc123", "repository_id": "repo_xyz"}
    )
    assert retrieved is not None
    assert retrieved.repository_id == "repo_xyz"
    assert retrieved.index_mode == "cloud_ai"

    # Delete assertion
    mock_collection.delete_one.return_value.deleted_count = 1
    result = repo.delete("sess_abc123", "repo_xyz")
    mock_collection.delete_one.assert_called_with(
        {"owner_session_id": "sess_abc123", "repository_id": "repo_xyz"}
    )
    assert result is True


def test_repository_repository_list_and_count_scoping(
    mock_collection: MagicMock,
) -> None:
    repo = MongoRepositoryRepository(collection=mock_collection)
    now = datetime.now(UTC)

    mock_cursor = MagicMock()
    mock_cursor.__iter__.return_value = iter(
        [
            {
                "_id": ObjectId(),
                "repository_id": "repo_1",
                "owner_session_id": "sess_abc123",
                "name": "repo1",
                "source_type": "zip",
                "status": "ready",
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    mock_collection.find.return_value.sort.return_value = mock_cursor

    # List by owner
    items = repo.list_by_owner("sess_abc123")
    mock_collection.find.assert_called_with({"owner_session_id": "sess_abc123"})
    mock_collection.find.return_value.sort.assert_called_with("created_at", -1)
    assert len(items) == 1
    assert items[0].repository_id == "repo_1"

    # Count by owner
    mock_collection.count_documents.return_value = 1
    count = repo.count_by_owner("sess_abc123")
    mock_collection.count_documents.assert_called_with({"owner_session_id": "sess_abc123"})
    assert count == 1


def test_indexing_job_repository_scoping_and_operations(
    mock_collection: MagicMock,
) -> None:
    repo = MongoIndexingJobRepository(collection=mock_collection)
    now = datetime.now(UTC)
    job = IndexingJobRecord(
        job_id="job_789",
        repository_id="repo_xyz",
        owner_session_id="sess_abc123",
        status="queued",
        current_step="initializing",
        created_at=now,
        updated_at=now,
    )

    # Save assertion
    repo.save(job)
    mock_collection.replace_one.assert_called_once()
    call_args = mock_collection.replace_one.call_args
    assert call_args[0][0] == {
        "owner_session_id": "sess_abc123",
        "job_id": "job_789",
    }
    assert call_args[1] == {"upsert": True}

    # Get by ID
    mock_collection.find_one.return_value = {
        "_id": ObjectId(),
        "job_id": "job_789",
        "repository_id": "repo_xyz",
        "owner_session_id": "sess_abc123",
        "status": "queued",
        "current_step": "initializing",
        "created_at": now,
        "updated_at": now,
    }
    retrieved = repo.get_by_id("sess_abc123", "job_789")
    mock_collection.find_one.assert_called_with(
        {"owner_session_id": "sess_abc123", "job_id": "job_789"}
    )
    assert retrieved is not None
    assert retrieved.job_id == "job_789"

    # Get by repository
    repo.get_by_repository("sess_abc123", "repo_xyz")
    mock_collection.find_one.assert_called_with(
        {"owner_session_id": "sess_abc123", "repository_id": "repo_xyz"}
    )

    # Delete by repository
    mock_collection.delete_many.return_value.deleted_count = 2
    deleted = repo.delete_by_repository("sess_abc123", "repo_xyz")
    mock_collection.delete_many.assert_called_with(
        {"owner_session_id": "sess_abc123", "repository_id": "repo_xyz"}
    )
    assert deleted == 2


def test_returned_domain_records_exclude_mongodb_id(
    mock_collection: MagicMock,
) -> None:
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    now = datetime.now(UTC)
    mock_collection.find_one.return_value = {
        "_id": ObjectId("60c72b2f9b1d8b2a3c8e4f1a"),
        "owner_session_id": "sess_abc123",
        "last_active_at": now,
        "expires_at": now,
        "created_at": now,
        "updated_at": now,
    }

    session = repo.get_by_id("sess_abc123")
    assert session is not None
    assert not hasattr(session, "_id")


def test_datetimes_are_timezone_aware_utc(mock_collection: MagicMock) -> None:
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    naive_dt = datetime(2026, 7, 24, 0, 0, 0)
    mock_collection.find_one.return_value = {
        "owner_session_id": "sess_abc123",
        "last_active_at": naive_dt,
        "expires_at": naive_dt,
        "created_at": naive_dt,
        "updated_at": naive_dt,
    }

    session = repo.get_by_id("sess_abc123")
    assert session is not None
    assert session.created_at.tzinfo == UTC


def test_malformed_documents_fail_safely(mock_collection: MagicMock) -> None:
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    mock_collection.find_one.return_value = {
        "_id": ObjectId(),
        "owner_session_id": "sess_abc123",
        # Missing required fields like created_at, expires_at, etc.
    }

    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_abc123")

    assert "StorageDataError" in repr(exc_info.value)
    assert "ObjectId" not in str(exc_info.value)
    assert "sess_abc123" not in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_owner_id",
    [123, True, {"session": "123"}, ["sess_123"], ""],
)
def test_malformed_owner_session_id_rejection(
    mock_collection: MagicMock, invalid_owner_id: object
) -> None:
    now = datetime.now(UTC)
    mock_collection.find_one.return_value = {
        "owner_session_id": invalid_owner_id,
        "last_active_at": now,
        "expires_at": now,
        "created_at": now,
        "updated_at": now,
    }
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_test")

    err_msg = str(exc_info.value)
    assert "123" not in err_msg
    assert "sess_123" not in err_msg


@pytest.mark.parametrize(
    "invalid_repo_id",
    [["repo1"], {"repo": "1"}, 456, True, ""],
)
def test_malformed_repository_id_rejection(
    mock_collection: MagicMock, invalid_repo_id: object
) -> None:
    now = datetime.now(UTC)
    mock_collection.find_one.return_value = {
        "repository_id": invalid_repo_id,
        "owner_session_id": "sess_valid",
        "name": "test-repo",
        "source_type": "github",
        "status": "ready",
        "created_at": now,
        "updated_at": now,
    }
    repo = MongoRepositoryRepository(collection=mock_collection)
    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "repo_test")

    err_msg = str(exc_info.value)
    assert "repo1" not in err_msg
    assert "456" not in err_msg


@pytest.mark.parametrize(
    "invalid_job_id",
    [True, False, 789, ["job1"], {"job": "1"}, ""],
)
def test_malformed_job_id_rejection(mock_collection: MagicMock, invalid_job_id: object) -> None:
    now = datetime.now(UTC)
    mock_collection.find_one.return_value = {
        "job_id": invalid_job_id,
        "repository_id": "repo_valid",
        "owner_session_id": "sess_valid",
        "status": "queued",
        "current_step": "initializing",
        "created_at": now,
        "updated_at": now,
    }
    repo = MongoIndexingJobRepository(collection=mock_collection)
    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "job_test")

    err_msg = str(exc_info.value)
    assert "789" not in err_msg
    assert "job1" not in err_msg


@pytest.mark.parametrize(
    "field,invalid_val",
    [
        ("name", 123),
        ("name", True),
        ("status", ["ready"]),
        ("source_type", {"type": "github"}),
    ],
)
def test_repository_non_string_field_rejection(
    mock_collection: MagicMock, field: str, invalid_val: object
) -> None:
    now = datetime.now(UTC)
    doc = {
        "repository_id": "repo_valid",
        "owner_session_id": "sess_valid",
        "name": "test-repo",
        "source_type": "github",
        "status": "ready",
        "created_at": now,
        "updated_at": now,
    }
    doc[field] = invalid_val
    mock_collection.find_one.return_value = doc
    repo = MongoRepositoryRepository(collection=mock_collection)

    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "repo_valid")

    err_msg = str(exc_info.value)
    assert "123" not in err_msg


@pytest.mark.parametrize(
    "field,invalid_val",
    [
        ("current_step", 456),
        ("status", True),
    ],
)
def test_job_non_string_field_rejection(
    mock_collection: MagicMock, field: str, invalid_val: object
) -> None:
    now = datetime.now(UTC)
    doc = {
        "job_id": "job_valid",
        "repository_id": "repo_valid",
        "owner_session_id": "sess_valid",
        "status": "queued",
        "current_step": "initializing",
        "created_at": now,
        "updated_at": now,
    }
    doc[field] = invalid_val
    mock_collection.find_one.return_value = doc
    repo = MongoIndexingJobRepository(collection=mock_collection)

    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "job_valid")

    err_msg = str(exc_info.value)
    assert "456" not in err_msg


@pytest.mark.parametrize(
    "invalid_url",
    [12345, True, ["https://github.com"], {"url": "https://github.com"}],
)
def test_repository_invalid_github_url_rejection(
    mock_collection: MagicMock, invalid_url: object
) -> None:
    now = datetime.now(UTC)
    mock_collection.find_one.return_value = {
        "repository_id": "repo_valid",
        "owner_session_id": "sess_valid",
        "name": "test-repo",
        "source_type": "github",
        "status": "ready",
        "created_at": now,
        "updated_at": now,
        "github_url": invalid_url,
    }
    repo = MongoRepositoryRepository(collection=mock_collection)

    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "repo_valid")

    err_msg = str(exc_info.value)
    assert "12345" not in err_msg


@pytest.mark.parametrize(
    "invalid_err",
    [500, True, {"err": "msg"}, ["error"]],
)
def test_job_invalid_error_message_rejection(
    mock_collection: MagicMock, invalid_err: object
) -> None:
    now = datetime.now(UTC)
    mock_collection.find_one.return_value = {
        "job_id": "job_valid",
        "repository_id": "repo_valid",
        "owner_session_id": "sess_valid",
        "status": "failed",
        "current_step": "error",
        "created_at": now,
        "updated_at": now,
        "error_message": invalid_err,
    }
    repo = MongoIndexingJobRepository(collection=mock_collection)

    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "job_valid")

    err_msg = str(exc_info.value)
    assert "500" not in err_msg


@pytest.mark.parametrize(
    "field,invalid_num",
    [
        ("file_count", True),
        ("file_count", False),
        ("file_count", 10.5),
        ("file_count", "10"),
        ("chunk_count", True),
        ("chunk_count", 20.7),
        ("chunk_count", "20"),
    ],
)
def test_repository_numeric_fields_type_rejection(
    mock_collection: MagicMock, field: str, invalid_num: object
) -> None:
    now = datetime.now(UTC)
    doc = {
        "repository_id": "repo_valid",
        "owner_session_id": "sess_valid",
        "name": "test-repo",
        "source_type": "github",
        "status": "ready",
        "created_at": now,
        "updated_at": now,
    }
    doc[field] = invalid_num
    mock_collection.find_one.return_value = doc
    repo = MongoRepositoryRepository(collection=mock_collection)

    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "repo_valid")

    err_msg = str(exc_info.value)
    assert "10.5" not in err_msg
    assert "20.7" not in err_msg


@pytest.mark.parametrize(
    "invalid_progress",
    [True, False, 50.5, "50", [50], {"p": 50}],
)
def test_job_progress_percentage_type_rejection(
    mock_collection: MagicMock, invalid_progress: object
) -> None:
    now = datetime.now(UTC)
    mock_collection.find_one.return_value = {
        "job_id": "job_valid",
        "repository_id": "repo_valid",
        "owner_session_id": "sess_valid",
        "status": "parsing",
        "current_step": "ast_parsing",
        "created_at": now,
        "updated_at": now,
        "progress_percentage": invalid_progress,
    }
    repo = MongoIndexingJobRepository(collection=mock_collection)

    with pytest.raises(StorageDataError) as exc_info:
        repo.get_by_id("sess_valid", "job_valid")

    err_msg = str(exc_info.value)
    assert "50.5" not in err_msg


def test_reserve_repository_slot_atomic_filter_and_update(
    mock_collection: MagicMock,
) -> None:
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    now = datetime(2026, 7, 24, 10, 0, 0, tzinfo=UTC)

    mock_collection.find_one_and_update.return_value = {
        "_id": ObjectId(),
        "owner_session_id": "sess_abc123",
        "last_active_at": now,
        "expires_at": datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC),
        "created_at": now,
        "updated_at": now,
        "active_repository_count": 1,
    }

    reserved = repo.reserve_repository_slot("sess_abc123", now=now, max_quota=3)
    assert reserved is not None
    assert reserved.owner_session_id == "sess_abc123"
    assert reserved.active_repository_count == 1

    mock_collection.find_one_and_update.assert_called_once()
    call_args = mock_collection.find_one_and_update.call_args
    query_filter, update_doc = call_args[0][0], call_args[0][1]

    assert query_filter["owner_session_id"] == "sess_abc123"
    assert query_filter["expires_at"] == {"$gt": now}
    assert query_filter["$or"] == [
        {"active_repository_count": {"$lt": 3}},
        {"active_repository_count": {"$exists": False}},
    ]
    assert update_doc["$inc"] == {"active_repository_count": 1}
    assert update_doc["$set"]["last_active_at"] == now
    assert update_doc["$set"]["expires_at"] == datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC)


def test_reserve_repository_slot_returns_none_when_query_fails(
    mock_collection: MagicMock,
) -> None:
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    now = datetime.now(UTC)
    mock_collection.find_one_and_update.return_value = None

    reserved = repo.reserve_repository_slot("sess_expired_or_full", now=now)
    assert reserved is None


def test_release_repository_slot_atomic_decrement(
    mock_collection: MagicMock,
) -> None:
    repo = MongoAnonymousSessionRepository(collection=mock_collection)
    mock_collection.find_one_and_update.return_value = {"active_repository_count": 0}

    released = repo.release_repository_slot("sess_abc123")
    assert released is True

    call_args = mock_collection.find_one_and_update.call_args
    query_filter, update_doc = call_args[0][0], call_args[0][1]

    assert query_filter == {
        "owner_session_id": "sess_abc123",
        "active_repository_count": {"$gt": 0},
    }
    assert update_doc == {"$inc": {"active_repository_count": -1}}


def test_mongo_job_claim_transition_filter_and_no_upsert(
    mock_collection: MagicMock,
) -> None:
    repo = MongoIndexingJobRepository(collection=mock_collection)
    now = datetime.now(UTC)

    mock_collection.find_one_and_update.return_value = {
        "_id": ObjectId(),
        "job_id": "job_789",
        "repository_id": "repo_xyz",
        "owner_session_id": "sess_abc123",
        "status": "acquiring",
        "current_step": "Acquiring source repository",
        "created_at": now,
        "updated_at": now,
        "progress_percentage": 15,
    }

    result = repo.transition_status(
        owner_session_id="sess_abc123",
        job_id="job_789",
        repository_id="repo_xyz",
        expected_status="queued",
        new_status="acquiring",
        current_step="Acquiring source repository",
        progress_percentage=15,
        updated_at=now,
    )

    assert result is not None
    assert result.status == "acquiring"

    mock_collection.find_one_and_update.assert_called_once()
    call_args = mock_collection.find_one_and_update.call_args
    query_filter = call_args[0][0]
    update_doc = call_args[0][1]
    kwargs = call_args[1]

    assert query_filter == {
        "owner_session_id": "sess_abc123",
        "job_id": "job_789",
        "repository_id": "repo_xyz",
        "status": "queued",
    }
    assert update_doc["$set"]["status"] == "acquiring"
    assert update_doc["$set"]["current_step"] == "Acquiring source repository"
    assert kwargs.get("upsert") is False


def test_mongo_repository_transition_filter_and_no_upsert(
    mock_collection: MagicMock,
) -> None:
    repo = MongoRepositoryRepository(collection=mock_collection)
    now = datetime.now(UTC)

    mock_collection.find_one_and_update.return_value = {
        "_id": ObjectId(),
        "repository_id": "repo_xyz",
        "owner_session_id": "sess_abc123",
        "name": "test-repo",
        "source_type": "github",
        "status": "indexing",
        "created_at": now,
        "updated_at": now,
    }

    result = repo.transition_status(
        owner_session_id="sess_abc123",
        repository_id="repo_xyz",
        expected_status="pending",
        new_status="indexing",
        updated_at=now,
    )

    assert result is not None
    assert result.status == "indexing"

    mock_collection.find_one_and_update.assert_called_once()
    call_args = mock_collection.find_one_and_update.call_args
    query_filter = call_args[0][0]
    update_doc = call_args[0][1]
    kwargs = call_args[1]

    assert query_filter == {
        "owner_session_id": "sess_abc123",
        "repository_id": "repo_xyz",
        "status": "pending",
    }
    assert update_doc["$set"]["status"] == "indexing"
    assert kwargs.get("upsert") is False


def test_mongo_job_scanning_transition_filter_and_no_upsert(
    mock_collection: MagicMock,
) -> None:
    repo = MongoIndexingJobRepository(collection=mock_collection)
    now = datetime.now(UTC)

    mock_collection.find_one_and_update.return_value = {
        "_id": ObjectId(),
        "job_id": "job_789",
        "repository_id": "repo_xyz",
        "owner_session_id": "sess_abc123",
        "status": "scanning",
        "current_step": "Scanning source files",
        "created_at": now,
        "updated_at": now,
        "progress_percentage": 30,
    }

    result = repo.transition_status(
        owner_session_id="sess_abc123",
        job_id="job_789",
        repository_id="repo_xyz",
        expected_status="acquiring",
        new_status="scanning",
        current_step="Scanning source files",
        progress_percentage=30,
        updated_at=now,
    )

    assert result is not None
    assert result.status == "scanning"

    mock_collection.find_one_and_update.assert_called_once()
    call_args = mock_collection.find_one_and_update.call_args
    query_filter = call_args[0][0]
    kwargs = call_args[1]

    assert query_filter == {
        "owner_session_id": "sess_abc123",
        "job_id": "job_789",
        "repository_id": "repo_xyz",
        "status": "acquiring",
    }
    assert kwargs.get("upsert") is False


def test_mongo_failure_finalization_transitions_filter_legitimate_states_and_no_upsert(
    mock_collection: MagicMock,
) -> None:
    job_repo = MongoIndexingJobRepository(collection=mock_collection)
    repo_repo = MongoRepositoryRepository(collection=mock_collection)
    now = datetime.now(UTC)

    # Job failure transition accepts expected_status=("acquiring", "scanning")
    mock_collection.find_one_and_update.return_value = {
        "_id": ObjectId(),
        "job_id": "job_789",
        "repository_id": "repo_xyz",
        "owner_session_id": "sess_abc123",
        "status": "failed",
        "current_step": "Acquisition failed",
        "created_at": now,
        "updated_at": now,
        "progress_percentage": 15,
        "error_message": "Acquisition failed safely.",
        "completed_at": now,
    }

    job_result = job_repo.transition_status(
        owner_session_id="sess_abc123",
        job_id="job_789",
        repository_id="repo_xyz",
        expected_status=("acquiring", "scanning"),
        new_status="failed",
        current_step="Acquisition failed",
        progress_percentage=15,
        updated_at=now,
        error_message="Acquisition failed safely.",
        completed_at=now,
    )

    assert job_result is not None
    assert job_result.status == "failed"
    job_call = mock_collection.find_one_and_update.call_args
    assert job_call[0][0]["status"] == {"$in": ["acquiring", "scanning"]}
    assert job_call[1].get("upsert") is False

    # Repository failure transition accepts expected_status=("pending", "indexing")
    mock_collection.find_one_and_update.return_value = {
        "_id": ObjectId(),
        "repository_id": "repo_xyz",
        "owner_session_id": "sess_abc123",
        "name": "test-repo",
        "source_type": "github",
        "status": "failed",
        "created_at": now,
        "updated_at": now,
    }

    repo_result = repo_repo.transition_status(
        owner_session_id="sess_abc123",
        repository_id="repo_xyz",
        expected_status=("pending", "indexing"),
        new_status="failed",
        updated_at=now,
    )

    assert repo_result is not None
    assert repo_result.status == "failed"
    repo_call = mock_collection.find_one_and_update.call_args
    assert repo_call[0][0]["status"] == {"$in": ["pending", "indexing"]}
    assert repo_call[1].get("upsert") is False


def test_job_transition_status_optional_progress(mock_collection: MagicMock) -> None:
    now = datetime.now(UTC)
    mock_collection.find_one_and_update.return_value = {
        "job_id": "job_123",
        "repository_id": "repo_123",
        "owner_session_id": "sess_123",
        "status": "failed",
        "current_step": "Indexing failed",
        "created_at": now,
        "updated_at": now,
        "progress_percentage": 65,
    }
    repo = MongoIndexingJobRepository(collection=mock_collection)

    # 1. None progress omits progress_percentage from $set update document
    res = repo.transition_status(
        owner_session_id="sess_123",
        job_id="job_123",
        repository_id="repo_123",
        expected_status=("parsing", "embedding"),
        new_status="failed",
        current_step="Indexing failed",
        progress_percentage=None,
        updated_at=now,
    )
    assert res is not None
    call_args = mock_collection.find_one_and_update.call_args
    set_doc = call_args[0][1]["$set"]
    assert "progress_percentage" not in set_doc
    assert call_args[1].get("upsert") is False

    # 2. Integer progress includes progress_percentage in $set update document
    repo.transition_status(
        owner_session_id="sess_123",
        job_id="job_123",
        repository_id="repo_123",
        expected_status="parsing",
        new_status="embedding",
        current_step="Generating chunk embeddings",
        progress_percentage=65,
        updated_at=now,
    )
    call_args2 = mock_collection.find_one_and_update.call_args
    set_doc2 = call_args2[0][1]["$set"]
    assert set_doc2["progress_percentage"] == 65

    # 3. Invalid progress values raise StorageDataError
    for bad_p in (True, False, "65", 65.5, -1, 101):
        with pytest.raises(StorageDataError):
            repo.transition_status(
                owner_session_id="sess_123",
                job_id="job_123",
                repository_id="repo_123",
                expected_status="parsing",
                new_status="embedding",
                current_step="Generating chunk embeddings",
                progress_percentage=bad_p,  # type: ignore
                updated_at=now,
            )
