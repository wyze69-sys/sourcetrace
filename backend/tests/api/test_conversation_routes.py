"""Offline API tests for chat routes, ownership verification, and error contracts."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_conversation_exchange_repository,
    get_conversation_repository,
    get_current_owner_id,
    get_grounded_answer_service,
    get_message_repository,
    get_repository_repository,
    get_session_repository,
    get_session_signer,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import GenerationError, StorageOperationError
from sourcetrace.core.security import SessionSigner
from sourcetrace.generation.client import GenerationMessage
from sourcetrace.models.domain import (
    AnonymousSession,
    CitationRecord,
    ConversationRecord,
    EvidenceSnippetRecord,
    GroundedAnswerResult,
    MessageRecord,
    RepositoryRecord,
)

TEST_SECRET = "a_very_secret_key_that_is_at_least_32_bytes_long!"


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

    def delete(self, owner_session_id: str, repository_id: str, conversation_id: str) -> bool:
        key = (owner_session_id, repository_id, conversation_id)
        return self.items.pop(key, None) is not None

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        keys = [k for k in self.items if k[0] == owner_session_id and k[1] == repository_id]
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
        keys = [k for k in self.items if k[0] == owner_session_id and k[1] == repository_id]
        for k in keys:
            del self.items[k]
        return len(keys)


class InMemoryConversationExchangeRepository:
    def __init__(
        self,
        conv_repo: InMemoryConversationRepository,
        msg_repo: InMemoryMessageRepository,
        fail_on_create: bool = False,
        fail_on_append: bool = False,
    ) -> None:
        self.conv_repo = conv_repo
        self.msg_repo = msg_repo
        self.fail_on_create = fail_on_create
        self.fail_on_append = fail_on_append
        self.created_exchanges: list[tuple[ConversationRecord, MessageRecord, MessageRecord]] = []
        self.appended_exchanges: list[tuple[ConversationRecord, MessageRecord, MessageRecord]] = []

    def create_conversation_exchange(
        self,
        conversation: ConversationRecord,
        user_message: MessageRecord,
        assistant_message: MessageRecord,
    ) -> None:
        if self.fail_on_create:
            raise StorageOperationError("Exchange creation failed safely.")
        self.conv_repo.save(conversation)
        self.msg_repo.save(user_message)
        self.msg_repo.save(assistant_message)
        self.created_exchanges.append((conversation, user_message, assistant_message))

    def append_message_exchange(
        self,
        updated_conversation: ConversationRecord,
        user_message: MessageRecord,
        assistant_message: MessageRecord,
    ) -> None:
        if self.fail_on_append:
            raise StorageOperationError("Exchange append failed safely.")
        self.conv_repo.save(updated_conversation)
        self.msg_repo.save(user_message)
        self.msg_repo.save(assistant_message)
        self.appended_exchanges.append((updated_conversation, user_message, assistant_message))


class InMemoryRepositoryRepository:
    def __init__(self, repositories: list[RepositoryRecord] | None = None) -> None:
        self.repos: dict[tuple[str, str], RepositoryRecord] = {
            (r.owner_session_id, r.repository_id): r for r in (repositories or [])
        }

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.repos.get((owner_session_id, repository_id))

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for (owner, _), r in self.repos.items() if owner == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        self.repos[(repository.owner_session_id, repository.repository_id)] = repository
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
        repo = self.get_by_id(owner_session_id, repository_id)
        if not repo:
            return None
        expected = (expected_status,) if isinstance(expected_status, str) else expected_status
        if repo.status not in expected:
            return None
        updated = RepositoryRecord(
            repository_id=repo.repository_id,
            owner_session_id=repo.owner_session_id,
            name=repo.name,
            source_type=repo.source_type,
            status=new_status,
            created_at=repo.created_at,
            updated_at=updated_at,
            github_url=repo.github_url,
            file_count=file_count if file_count is not None else repo.file_count,
            chunk_count=chunk_count if chunk_count is not None else repo.chunk_count,
        )
        self.repos[(owner_session_id, repository_id)] = updated
        return updated

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        return self.repos.pop((owner_session_id, repository_id), None) is not None


class InMemoryAnonymousSessionRepo:
    def __init__(self, session: AnonymousSession, fail_save: bool = False) -> None:
        self.session = session
        self.saved_sessions: list[AnonymousSession] = []
        self.fail_save = fail_save

    def get_by_id(self, owner_session_id: str) -> AnonymousSession | None:
        if self.session.owner_session_id == owner_session_id:
            return self.session
        return None

    def save(self, session: AnonymousSession) -> AnonymousSession:
        if self.fail_save:
            raise StorageOperationError("Session save failed safely.")
        self.session = session
        self.saved_sessions.append(session)
        return session

    def delete(self, owner_session_id: str) -> bool:
        return True

    def reserve_repository_slot(
        self,
        owner_session_id: str,
        now: datetime,
        max_quota: int = 3,
        retention_days: int = 7,
    ) -> AnonymousSession | None:
        return self.session

    def release_repository_slot(self, owner_session_id: str) -> bool:
        return True


class StrictFakeGroundedAnswerService:
    """Strict fake GroundedAnswerService that enforces exact parameter signatures."""

    def __init__(
        self,
        result: GroundedAnswerResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or GroundedAnswerResult(
            answer="Strict grounded answer [E1].",
            citations=(
                CitationRecord(
                    relative_path="main.py",
                    start_line=1,
                    end_line=10,
                    symbol_name="run",
                    symbol_type="function",
                ),
            ),
            evidence=(
                EvidenceSnippetRecord(
                    snippet="def run(): pass",
                    relative_path="main.py",
                    start_line=1,
                    end_line=10,
                    symbol_name="run",
                    symbol_type="function",
                ),
            ),
            insufficient_evidence=False,
            chunks_retrieved=1,
        )
        self.error = error
        self.last_conversation_context: Sequence[GenerationMessage] | None = None
        self.call_count = 0

    def generate_answer(
        self,
        owner_session_id: str,
        repository_id: str,
        question: str,
        *,
        limit: int = 5,
        conversation_context: Sequence[GenerationMessage] | None = None,
    ) -> GroundedAnswerResult:
        self.call_count += 1
        self.last_conversation_context = conversation_context
        if self.error:
            raise self.error
        return self.result


def _setup_app_and_client(
    repo_record: RepositoryRecord | None = None,
    grounded_result: GroundedAnswerResult | None = None,
    grounded_error: Exception | None = None,
    fail_exchange_create: bool = False,
    fail_exchange_append: bool = False,
    fail_session_save: bool = False,
):
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_test_owner"

    session = AnonymousSession(
        owner_session_id=owner_id,
        last_active_at=now,
        expires_at=exp,
        created_at=now,
        updated_at=now,
    )

    if repo_record is None:
        repo_record = RepositoryRecord(
            repository_id="repo_ready123",
            owner_session_id=owner_id,
            name="test-repo",
            source_type="github",
            status="ready",
            created_at=now,
            updated_at=now,
        )

    app = create_app()
    settings_obj = Settings(env="development", session_signing_secret=SecretStr(TEST_SECRET))

    session_repo = InMemoryAnonymousSessionRepo(session, fail_save=fail_session_save)
    repository_repo = InMemoryRepositoryRepository([repo_record])
    conversation_repo = InMemoryConversationRepository()
    message_repo = InMemoryMessageRepository()
    exchange_repo = InMemoryConversationExchangeRepository(
        conv_repo=conversation_repo,
        msg_repo=message_repo,
        fail_on_create=fail_exchange_create,
        fail_on_append=fail_exchange_append,
    )

    strict_answer_service = StrictFakeGroundedAnswerService(
        result=grounded_result, error=grounded_error
    )

    app.dependency_overrides[get_settings] = lambda: settings_obj
    app.dependency_overrides[get_current_owner_id] = lambda: owner_id
    app.dependency_overrides[get_session_signer] = lambda: SessionSigner(TEST_SECRET)
    app.dependency_overrides[get_session_repository] = lambda: session_repo
    app.dependency_overrides[get_repository_repository] = lambda: repository_repo
    app.dependency_overrides[get_conversation_repository] = lambda: conversation_repo
    app.dependency_overrides[get_message_repository] = lambda: message_repo
    app.dependency_overrides[get_conversation_exchange_repository] = lambda: exchange_repo
    app.dependency_overrides[get_grounded_answer_service] = lambda: strict_answer_service

    client = TestClient(app, raise_server_exceptions=False)
    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    return (
        client,
        owner_id,
        repo_record.repository_id,
        session_repo,
        conversation_repo,
        message_repo,
        exchange_repo,
        strict_answer_service,
    )


def test_create_conversation_success_returns_201() -> None:
    (
        client,
        owner_id,
        repo_id,
        session_repo,
        conv_repo,
        msg_repo,
        exchange_repo,
        answer_service,
    ) = _setup_app_and_client()

    res = client.post(
        f"/api/v1/repositories/{repo_id}/conversations",
        json={"question": "How does main function work?"},
    )

    assert res.status_code == 201
    body = res.json()
    assert "conversation_id" in body
    assert body["conversation_id"].startswith("conv_")
    assert body["repository_id"] == repo_id
    assert body["user_message"]["role"] == "user"
    assert body["user_message"]["content"] == "How does main function work?"
    assert body["assistant_message"]["role"] == "assistant"
    assert "Strict grounded answer" in body["assistant_message"]["content"]
    assert len(body["assistant_message"]["citations"]) == 1
    assert body["request_metadata"]["latency_ms"] >= 0
    assert body["request_metadata"]["chunks_retrieved"] == 1

    # Check that session activity updated
    assert len(session_repo.saved_sessions) > 0
    assert len(exchange_repo.created_exchanges) == 1


def test_create_conversation_missing_or_not_ready_repository_returns_404() -> None:
    now = datetime.now(UTC)
    not_ready_repo = RepositoryRecord(
        repository_id="repo_indexing",
        owner_session_id="sess_test_owner",
        name="test-repo",
        source_type="github",
        status="indexing",
        created_at=now,
        updated_at=now,
    )
    client, _, _, _, _, _, _, _ = _setup_app_and_client(repo_record=not_ready_repo)

    res = client.post(
        f"/api/v1/repositories/{not_ready_repo.repository_id}/conversations",
        json={"question": "What is in this repo?"},
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    res_missing = client.post(
        "/api/v1/repositories/repo_nonexistent/conversations",
        json={"question": "What is in this repo?"},
    )
    assert res_missing.status_code == 404


def test_create_conversation_invalid_question_returns_422() -> None:
    client, _, repo_id, _, _, _, _, _ = _setup_app_and_client()

    # Empty question
    res1 = client.post(
        f"/api/v1/repositories/{repo_id}/conversations",
        json={"question": "   "},
    )
    assert res1.status_code == 422
    assert res1.json()["error"]["code"] == "VALIDATION_ERROR"

    # NUL char question
    res2 = client.post(
        f"/api/v1/repositories/{repo_id}/conversations",
        json={"question": "Bad \x00 question"},
    )
    assert res2.status_code == 422


def test_get_conversation_history_returns_200() -> None:
    client, owner_id, repo_id, session_repo, conv_repo, msg_repo, _, _ = _setup_app_and_client()

    now = datetime.now(UTC)
    conv = ConversationRecord(
        conversation_id="conv_100",
        repository_id=repo_id,
        owner_session_id=owner_id,
        title="Main question",
        created_at=now,
        updated_at=now,
    )
    conv_repo.save(conv)

    msg1 = MessageRecord(
        message_id="msg_101",
        conversation_id="conv_100",
        repository_id=repo_id,
        owner_session_id=owner_id,
        role="user",
        content="What is run?",
        created_at=now,
    )
    msg2 = MessageRecord(
        message_id="msg_102",
        conversation_id="conv_100",
        repository_id=repo_id,
        owner_session_id=owner_id,
        role="assistant",
        content="Run executes the process.",
        created_at=now + timedelta(seconds=1),
    )
    msg_repo.save(msg1)
    msg_repo.save(msg2)

    saved_sessions_before = len(session_repo.saved_sessions)

    res = client.get(f"/api/v1/repositories/{repo_id}/conversations/conv_100")
    assert res.status_code == 200
    body = res.json()
    assert body["conversation_id"] == "conv_100"
    assert body["title"] == "Main question"
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][1]["role"] == "assistant"

    # Reading history must NOT update session activity
    assert len(session_repo.saved_sessions) == saved_sessions_before


def test_get_conversation_wrong_owner_or_missing_returns_404() -> None:
    client, _, repo_id, _, conv_repo, _, _, _ = _setup_app_and_client()

    now = datetime.now(UTC)
    other_conv = ConversationRecord(
        conversation_id="conv_other",
        repository_id=repo_id,
        owner_session_id="sess_other_user",
        title="Other user chat",
        created_at=now,
        updated_at=now,
    )
    conv_repo.save(other_conv)

    # Wrong owner conversation
    res = client.get(f"/api/v1/repositories/{repo_id}/conversations/conv_other")
    assert res.status_code == 404

    # Nonexistent conversation
    res2 = client.get(f"/api/v1/repositories/{repo_id}/conversations/conv_nonexistent")
    assert res2.status_code == 404


def test_send_message_success_returns_200_and_uses_conversation_context_keyword() -> None:
    client, owner_id, repo_id, session_repo, conv_repo, msg_repo, exchange_repo, answer_service = (
        _setup_app_and_client()
    )

    now = datetime.now(UTC)
    conv = ConversationRecord(
        conversation_id="conv_200",
        repository_id=repo_id,
        owner_session_id=owner_id,
        title="First question",
        created_at=now,
        updated_at=now,
    )
    conv_repo.save(conv)

    msg1 = MessageRecord(
        message_id="msg_prior_user",
        conversation_id="conv_200",
        repository_id=repo_id,
        owner_session_id=owner_id,
        role="user",
        content="Prior question",
        created_at=now,
    )
    msg_repo.save(msg1)

    res = client.post(
        f"/api/v1/repositories/{repo_id}/conversations/conv_200/messages",
        json={"question": "And where is it called?"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["conversation_id"] == "conv_200"
    assert body["user_message"]["content"] == "And where is it called?"
    assert body["assistant_message"]["role"] == "assistant"
    assert body["request_metadata"]["latency_ms"] >= 0

    # Verify conversation_context keyword was used and contained prior message
    assert answer_service.last_conversation_context is not None
    assert len(answer_service.last_conversation_context) == 1
    assert answer_service.last_conversation_context[0].content == "Prior question"
    assert len(exchange_repo.appended_exchanges) == 1


def test_send_message_missing_conversation_returns_404() -> None:
    client, _, repo_id, _, _, _, _, _ = _setup_app_and_client()

    res = client.post(
        f"/api/v1/repositories/{repo_id}/conversations/conv_missing/messages",
        json={"question": "Hello?"},
    )
    assert res.status_code == 404


def test_generation_failure_returns_500_and_does_not_persist() -> None:
    client, owner_id, repo_id, _, conv_repo, msg_repo, _, answer_service = _setup_app_and_client(
        grounded_error=GenerationError("LLM service unavailable")
    )

    now = datetime.now(UTC)
    conv = ConversationRecord(
        conversation_id="conv_300",
        repository_id=repo_id,
        owner_session_id=owner_id,
        title="Fail test",
        created_at=now,
        updated_at=now,
    )
    conv_repo.save(conv)

    res = client.post(
        f"/api/v1/repositories/{repo_id}/conversations/conv_300/messages",
        json={"question": "Fail question"},
    )
    assert res.status_code == 500
    assert res.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }

    # Messages must not be persisted on generation failure
    assert len(msg_repo.list_by_conversation(owner_id, repo_id, "conv_300")) == 0


def test_exchange_persistence_failure_returns_500() -> None:
    client, owner_id, repo_id, _, conv_repo, msg_repo, _, _ = _setup_app_and_client(
        fail_exchange_create=True
    )

    res = client.post(
        f"/api/v1/repositories/{repo_id}/conversations",
        json={"question": "Question during exchange fail"},
    )
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "INTERNAL_ERROR"

    # No conversation or messages stored
    assert len(conv_repo.items) == 0
    assert len(msg_repo.items) == 0


def test_session_activity_failure_does_not_cause_500_when_exchange_succeeds() -> None:
    client, owner_id, repo_id, _, conv_repo, msg_repo, exchange_repo, _ = _setup_app_and_client(
        fail_session_save=True
    )

    res = client.post(
        f"/api/v1/repositories/{repo_id}/conversations",
        json={"question": "Question with session save failure"},
    )
    assert res.status_code == 201
    body = res.json()
    assert "conversation_id" in body
    assert len(exchange_repo.created_exchanges) == 1
