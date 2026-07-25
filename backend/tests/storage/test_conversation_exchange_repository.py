"""Tests for MongoConversationExchangeRepository atomic operations and scoped compensation."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from sourcetrace.core.exceptions import StorageDataError, StorageOperationError
from sourcetrace.models.domain import (
    ConversationRecord,
    MessageRecord,
)
from sourcetrace.storage.mongo_repositories import (
    MongoConversationExchangeRepository,
    MongoConversationRepository,
    MongoMessageRepository,
)


def _make_conversation(
    conv_id: str = "conv_exchange123",
    repo_id: str = "repo_exchange123",
    owner_id: str = "sess_owner123",
    title: str = "Exchange test",
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


def _make_messages(
    conv_id: str = "conv_exchange123",
    repo_id: str = "repo_exchange123",
    owner_id: str = "sess_owner123",
) -> tuple[MessageRecord, MessageRecord]:
    now = datetime.now(UTC)
    user_msg = MessageRecord(
        message_id="msg_user123",
        conversation_id=conv_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        role="user",
        content="What is this function?",
        created_at=now,
    )
    assistant_msg = MessageRecord(
        message_id="msg_assistant123",
        conversation_id=conv_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        role="assistant",
        content="This function executes a task.",
        created_at=now + timedelta(microseconds=1),
    )
    return user_msg, assistant_msg


def test_create_conversation_exchange_success() -> None:
    mock_conv_repo = MagicMock(spec=MongoConversationRepository)
    mock_msg_repo = MagicMock(spec=MongoMessageRepository)

    mock_conv_repo.get_by_id.return_value = None  # No pre-existing conversation

    exchange_repo = MongoConversationExchangeRepository(
        conv_repo=mock_conv_repo, msg_repo=mock_msg_repo
    )

    conv = _make_conversation()
    user_msg, assistant_msg = _make_messages()

    exchange_repo.create_conversation_exchange(conv, user_msg, assistant_msg)

    mock_conv_repo.save.assert_called_once_with(conv)
    assert mock_msg_repo.save.call_count == 2


def test_create_conversation_exchange_invalid_record_types_raises_storage_data_error() -> None:
    mock_conv_repo = MagicMock(spec=MongoConversationRepository)
    mock_msg_repo = MagicMock(spec=MongoMessageRepository)

    exchange_repo = MongoConversationExchangeRepository(
        conv_repo=mock_conv_repo, msg_repo=mock_msg_repo
    )

    conv = _make_conversation()
    user_msg, assistant_msg = _make_messages()

    # Pass invalid user message type
    with pytest.raises(StorageDataError):
        exchange_repo.create_conversation_exchange(conv, "not a message", assistant_msg)  # type: ignore


def test_create_conversation_exchange_mismatched_owner_ids_raises_storage_data_error() -> None:
    mock_conv_repo = MagicMock(spec=MongoConversationRepository)
    mock_msg_repo = MagicMock(spec=MongoMessageRepository)

    exchange_repo = MongoConversationExchangeRepository(
        conv_repo=mock_conv_repo, msg_repo=mock_msg_repo
    )

    conv = _make_conversation(owner_id="sess_owner123")
    user_msg, assistant_msg = _make_messages(owner_id="sess_other_owner")

    with pytest.raises(StorageDataError):
        exchange_repo.create_conversation_exchange(conv, user_msg, assistant_msg)


def test_create_conversation_exchange_compensation_on_assistant_save_failure() -> None:
    mock_conv_repo = MagicMock(spec=MongoConversationRepository)
    mock_msg_repo = MagicMock(spec=MongoMessageRepository)

    mock_conv_repo.get_by_id.return_value = None

    # Simulate assistant message save failure
    mock_msg_repo.save.side_effect = [
        None,  # user_message save succeeds
        Exception("Mongo write error on assistant message"),  # assistant_message fails
    ]

    exchange_repo = MongoConversationExchangeRepository(
        conv_repo=mock_conv_repo, msg_repo=mock_msg_repo
    )

    conv = _make_conversation()
    user_msg, assistant_msg = _make_messages()

    with pytest.raises(StorageOperationError) as exc_info:
        exchange_repo.create_conversation_exchange(conv, user_msg, assistant_msg)

    assert "Failed to save assistant message during exchange" in str(exc_info.value)
    # Check that compensation deleted the user message and conversation
    mock_msg_repo.delete_by_conversation.assert_called_once_with(
        conv.owner_session_id, conv.repository_id, conv.conversation_id
    )
    mock_conv_repo.delete.assert_called_once_with(
        conv.owner_session_id, conv.repository_id, conv.conversation_id
    )


def test_append_message_exchange_success() -> None:
    mock_conv_repo = MagicMock(spec=MongoConversationRepository)
    mock_msg_repo = MagicMock(spec=MongoMessageRepository)

    conv = _make_conversation()
    mock_conv_repo.get_by_id.return_value = conv
    mock_msg_repo.list_by_conversation.return_value = []

    exchange_repo = MongoConversationExchangeRepository(
        conv_repo=mock_conv_repo, msg_repo=mock_msg_repo
    )

    user_msg, assistant_msg = _make_messages()
    updated_conv = _make_conversation(title="Exchange test updated")

    exchange_repo.append_message_exchange(updated_conv, user_msg, assistant_msg)

    mock_conv_repo.save.assert_called_once_with(updated_conv)
    assert mock_msg_repo.save.call_count == 2


def test_append_message_exchange_compensation_on_assistant_save_failure() -> None:
    mock_conv_repo = MagicMock(spec=MongoConversationRepository)
    mock_msg_repo = MagicMock(spec=MongoMessageRepository)

    original_conv = _make_conversation(title="Original title")
    mock_conv_repo.get_by_id.return_value = original_conv
    mock_msg_repo.list_by_conversation.return_value = []

    # Mock user message save succeeds, assistant save fails
    mock_msg_repo.save.side_effect = [
        None,
        Exception("Mongo write error on assistant append"),
    ]

    exchange_repo = MongoConversationExchangeRepository(
        conv_repo=mock_conv_repo, msg_repo=mock_msg_repo
    )

    user_msg, assistant_msg = _make_messages()
    updated_conv = _make_conversation(title="Updated title")

    with pytest.raises(StorageOperationError):
        exchange_repo.append_message_exchange(updated_conv, user_msg, assistant_msg)

    # Check that original conversation state was restored via save
    mock_conv_repo.save.assert_called_with(original_conv)
