"""Tests for MongoConversationRepository persistence operations, isolation, and error handling."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from sourcetrace.core.exceptions import StorageDataError, StorageOperationError
from sourcetrace.models.domain import ConversationRecord
from sourcetrace.storage.mongo_repositories import MongoConversationRepository


def _make_conversation(
    conv_id: str = "conv_test123",
    repo_id: str = "repo_test123",
    owner_id: str = "sess_owner123",
    title: str = "How does this work?",
) -> ConversationRecord:
    now = datetime.now(UTC)
    return ConversationRecord(
        conversation_id=conv_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        title=title,
        created_at=now,
        updated_at=now,
    )


def test_save_and_get_by_id_matching_owner_repo_conv() -> None:
    mock_collection = MagicMock()
    repo = MongoConversationRepository(collection=mock_collection)

    conv = _make_conversation()

    # Mock find_one returning matching BSON doc
    mock_collection.find_one.return_value = {
        "conversation_id": conv.conversation_id,
        "repository_id": conv.repository_id,
        "owner_session_id": conv.owner_session_id,
        "title": conv.title,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
    }

    saved = repo.save(conv)
    assert saved == conv
    mock_collection.replace_one.assert_called_once()

    fetched = repo.get_by_id(
        owner_session_id=conv.owner_session_id,
        repository_id=conv.repository_id,
        conversation_id=conv.conversation_id,
    )
    assert fetched is not None
    assert fetched.conversation_id == conv.conversation_id
    assert fetched.repository_id == conv.repository_id
    assert fetched.owner_session_id == conv.owner_session_id


def test_get_by_id_returns_none_when_not_found() -> None:
    mock_collection = MagicMock()
    mock_collection.find_one.return_value = None
    repo = MongoConversationRepository(collection=mock_collection)

    res = repo.get_by_id("sess_owner", "repo_1", "conv_missing")
    assert res is None


def test_list_by_repository_filters_and_sorts() -> None:
    mock_collection = MagicMock()
    now = datetime.now(UTC)
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [
        {
            "conversation_id": "conv_2",
            "repository_id": "repo_1",
            "owner_session_id": "sess_1",
            "title": "Question 2",
            "created_at": now,
            "updated_at": now,
        },
        {
            "conversation_id": "conv_1",
            "repository_id": "repo_1",
            "owner_session_id": "sess_1",
            "title": "Question 1",
            "created_at": now,
            "updated_at": now,
        },
    ]
    mock_collection.find.return_value = mock_cursor

    repo = MongoConversationRepository(collection=mock_collection)
    conversations = repo.list_by_repository("sess_1", "repo_1")

    assert len(conversations) == 2
    assert conversations[0].conversation_id == "conv_2"
    assert conversations[1].conversation_id == "conv_1"

    # Verify search query filtered by owner_session_id and repository_id
    mock_collection.find.assert_called_once_with(
        {"owner_session_id": "sess_1", "repository_id": "repo_1"}
    )


def test_delete_conversions_scoped() -> None:
    mock_collection = MagicMock()
    mock_collection.delete_one.return_value.deleted_count = 1
    mock_collection.delete_many.return_value.deleted_count = 3

    repo = MongoConversationRepository(collection=mock_collection)

    deleted = repo.delete("sess_1", "repo_1", "conv_1")
    assert deleted is True
    mock_collection.delete_one.assert_called_once_with(
        {"owner_session_id": "sess_1", "repository_id": "repo_1", "conversation_id": "conv_1"}
    )

    count = repo.delete_by_repository("sess_1", "repo_1")
    assert count == 3
    mock_collection.delete_many.assert_called_once_with(
        {"owner_session_id": "sess_1", "repository_id": "repo_1"}
    )


def test_malformed_document_raises_storage_data_error() -> None:
    mock_collection = MagicMock()
    mock_collection.find_one.return_value = {
        "conversation_id": "conv_1",
        # Missing required fields like repository_id or owner_session_id
    }

    repo = MongoConversationRepository(collection=mock_collection)
    with pytest.raises(StorageDataError):
        repo.get_by_id("sess_1", "repo_1", "conv_1")


def test_storage_exception_converted_to_safe_storage_operation_error() -> None:
    mock_collection = MagicMock()
    mock_collection.find.side_effect = Exception("Mongo connection lost")

    repo = MongoConversationRepository(collection=mock_collection)
    with pytest.raises(StorageOperationError) as exc_info:
        repo.list_by_repository("sess_1", "repo_1")

    assert "Mongo connection lost" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
