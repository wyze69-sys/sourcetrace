"""Typed repository interfaces for SourceTrace persistence operations."""

from datetime import datetime
from typing import Protocol

from sourcetrace.models.domain import (
    AnonymousSession,
    CodeChunk,
    ConversationRecord,
    IndexingJobRecord,
    MessageRecord,
    RepositoryRecord,
    RetrievalResult,
)


class AnonymousSessionRepository(Protocol):
    """Persistence interface for anonymous session records."""

    def get_by_id(self, owner_session_id: str) -> AnonymousSession | None: ...

    def save(self, session: AnonymousSession) -> AnonymousSession: ...

    def delete(self, owner_session_id: str) -> bool: ...

    def reserve_repository_slot(
        self,
        owner_session_id: str,
        now: datetime,
        max_quota: int = 3,
        retention_days: int = 7,
    ) -> AnonymousSession | None: ...

    def release_repository_slot(
        self,
        owner_session_id: str,
    ) -> bool: ...


class RepositoryRepository(Protocol):
    """Persistence interface for indexed repository metadata."""

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None: ...

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]: ...

    def count_by_owner(self, owner_session_id: str) -> int: ...

    def save(self, repository: RepositoryRecord) -> RepositoryRecord: ...

    def transition_status(
        self,
        owner_session_id: str,
        repository_id: str,
        expected_status: str | tuple[str, ...],
        new_status: str,
        updated_at: datetime,
        file_count: int | None = None,
        chunk_count: int | None = None,
        indexed_branch: str | None = None,
        indexed_commit_sha: str | None = None,
        last_indexed_at: datetime | None = None,
        parser_versions: tuple[str, ...] | list[str] | None = None,
        flow_evidence_complete: bool | None = None,
        indexed_file_count: int | None = None,
        indexed_chunk_count: int | None = None,
        active_generation_id: str | None = None,
    ) -> RepositoryRecord | None: ...

    def update_active_generation(
        self,
        owner_session_id: str,
        repository_id: str,
        active_generation_id: str,
        updated_at: datetime,
        indexed_branch: str | None = None,
        indexed_commit_sha: str | None = None,
        file_count: int | None = None,
        chunk_count: int | None = None,
        indexed_file_count: int | None = None,
        indexed_chunk_count: int | None = None,
        parser_versions: tuple[str, ...] | list[str] | None = None,
        flow_evidence_complete: bool | None = None,
        consecutive_refresh_failures: int | None = None,
        is_stale: bool | None = None,
    ) -> RepositoryRecord | None: ...

    def update_staleness(
        self,
        owner_session_id: str,
        repository_id: str,
        is_stale: bool,
        stale_checked_at: datetime,
    ) -> RepositoryRecord | None: ...

    def delete(self, owner_session_id: str, repository_id: str) -> bool: ...


class IndexingJobRepository(Protocol):
    """Persistence interface for indexing job status records."""

    def get_by_id(self, owner_session_id: str, job_id: str) -> IndexingJobRecord | None: ...

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None: ...

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord: ...

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
    ) -> IndexingJobRecord | None: ...

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int: ...

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[IndexingJobRecord]: ...


class CodeChunkRepository(Protocol):
    """Persistence interface for AST code chunks and vector search."""

    def save_many(self, chunks: list[CodeChunk]) -> int: ...

    def list_by_repository(
        self,
        owner_session_id: str,
        repository_id: str,
        generation_id: str | None = None,
        limit: int | None = None,
    ) -> list[CodeChunk]: ...

    def migrate_legacy_generation(
        self, owner_session_id: str, repository_id: str, target_generation_id: str
    ) -> int: ...

    def search_vectors(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: list[float],
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]: ...

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]: ...

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int: ...

    def delete_by_generation(
        self, owner_session_id: str, repository_id: str, generation_id: str | None
    ) -> int: ...


class ConversationRepository(Protocol):
    """Persistence interface for chat conversation threads."""

    def get_by_id(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> ConversationRecord | None: ...

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[ConversationRecord]: ...

    def save(self, conversation: ConversationRecord) -> ConversationRecord: ...

    def delete(self, owner_session_id: str, repository_id: str, conversation_id: str) -> bool: ...

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int: ...


class MessageRepository(Protocol):
    """Persistence interface for chat messages."""

    def list_by_conversation(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> list[MessageRecord]: ...

    def save(self, message: MessageRecord) -> MessageRecord: ...

    def delete_by_conversation(
        self, owner_session_id: str, repository_id: str, conversation_id: str
    ) -> int: ...

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int: ...


class ConversationExchangeRepository(Protocol):
    """Persistence interface for atomic conversation and message exchanges."""

    def create_conversation_exchange(
        self,
        conversation: ConversationRecord,
        user_message: MessageRecord,
        assistant_message: MessageRecord,
    ) -> None: ...

    def append_message_exchange(
        self,
        updated_conversation: ConversationRecord,
        user_message: MessageRecord,
        assistant_message: MessageRecord,
    ) -> None: ...
