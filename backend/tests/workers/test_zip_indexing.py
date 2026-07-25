import asyncio
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcetrace.embeddings.provider import EmbeddingProvider
from sourcetrace.ingestion.upload_staging import (
    FileSystemUploadStagingStore,
    UploadStagingStore,
)
from sourcetrace.models.domain import (
    CodeChunk,
    IndexingJobRecord,
    RepositoryRecord,
    RetrievalResult,
)
from sourcetrace.workers.zip_indexing import run_zip_indexing


def create_sample_zip_bytes(filename: str = "main.py", content: str = "def foo(): pass\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, content)
    return buf.getvalue()


class MockEmbeddingProvider(EmbeddingProvider):
    def embed(self, texts: Any) -> tuple[tuple[float, ...], ...]:
        return tuple((0.1,) * 1536 for _ in texts)

    @property
    def model_identifier(self) -> str:
        return "text-embedding-3-small"

    @property
    def embedding_dimensions(self) -> int:
        return 1536


class InMemoryRepositoryRepository:
    def __init__(self, records: list[RepositoryRecord] | None = None) -> None:
        self.records: list[RepositoryRecord] = records or []

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        for r in self.records:
            if r.owner_session_id == owner_session_id and r.repository_id == repository_id:
                return r
        return None

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for r in self.records if r.owner_session_id == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        self.records.append(repository)
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
        rec = self.get_by_id(owner_session_id, repository_id)
        if rec is None:
            return None
        allowed = (expected_status,) if isinstance(expected_status, str) else expected_status
        if rec.status not in allowed:
            return None
        updated_rec = RepositoryRecord(
            repository_id=rec.repository_id,
            owner_session_id=rec.owner_session_id,
            name=rec.name,
            source_type=rec.source_type,
            status=new_status,  # type: ignore[arg-type]
            created_at=rec.created_at,
            updated_at=updated_at,
            file_count=file_count if file_count is not None else rec.file_count,
            chunk_count=chunk_count if chunk_count is not None else rec.chunk_count,
            index_mode=rec.index_mode,
        )
        self.records = [
            updated_rec
            if r.repository_id == repository_id and r.owner_session_id == owner_session_id
            else r
            for r in self.records
        ]
        return updated_rec

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        initial = len(self.records)
        self.records = [
            r for r in self.records
            if not (r.owner_session_id == owner_session_id and r.repository_id == repository_id)
        ]
        return len(self.records) < initial


class InMemoryIndexingJobRepository:
    def __init__(self, records: list[IndexingJobRecord] | None = None) -> None:
        self.records: list[IndexingJobRecord] = records or []

    def get_by_id(self, owner_session_id: str, job_id: str) -> IndexingJobRecord | None:
        for j in self.records:
            if j.owner_session_id == owner_session_id and j.job_id == job_id:
                return j
        return None

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        for j in self.records:
            if j.owner_session_id == owner_session_id and j.repository_id == repository_id:
                return j
        return None

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        self.records.append(job)
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
        job = self.get_by_id(owner_session_id, job_id)
        if job is None or job.repository_id != repository_id:
            return None
        allowed = (expected_status,) if isinstance(expected_status, str) else expected_status
        if job.status not in allowed:
            return None
        updated_job = IndexingJobRecord(
            job_id=job.job_id,
            repository_id=job.repository_id,
            owner_session_id=job.owner_session_id,
            status=new_status,  # type: ignore[arg-type]
            current_step=current_step,
            created_at=job.created_at,
            updated_at=updated_at,
            progress_percentage=(
                progress_percentage if progress_percentage is not None else job.progress_percentage
            ),
            error_message=error_message if error_message is not None else job.error_message,
            completed_at=completed_at if completed_at is not None else job.completed_at,
        )
        self.records = [
            updated_job if j.job_id == job_id and j.owner_session_id == owner_session_id else j
            for j in self.records
        ]
        return updated_job

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        initial = len(self.records)
        self.records = [
            j for j in self.records
            if not (j.owner_session_id == owner_session_id and j.repository_id == repository_id)
        ]
        return initial - len(self.records)


class InMemoryCodeChunkRepository:
    def __init__(self) -> None:
        self.saved_chunks: list[CodeChunk] = []

    def save_many(self, chunks: list[CodeChunk]) -> int:
        self.saved_chunks.extend(chunks)
        return len(chunks)

    def list_by_repository(self, owner_session_id: str, repository_id: str) -> list[CodeChunk]:
        return [
            c for c in self.saved_chunks
            if c.owner_session_id == owner_session_id and c.repository_id == repository_id
        ]

    def search_vectors(
        self, owner_session_id: str, repository_id: str, query_vector: list[float], limit: int = 5
    ) -> list[RetrievalResult]:
        return []

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        initial = len(self.saved_chunks)
        self.saved_chunks = [
            c for c in self.saved_chunks
            if not (c.owner_session_id == owner_session_id and c.repository_id == repository_id)
        ]
        return initial - len(self.saved_chunks)


class DummyUploadFile:
    def __init__(self, content: bytes) -> None:
        self.file = io.BytesIO(content)

    async def read(self, size: int = -1) -> bytes:
        return self.file.read(size)


def test_run_zip_indexing_success(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    owner_id = "sess_zip_worker_success"
    repo_id = "repo_zip_worker_success"
    job_id = "job_zip_worker_success"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="zip-test",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    job = IndexingJobRecord(
        job_id=job_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        status="queued",
        current_step="Queued for acquisition",
        created_at=now,
        updated_at=now,
    )

    repo_repo = InMemoryRepositoryRepository([repo])
    job_repo = InMemoryIndexingJobRepository([job])
    code_chunk_repo = InMemoryCodeChunkRepository()
    provider = MockEmbeddingProvider()

    staging_store = FileSystemUploadStagingStore(staging_root=tmp_path)
    zip_bytes = create_sample_zip_bytes("utils.py", "def helper(): return 42\n")
    staged_upload = asyncio.run(staging_store.stage(DummyUploadFile(zip_bytes)))
    token = staged_upload.token

    staged_path = staging_store.resolve(token)
    assert staged_path.exists()

    run_zip_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        staging_token=token,
        repository_repo=repo_repo,
        job_repo=job_repo,
        code_chunk_repo=code_chunk_repo,
        provider=provider,
        staging_store=staging_store,
    )

    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    final_job = job_repo.get_by_id(owner_id, job_id)

    assert final_repo is not None
    assert final_repo.status == "ready"
    assert final_repo.file_count == 1
    assert final_repo.chunk_count == 1

    assert final_job is not None
    assert final_job.status == "ready"
    assert final_job.progress_percentage == 100

    assert not staged_path.exists()


def test_run_zip_indexing_setup_failure_exact_signature_and_cleanup(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    owner_id = "owner_setup_fail"
    repo_id = "repo_setup_fail"
    job_id = "job_setup_fail"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="zip-test",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
        index_mode="cloud_ai",
    )
    job = IndexingJobRecord(
        job_id=job_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        status="queued",
        current_step="Queued for acquisition",
        created_at=now,
        updated_at=now,
    )

    repo_repo = InMemoryRepositoryRepository([repo])
    job_repo = InMemoryIndexingJobRepository([job])
    code_chunk_repo = InMemoryCodeChunkRepository()

    staging_store = FileSystemUploadStagingStore(staging_root=tmp_path)
    zip_bytes = create_sample_zip_bytes()
    staged_upload = asyncio.run(staging_store.stage(DummyUploadFile(zip_bytes)))
    token = staged_upload.token
    staged_path = staging_store.resolve(token)

    def failing_provider_factory() -> Any:
        raise RuntimeError("Provider init failure")

    run_zip_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        staging_token=token,
        repository_repo=repo_repo,
        job_repo=job_repo,
        code_chunk_repo=code_chunk_repo,
        provider_factory=failing_provider_factory,
        staging_store=staging_store,
    )

    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    final_job = job_repo.get_by_id(owner_id, job_id)

    assert final_repo is not None
    assert final_repo.status == "failed"

    assert final_job is not None
    assert final_job.status == "failed"
    assert final_job.current_step == "Indexing setup failed"

    assert not staged_path.exists()


def test_run_zip_indexing_cleans_up_on_keyboard_interrupt(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    owner_id = "sess_interrupt"
    repo_id = "repo_interrupt"
    job_id = "job_interrupt"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="zip-test",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
        index_mode="cloud_ai",
    )
    repo_repo = InMemoryRepositoryRepository([repo])

    staging_store = FileSystemUploadStagingStore(staging_root=tmp_path)
    zip_bytes = create_sample_zip_bytes()
    staged_upload = asyncio.run(staging_store.stage(DummyUploadFile(zip_bytes)))
    token = staged_upload.token
    staged_path = staging_store.resolve(token)

    def interrupt_provider_factory() -> Any:
        raise KeyboardInterrupt("Simulated user cancellation")

    with pytest.raises(KeyboardInterrupt):
        run_zip_indexing(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            staging_token=token,
            repository_repo=repo_repo,
            provider_factory=interrupt_provider_factory,
            staging_store=staging_store,
        )

    assert not staged_path.exists()


def test_run_zip_indexing_cleans_up_on_system_exit(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    owner_id = "sess_sys_exit"
    repo_id = "repo_sys_exit"
    job_id = "job_sys_exit"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="zip-test",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
        index_mode="cloud_ai",
    )
    repo_repo = InMemoryRepositoryRepository([repo])

    staging_store = FileSystemUploadStagingStore(staging_root=tmp_path)
    zip_bytes = create_sample_zip_bytes()
    staged_upload = asyncio.run(staging_store.stage(DummyUploadFile(zip_bytes)))
    token = staged_upload.token
    staged_path = staging_store.resolve(token)

    def exit_provider_factory() -> Any:
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        run_zip_indexing(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            staging_token=token,
            repository_repo=repo_repo,
            provider_factory=exit_provider_factory,
            staging_store=staging_store,
        )

    assert not staged_path.exists()


def test_run_zip_indexing_ordinary_runner_failure_contained(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    owner_id = "sess_ordinary_fail"
    repo_id = "repo_ordinary_fail"
    job_id = "job_ordinary_fail"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="zip-test",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
        index_mode="cloud_ai",
    )
    job = IndexingJobRecord(
        job_id=job_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        status="queued",
        current_step="Queued for acquisition",
        created_at=now,
        updated_at=now,
    )

    repo_repo = InMemoryRepositoryRepository([repo])
    job_repo = InMemoryIndexingJobRepository([job])
    code_chunk_repo = InMemoryCodeChunkRepository()

    staging_store = FileSystemUploadStagingStore(staging_root=tmp_path)
    # Stage corrupt non-zip file
    staged_path = tmp_path / "stg_corrupt12345678901234567890123.staged"
    staged_path.write_bytes(b"not a valid zip file for extraction")
    token = "stg_corrupt12345678901234567890123"

    # Worker must NOT raise exception to background boundary
    run_zip_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        staging_token=token,
        repository_repo=repo_repo,
        job_repo=job_repo,
        code_chunk_repo=code_chunk_repo,
        provider=MockEmbeddingProvider(),
        staging_store=staging_store,
    )

    assert not staged_path.exists()


def test_zip_indexing_cleanup_exception_suppression(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    repo = RepositoryRecord(
        repository_id="repo",
        owner_session_id="owner",
        name="zip-test",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
        index_mode="cloud_ai",
    )
    repo_repo = InMemoryRepositoryRepository([repo])

    mock_store = MagicMock()
    mock_store.resolve.return_value = tmp_path / "valid.staged"
    (tmp_path / "valid.staged").write_bytes(b"data")
    mock_store.delete.side_effect = RuntimeError("Delete failure with secret /path/to/secret")

    def interrupt_factory() -> Any:
        raise KeyboardInterrupt("Original Interrupt")

    with pytest.raises(KeyboardInterrupt) as exc_info:
        run_zip_indexing(
            owner_session_id="owner",
            repository_id="repo",
            job_id="job",
            staging_token="stg_12345678901234567890123456789012",
            repository_repo=repo_repo,
            provider_factory=interrupt_factory,
            staging_store=mock_store,
        )

    assert "Original Interrupt" in str(exc_info.value)


@pytest.mark.parametrize(
    "failure_mode",
    ["success", "setup_failure", "execution_failure", "keyboard_interrupt", "system_exit"],
)
def test_zip_indexing_single_cleanup_owner_call_count(tmp_path: Path, failure_mode: str) -> None:
    now = datetime.now(UTC)
    owner_id = "owner_single_cleanup"
    repo_id = "repo_single_cleanup"
    job_id = "job_single_cleanup"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="zip-test",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
        index_mode="cloud_ai",
    )
    job = IndexingJobRecord(
        job_id=job_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        status="queued",
        current_step="Queued for acquisition",
        created_at=now,
        updated_at=now,
    )

    repo_repo = InMemoryRepositoryRepository([repo])
    job_repo = InMemoryIndexingJobRepository([job])
    code_chunk_repo = InMemoryCodeChunkRepository()

    staging_store = MagicMock(spec=UploadStagingStore)
    token = "stg_12345678901234567890123456789012"

    if failure_mode == "setup_failure":
        staging_store.resolve.side_effect = RuntimeError("Setup failed")
    else:
        staged_file = tmp_path / "test.zip"
        staged_file.write_bytes(create_sample_zip_bytes())
        staging_store.resolve.return_value = staged_file

    def failing_provider_factory() -> Any:
        if failure_mode == "execution_failure":
            raise RuntimeError("Execution error")
        elif failure_mode == "keyboard_interrupt":
            raise KeyboardInterrupt()
        elif failure_mode == "system_exit":
            raise SystemExit(1)
        return MockEmbeddingProvider()

    if failure_mode in ("keyboard_interrupt", "system_exit"):
        with pytest.raises((KeyboardInterrupt, SystemExit)):
            run_zip_indexing(
                owner_session_id=owner_id,
                repository_id=repo_id,
                job_id=job_id,
                staging_token=token,
                repository_repo=repo_repo,
                job_repo=job_repo,
                code_chunk_repo=code_chunk_repo,
                provider_factory=failing_provider_factory,
                staging_store=staging_store,
            )
    else:
        run_zip_indexing(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            staging_token=token,
            repository_repo=repo_repo,
            job_repo=job_repo,
            code_chunk_repo=code_chunk_repo,
            provider_factory=failing_provider_factory,
            staging_store=staging_store,
        )

    assert staging_store.delete.call_count == 1
    staging_store.delete.assert_called_once_with(token)

