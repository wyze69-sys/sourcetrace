"""Tests for MongoMessageRepository persistence operations and error handling."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from sourcetrace.core.exceptions import StorageDataError, StorageOperationError
from sourcetrace.models.domain import (
    CitationRecord,
    EvidenceSnippetRecord,
    MessageRecord,
)
from sourcetrace.storage.mongo_repositories import MongoMessageRepository


def _make_message(
    msg_id: str = "msg_test123",
    conv_id: str = "conv_test123",
    repo_id: str = "repo_test123",
    owner_id: str = "sess_owner123",
    role: str = "user",
    content: str = "What does main.py do?",
    citations: tuple[CitationRecord, ...] = (),
    evidence: tuple[EvidenceSnippetRecord, ...] = (),
    insufficient_evidence: bool = False,
) -> MessageRecord:
    now = datetime.now(UTC)
    return MessageRecord(
        message_id=msg_id,
        conversation_id=conv_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        role=role,
        content=content,
        created_at=now,
        citations=citations,
        evidence=evidence,
        insufficient_evidence=insufficient_evidence,
    )


def test_save_and_list_by_conversation_matching_owner_repo_conv() -> None:
    mock_collection = MagicMock()
    repo = MongoMessageRepository(collection=mock_collection)

    citation = CitationRecord(
        relative_path="src/main.py",
        start_line=1,
        end_line=10,
        symbol_name="main",
        symbol_type="function",
    )
    evidence = EvidenceSnippetRecord(
        snippet="def main(): pass",
        relative_path="src/main.py",
        start_line=1,
        end_line=10,
        symbol_name="main",
        symbol_type="function",
    )
    msg = _make_message(
        role="assistant",
        content="Here is the explanation [E1].",
        citations=(citation,),
        evidence=(evidence,),
    )

    saved = repo.save(msg)
    assert saved == msg
    mock_collection.replace_one.assert_called_once()

    now = datetime.now(UTC)
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [
        {
            "message_id": msg.message_id,
            "conversation_id": msg.conversation_id,
            "repository_id": msg.repository_id,
            "owner_session_id": msg.owner_session_id,
            "role": "assistant",
            "content": msg.content,
            "created_at": now,
            "citations": [
                {
                    "relative_path": "src/main.py",
                    "start_line": 1,
                    "end_line": 10,
                    "symbol_name": "main",
                    "symbol_type": "function",
                }
            ],
            "evidence": [
                {
                    "snippet": "def main(): pass",
                    "relative_path": "src/main.py",
                    "start_line": 1,
                    "end_line": 10,
                    "symbol_name": "main",
                    "symbol_type": "function",
                }
            ],
            "insufficient_evidence": False,
        }
    ]
    mock_collection.find.return_value = mock_cursor

    messages = repo.list_by_conversation(
        owner_session_id=msg.owner_session_id,
        repository_id=msg.repository_id,
        conversation_id=msg.conversation_id,
    )
    assert len(messages) == 1
    assert messages[0].message_id == msg.message_id
    assert messages[0].role == "assistant"
    assert len(messages[0].citations) == 1
    assert messages[0].citations[0].symbol_name == "main"


def test_invalid_role_raises_storage_data_error() -> None:
    mock_collection = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [
        {
            "message_id": "msg_1",
            "conversation_id": "conv_1",
            "repository_id": "repo_1",
            "owner_session_id": "sess_1",
            "role": "system",  # Invalid role for chat message record
            "content": "hello",
            "created_at": datetime.now(UTC),
            "citations": [],
            "evidence": [],
            "insufficient_evidence": False,
        }
    ]
    mock_collection.find.return_value = mock_cursor

    repo = MongoMessageRepository(collection=mock_collection)
    with pytest.raises(StorageDataError):
        repo.list_by_conversation("sess_1", "repo_1", "conv_1")


def test_delete_messages_scoped() -> None:
    mock_collection = MagicMock()
    mock_collection.delete_many.return_value.deleted_count = 2

    repo = MongoMessageRepository(collection=mock_collection)

    count_conv = repo.delete_by_conversation("sess_1", "repo_1", "conv_1")
    assert count_conv == 2
    mock_collection.delete_many.assert_called_with(
        {"owner_session_id": "sess_1", "repository_id": "repo_1", "conversation_id": "conv_1"}
    )

    count_repo = repo.delete_by_repository("sess_1", "repo_1")
    assert count_repo == 2
    mock_collection.delete_many.assert_called_with(
        {"owner_session_id": "sess_1", "repository_id": "repo_1"}
    )


def test_storage_exception_suppresses_mongo_details() -> None:
    mock_collection = MagicMock()
    mock_collection.find.side_effect = Exception("Internal MongoDB socket failure")

    repo = MongoMessageRepository(collection=mock_collection)
    with pytest.raises(StorageOperationError) as exc_info:
        repo.list_by_conversation("sess_1", "repo_1", "conv_1")

    assert "socket failure" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
