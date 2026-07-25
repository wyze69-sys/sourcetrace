"""Tests for chat service routing, ownership checks, and bounded history handling."""

from datetime import UTC, datetime

from sourcetrace.generation.client import GenerationMessage
from sourcetrace.models.domain import (
    ConversationRecord,
    MessageRecord,
)


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], ConversationRecord] = {}

    def get_by_id(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> ConversationRecord | None:
        return self.items.get((owner_session_id, repository_id, conversation_id))

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[ConversationRecord]:
        res = [
            c
            for (owner, repo, _), c in self.items.items()
            if owner == owner_session_id and repo == repository_id
        ]
        return sorted(res, key=lambda c: c.created_at, reverse=True)

    def save(self, conversation: ConversationRecord) -> ConversationRecord:
        key = (
            conversation.owner_session_id,
            conversation.repository_id,
            conversation.conversation_id,
        )
        self.items[key] = conversation
        return conversation

    def delete(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> bool:
        key = (owner_session_id, repository_id, conversation_id)
        return self.items.pop(key, None) is not None

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        keys = [
            k
            for k in self.items
            if k[0] == owner_session_id and k[1] == repository_id
        ]
        for k in keys:
            del self.items[k]
        return len(keys)


class InMemoryMessageRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str, str], MessageRecord] = {}

    def list_by_conversation(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> list[MessageRecord]:
        res = [
            m
            for (owner, repo, conv, _), m in self.items.items()
            if owner == owner_session_id and repo == repository_id and conv == conversation_id
        ]
        return sorted(res, key=lambda m: (m.created_at, m.message_id))

    def save(self, message: MessageRecord) -> MessageRecord:
        key = (
            message.owner_session_id,
            message.repository_id,
            message.conversation_id,
            message.message_id,
        )
        self.items[key] = message
        return message

    def delete_by_conversation(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> int:
        keys = [
            k
            for k in self.items
            if k[0] == owner_session_id and k[1] == repository_id and k[2] == conversation_id
        ]
        for k in keys:
            del self.items[k]
        return len(keys)

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        keys = [
            k
            for k in self.items
            if k[0] == owner_session_id and k[1] == repository_id
        ]
        for k in keys:
            del self.items[k]
        return len(keys)


def test_build_generation_history_bounds_messages_and_characters() -> None:
    from sourcetrace.api.routes.conversations import build_generation_history

    now = datetime.now(UTC)
    messages = [
        MessageRecord(
            message_id=f"msg_{i}",
            conversation_id="conv_1",
            repository_id="repo_1",
            owner_session_id="sess_1",
            role="user" if i % 2 == 0 else "assistant",
            content=f"Message {i} " + ("x" * 100),
            created_at=now,
        )
        for i in range(15)
    ]

    history = build_generation_history(messages, max_messages=6, max_characters=300)
    assert len(history) <= 6
    total_chars = sum(len(m.content) for m in history)
    assert total_chars <= 300
    assert all(isinstance(m, GenerationMessage) for m in history)
