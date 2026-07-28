"""Offline unit and integration tests for DELETE /api/v1/repositories/{repository_id} route."""

from datetime import UTC, datetime, timedelta

from fastapi import status
from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_code_chunk_repository,
    get_conversation_repository,
    get_indexing_job_repository,
    get_message_repository,
    get_repository_repository,
    get_session_repository,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import JWTSigner
from sourcetrace.models.domain import (
    AnonymousSession,
    CodeChunk,
    ConversationRecord,
    IndexingJobRecord,
    MessageRecord,
    RepositoryRecord,
)

NOW = datetime.now(UTC)
TEST_SECRET = "sk-test-secret-key-32-chars-long-string-min"


class InMemoryAnonymousSessionRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, AnonymousSession] = {}
        self.release_called_count = 0

    def get_by_id(self, owner_session_id: str) -> AnonymousSession | None:
        return self.sessions.get(owner_session_id)

    def save(self, session: AnonymousSession) -> AnonymousSession:
        self.sessions[session.owner_session_id] = session
        return session

    def release_repository_slot(self, owner_session_id: str) -> bool:
        sess = self.sessions.get(owner_session_id)
        if sess and sess.active_repository_count > 0:
            import dataclasses

            updated = dataclasses.replace(
                sess, active_repository_count=sess.active_repository_count - 1
            )
            self.sessions[owner_session_id] = updated
            self.release_called_count += 1
            return True
        return False


class InMemoryRepositoryRepository:
    def __init__(self) -> None:
        self.repos: dict[tuple[str, str], RepositoryRecord] = {}

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.repos.get((owner_session_id, repository_id))

    def save(self, record: RepositoryRecord) -> RepositoryRecord:
        self.repos[(record.owner_session_id, record.repository_id)] = record
        return record

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        return self.repos.pop((owner_session_id, repository_id), None) is not None


class InMemoryIndexingJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], IndexingJobRecord] = {}

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        return self.jobs.get((owner_session_id, repository_id))

    def save(self, record: IndexingJobRecord) -> IndexingJobRecord:
        self.jobs[(record.owner_session_id, record.repository_id)] = record
        return record

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        keys_to_del = [k for k in self.jobs if k[0] == owner_session_id and k[1] == repository_id]
        for k in keys_to_del:
            del self.jobs[k]
        return len(keys_to_del)


class InMemoryCodeChunkRepository:
    def __init__(self) -> None:
        self.chunks: list[CodeChunk] = []

    def save(self, chunk: CodeChunk) -> CodeChunk:
        self.chunks.append(chunk)
        return chunk

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        before = len(self.chunks)
        self.chunks = [
            c
            for c in self.chunks
            if not (c.owner_session_id == owner_session_id and c.repository_id == repository_id)
        ]
        return before - len(self.chunks)


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self.convs: list[ConversationRecord] = []

    def save(self, conv: ConversationRecord) -> ConversationRecord:
        self.convs.append(conv)
        return conv

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        before = len(self.convs)
        self.convs = [
            c
            for c in self.convs
            if not (c.owner_session_id == owner_session_id and c.repository_id == repository_id)
        ]
        return before - len(self.convs)


class InMemoryMessageRepository:
    def __init__(self) -> None:
        self.msgs: list[MessageRecord] = []

    def save(self, msg: MessageRecord) -> MessageRecord:
        self.msgs.append(msg)
        return msg

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        before = len(self.msgs)
        self.msgs = [
            m
            for m in self.msgs
            if not (m.owner_session_id == owner_session_id and m.repository_id == repository_id)
        ]
        return before - len(self.msgs)


def _make_auth_header(owner_session_id: str, settings: Settings) -> dict[str, str]:
    jwt_signer = JWTSigner(settings=settings)
    token = jwt_signer.create_access_token(owner_session_id)
    return {"Authorization": f"Bearer {token}"}


def test_delete_repository_success_and_quota_recovery(monkeypatch) -> None:
    """Deleting a repository removes repo, chunks, jobs, conversations, and quota slot."""
    settings = Settings(
        session_signing_secret=SecretStr(TEST_SECRET),
        jwt_secret=SecretStr(TEST_SECRET),
    )
    monkeypatch.setattr("sourcetrace.api.app.get_settings", lambda: settings)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    chunk_repo = InMemoryCodeChunkRepository()
    conv_repo = InMemoryConversationRepository()
    msg_repo = InMemoryMessageRepository()
    sess_repo = InMemoryAnonymousSessionRepository()

    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: chunk_repo
    app.dependency_overrides[get_conversation_repository] = lambda: conv_repo
    app.dependency_overrides[get_message_repository] = lambda: msg_repo
    app.dependency_overrides[get_session_repository] = lambda: sess_repo

    owner_id = "sess_owner_delete_001"
    repo_id = "repo_del_001"

    sess_repo.save(
        AnonymousSession(
            owner_session_id=owner_id,
            last_active_at=NOW,
            expires_at=NOW + timedelta(days=7),
            created_at=NOW,
            updated_at=NOW,
            active_repository_count=3,
        )
    )

    repo_repo.save(
        RepositoryRecord(
            repository_id=repo_id,
            owner_session_id=owner_id,
            name="DeleteRepoTest",
            source_type="github",
            status="ready",
            created_at=NOW,
            updated_at=NOW,
            file_count=5,
            chunk_count=10,
        )
    )

    job_repo.save(
        IndexingJobRecord(
            job_id="job_del_001",
            repository_id=repo_id,
            owner_session_id=owner_id,
            status="ready",
            progress_percentage=100,
            current_step="Done",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    chunk_repo.save(
        CodeChunk(
            chunk_id="chunk_del_001",
            repository_id=repo_id,
            owner_session_id=owner_id,
            relative_path="src/main.py",
            language="python",
            symbol_name="main",
            symbol_type="function",
            start_line=1,
            end_line=10,
            content="def main(): pass",
            content_hash="h123",
            parser_version="v1",
            created_at=NOW,
            generation_id="gen_v1",
        )
    )

    conv_repo.save(
        ConversationRecord(
            conversation_id="conv_del_001",
            repository_id=repo_id,
            owner_session_id=owner_id,
            title="Test Conv",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    msg_repo.save(
        MessageRecord(
            message_id="msg_del_001",
            conversation_id="conv_del_001",
            repository_id=repo_id,
            owner_session_id=owner_id,
            role="user",
            content="Hello",
            created_at=NOW,
        )
    )

    client = TestClient(app)
    headers = _make_auth_header(owner_id, settings)

    resp = client.delete(f"/api/v1/repositories/{repo_id}", headers=headers)
    assert resp.status_code == status.HTTP_200_OK

    body = resp.json()
    assert body["repository_id"] == repo_id
    assert "deleted successfully" in body["message"].lower()

    # Verify all records deleted
    assert repo_repo.get_by_id(owner_id, repo_id) is None
    assert job_repo.get_by_repository(owner_id, repo_id) is None
    assert len(chunk_repo.chunks) == 0
    assert len(conv_repo.convs) == 0
    assert len(msg_repo.msgs) == 0

    # Verify session active_repository_count decremented to 2
    updated_sess = sess_repo.get_by_id(owner_id)
    assert updated_sess is not None
    assert updated_sess.active_repository_count == 2
    assert sess_repo.release_called_count == 1


def test_delete_repository_nonexistent_returns_404(monkeypatch) -> None:
    """Attempting to delete a missing repository returns HTTP 404 Not Found."""
    settings = Settings(
        session_signing_secret=SecretStr(TEST_SECRET),
        jwt_secret=SecretStr(TEST_SECRET),
    )
    monkeypatch.setattr("sourcetrace.api.app.get_settings", lambda: settings)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_repository_repository] = lambda: InMemoryRepositoryRepository()

    client = TestClient(app)
    headers = _make_auth_header("sess_user_001", settings)

    resp = client.delete("/api/v1/repositories/repo_nonexistent", headers=headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["error"]["message"] == "The requested resource was not found."


def test_delete_repository_non_owned_returns_404(monkeypatch) -> None:
    """Deleting a repository owned by another session returns uniform HTTP 404."""
    settings = Settings(
        session_signing_secret=SecretStr(TEST_SECRET),
        jwt_secret=SecretStr(TEST_SECRET),
    )
    monkeypatch.setattr("sourcetrace.api.app.get_settings", lambda: settings)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    repo_repo = InMemoryRepositoryRepository()
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo

    repo_rec = RepositoryRecord(
        repository_id="repo_other_001",
        owner_session_id="sess_owner_A",
        name="OtherRepo",
        source_type="github",
        status="ready",
        created_at=NOW,
        updated_at=NOW,
    )
    repo_repo.save(repo_rec)

    client = TestClient(app)

    # Session B attempts to delete Session A's repository
    headers_B = _make_auth_header("sess_owner_B", settings)
    resp = client.delete("/api/v1/repositories/repo_other_001", headers=headers_B)

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["error"]["message"] == "The requested resource was not found."

    # Verify repository still exists for Session A
    assert repo_repo.get_by_id("sess_owner_A", "repo_other_001") is not None


def test_delete_failed_repository_success(monkeypatch) -> None:
    """Deleting a repository in failed status succeeds safely."""
    settings = Settings(
        session_signing_secret=SecretStr(TEST_SECRET),
        jwt_secret=SecretStr(TEST_SECRET),
    )
    monkeypatch.setattr("sourcetrace.api.app.get_settings", lambda: settings)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()
    sess_repo = InMemoryAnonymousSessionRepository()
    chunk_repo = InMemoryCodeChunkRepository()
    conv_repo = InMemoryConversationRepository()
    msg_repo = InMemoryMessageRepository()

    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_session_repository] = lambda: sess_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: chunk_repo
    app.dependency_overrides[get_conversation_repository] = lambda: conv_repo
    app.dependency_overrides[get_message_repository] = lambda: msg_repo

    owner_id = "sess_failed_owner"
    repo_id = "repo_failed_123"

    sess_repo.save(
        AnonymousSession(
            owner_session_id=owner_id,
            last_active_at=NOW,
            expires_at=NOW + timedelta(days=7),
            created_at=NOW,
            updated_at=NOW,
            active_repository_count=1,
        )
    )

    repo_repo.save(
        RepositoryRecord(
            repository_id=repo_id,
            owner_session_id=owner_id,
            name="FailedRepo",
            source_type="github",
            status="failed",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    job_repo.save(
        IndexingJobRecord(
            job_id="job_failed_123",
            repository_id=repo_id,
            owner_session_id=owner_id,
            status="failed",
            current_step="Scan failed",
            error_message="Invalid zip",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    client = TestClient(app)
    headers = _make_auth_header(owner_id, settings)

    resp = client.delete(f"/api/v1/repositories/{repo_id}", headers=headers)
    assert resp.status_code == status.HTTP_200_OK

    assert repo_repo.get_by_id(owner_id, repo_id) is None
    assert job_repo.get_by_repository(owner_id, repo_id) is None

    updated_sess = sess_repo.get_by_id(owner_id)
    assert updated_sess is not None
    assert updated_sess.active_repository_count == 0
