"""Offline integration tests for background GitHub indexing worker."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcetrace.models.domain import IndexingJobRecord, RepositoryRecord
from sourcetrace.workers.github_indexing import run_github_indexing


class InMemoryRepositoryRepository:
    def __init__(self, records: list[RepositoryRecord] | None = None) -> None:
        self.records: list[RepositoryRecord] = records or []

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        for r in self.records:
            if r.owner_session_id == owner_session_id and r.repository_id == repository_id:
                return r
        return None

    def transition_status(
        self,
        owner_session_id: str,
        repository_id: str,
        expected_status: str | tuple[str, ...],
        new_status: str,
        updated_at: datetime | None = None,
        file_count: int | None = None,
        chunk_count: int | None = None,
        indexed_branch: str | None = None,
        indexed_commit_sha: str | None = None,
        last_indexed_at: datetime | None = None,
        parser_versions: tuple[str, ...] | list[str] | None = None,
        flow_evidence_complete: bool | None = None,
        indexed_file_count: int | None = None,
        indexed_chunk_count: int | None = None,
        **kwargs: Any,
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
            updated_at=updated_at or datetime.now(UTC),
            file_count=file_count if file_count is not None else rec.file_count,
            chunk_count=chunk_count if chunk_count is not None else rec.chunk_count,
            index_mode=rec.index_mode,
            active_generation_id=rec.active_generation_id,
            indexed_branch=indexed_branch if indexed_branch is not None else rec.indexed_branch,
            indexed_commit_sha=indexed_commit_sha
            if indexed_commit_sha is not None
            else rec.indexed_commit_sha,
            last_indexed_at=last_indexed_at if last_indexed_at is not None else rec.last_indexed_at,
            parser_versions=tuple(parser_versions)
            if parser_versions is not None
            else rec.parser_versions,
            flow_evidence_complete=flow_evidence_complete
            if flow_evidence_complete is not None
            else rec.flow_evidence_complete,
            indexed_file_count=indexed_file_count
            if indexed_file_count is not None
            else rec.indexed_file_count,
            indexed_chunk_count=indexed_chunk_count
            if indexed_chunk_count is not None
            else rec.indexed_chunk_count,
        )
        self.records = [
            (
                updated_rec
                if r.repository_id == repository_id and r.owner_session_id == owner_session_id
                else r
            )
            for r in self.records
        ]
        return updated_rec


class InMemoryIndexingJobRepository:
    def __init__(self, records: list[IndexingJobRecord] | None = None) -> None:
        self.records: list[IndexingJobRecord] = records or []

    def get_by_id(self, owner_session_id: str, job_id: str) -> IndexingJobRecord | None:
        for j in self.records:
            if j.owner_session_id == owner_session_id and j.job_id == job_id:
                return j
        return None

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
            current_step=current_step if current_step is not None else job.current_step,
            progress_percentage=(
                progress_percentage if progress_percentage is not None else job.progress_percentage
            ),
            error_message=error_message if error_message is not None else job.error_message,
            completed_at=completed_at if completed_at is not None else job.completed_at,
            created_at=job.created_at,
            updated_at=updated_at or datetime.now(UTC),
        )
        self.records = [
            updated_job if j.job_id == job_id and j.owner_session_id == owner_session_id else j
            for j in self.records
        ]
        return updated_job


class InMemoryCodeChunkRepository:
    def __init__(self) -> None:
        self.chunks: list[Any] = []

    def save_many(self, chunks: list[Any]) -> int:
        self.chunks.extend(chunks)
        return len(chunks)


class FakeEmbeddingProvider:
    @property
    def model_identifier(self) -> str:
        return "text-embedding-3-small"

    @property
    def embedding_dimensions(self) -> int:
        return 1536

    def embed(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        return tuple((0.1,) * 1536 for _ in texts)


def test_run_github_indexing_worker_successful_execution(tmp_path: Any) -> None:
    now = datetime.now(UTC)
    owner_id = "sess_worker_test"
    repo_id = "repo_worker_test"
    job_id = "job_worker_test"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
        file_count=0,
        chunk_count=0,
    )
    job = IndexingJobRecord(
        job_id=job_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        status="queued",
        current_step="Queued for acquisition",
        created_at=now,
        updated_at=now,
        progress_percentage=0,
    )

    repo_repo = InMemoryRepositoryRepository([repo])
    job_repo = InMemoryIndexingJobRepository([job])
    code_chunk_repo = InMemoryCodeChunkRepository()
    provider = FakeEmbeddingProvider()

    # Mock download and extraction so no live HTTP is made
    mock_zip_file = tmp_path / "fake.zip"
    mock_zip_file.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # minimal zip

    mock_download = MagicMock()
    mock_download.__enter__.return_value = MagicMock(archive_path=mock_zip_file)

    mock_extract = MagicMock()
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir()
    (extracted_dir / "main.py").write_text("def hello(): pass")

    mock_manifest = MagicMock(
        relative_paths=("main.py",),
        total_files=1,
        total_bytes=17,
        skipped_unsupported_extensions=0,
        skipped_ignored_paths=0,
        skipped_secret_files=0,
        skipped=(),
    )
    mock_extract.__enter__.return_value = MagicMock(
        target_dir=extracted_dir, manifest=mock_manifest
    )

    with (
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_download_github_archive",
            lambda **kw: mock_download,
        )
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_extract_zip",
            lambda **kw: mock_extract,
        )

        run_github_indexing(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            repository_repo=repo_repo,
            job_repo=job_repo,
            code_chunk_repo=code_chunk_repo,
            provider=provider,
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
    assert final_job.current_step == "Repository ready"
    assert final_job.completed_at is not None


def test_run_github_indexing_worker_contains_ordinary_runner_failure() -> None:
    now = datetime.now(UTC)
    owner_id = "sess_worker_fail"
    repo_id = "repo_worker_fail"
    job_id = "job_worker_fail"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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
    provider = FakeEmbeddingProvider()

    def mock_failing_download(**kw: Any) -> Any:
        raise RuntimeError("Download network error")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_download_github_archive",
            mock_failing_download,
        )

        # Worker contains ordinary failure without raising exception to background caller
        run_github_indexing(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            repository_repo=repo_repo,
            job_repo=job_repo,
            code_chunk_repo=code_chunk_repo,
            provider=provider,
        )

    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    final_job = job_repo.get_by_id(owner_id, job_id)

    assert final_repo is not None
    assert final_repo.status == "failed"

    assert final_job is not None
    assert final_job.status == "failed"
    assert final_job.current_step == "Acquisition failed"
    assert final_job.error_message == "Acquisition failed safely."


def test_run_github_indexing_worker_passes_through_process_control_exceptions() -> None:
    now = datetime.now(UTC)
    owner_id = "sess_worker_sig"
    repo_id = "repo_worker_sig"
    job_id = "job_worker_sig"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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
    provider = FakeEmbeddingProvider()

    def mock_sig_download(**kw: Any) -> Any:
        raise KeyboardInterrupt()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_download_github_archive",
            mock_sig_download,
        )

        with pytest.raises(KeyboardInterrupt):
            run_github_indexing(
                owner_session_id=owner_id,
                repository_id=repo_id,
                job_id=job_id,
                repository_repo=repo_repo,
                job_repo=job_repo,
                code_chunk_repo=code_chunk_repo,
                provider=provider,
            )


def test_run_github_indexing_worker_pre_claim_composition_failure_conditional_transition() -> None:
    now = datetime.now(UTC)
    owner_id = "sess_comp_fail"
    repo_id = "repo_comp_fail"
    job_id = "job_comp_fail"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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

    def failing_provider_factory() -> Any:
        raise RuntimeError("Invalid API key or setup error")

    # Run worker with a factory that fails before runner is built
    run_github_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        repository_repo=repo_repo,
        job_repo=job_repo,
        provider_factory=failing_provider_factory,
    )

    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    final_job = job_repo.get_by_id(owner_id, job_id)

    assert final_repo is not None
    assert final_repo.status == "failed"

    assert final_job is not None
    assert final_job.status == "failed"
    assert final_job.current_step == "Indexing setup failed"
    assert final_job.error_message == "Indexing could not start safely."


def test_run_github_indexing_pre_claim_failure_preserves_active_job() -> None:
    now = datetime.now(UTC)
    owner_id = "sess_comp_active"
    repo_id = "repo_comp_active"
    job_id = "job_comp_active"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="indexing",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
        index_mode="cloud_ai",
    )
    job = IndexingJobRecord(
        job_id=job_id,
        repository_id=repo_id,
        owner_session_id=owner_id,
        status="acquiring",
        current_step="Acquiring source repository",
        created_at=now,
        updated_at=now,
    )

    repo_repo = InMemoryRepositoryRepository([repo])
    job_repo = InMemoryIndexingJobRepository([job])

    def failing_provider_factory() -> Any:
        raise RuntimeError("Setup error")

    run_github_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        repository_repo=repo_repo,
        job_repo=job_repo,
        provider_factory=failing_provider_factory,
    )

    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    final_job = job_repo.get_by_id(owner_id, job_id)

    # Statuses must NOT be overwritten if job was already acquiring or active
    assert final_repo is not None
    assert final_repo.status == "indexing"

    assert final_job is not None
    assert final_job.status == "acquiring"


def test_run_github_indexing_worker_setup_failure_exact_signature_and_gating() -> None:
    now = datetime.now(UTC)
    owner_id = "sess_comp_sig"
    repo_id = "repo_comp_sig"
    job_id = "job_comp_sig"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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

    job_transition_calls: list[dict[str, Any]] = []
    original_job_transition = job_repo.transition_status

    def spy_job_transition(*args: Any, **kwargs: Any) -> IndexingJobRecord | None:
        job_transition_calls.append({"args": args, "kwargs": kwargs})
        return original_job_transition(*args, **kwargs)

    job_repo.transition_status = spy_job_transition  # type: ignore[assignment]

    def failing_provider_factory() -> Any:
        raise RuntimeError("Setup error")

    run_github_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        repository_repo=repo_repo,
        job_repo=job_repo,
        provider_factory=failing_provider_factory,
    )

    assert len(job_transition_calls) == 1
    call_kwargs = job_transition_calls[0]["kwargs"]
    assert call_kwargs["owner_session_id"] == owner_id
    assert call_kwargs["job_id"] == job_id
    assert call_kwargs["repository_id"] == repo_id
    assert call_kwargs["expected_status"] == "queued"
    assert call_kwargs["new_status"] == "failed"
    assert call_kwargs["current_step"] == "Indexing setup failed"
    assert call_kwargs["progress_percentage"] is None
    assert call_kwargs["error_message"] == "Indexing could not start safely."

    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    assert final_repo is not None
    assert final_repo.status == "failed"


def test_run_github_indexing_worker_setup_failure_lost_race_prevents_repo_compensation() -> None:
    now = datetime.now(UTC)
    owner_id = "sess_race"
    repo_id = "repo_race"
    job_id = "job_race"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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

    job_repo.transition_status = MagicMock(return_value=None)
    repo_repo.transition_status = MagicMock()

    def failing_provider_factory() -> Any:
        raise RuntimeError("Setup error")

    run_github_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        repository_repo=repo_repo,
        job_repo=job_repo,
        provider_factory=failing_provider_factory,
    )

    repo_repo.transition_status.assert_not_called()


def test_run_github_indexing_worker_setup_failure_job_exception_contains_error() -> None:
    now = datetime.now(UTC)
    owner_id = "sess_exc"
    repo_id = "repo_exc"
    job_id = "job_exc"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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

    secret_msg = "secret_worker_db_credentials_123"
    job_repo.transition_status = MagicMock(side_effect=RuntimeError(secret_msg))
    repo_repo.transition_status = MagicMock()

    def failing_provider_factory() -> Any:
        raise RuntimeError("Setup error")

    # Worker must contain error without crashing
    run_github_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        repository_repo=repo_repo,
        job_repo=job_repo,
        provider_factory=failing_provider_factory,
    )

    repo_repo.transition_status.assert_not_called()


@pytest.mark.parametrize(
    "malformed_result",
    [
        True,
        {"status": "failed"},
        IndexingJobRecord(
            job_id="job_wrong",
            repository_id="repo_wrong",
            owner_session_id="sess_wrong",
            status="failed",
            current_step="Failed",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        IndexingJobRecord(
            job_id="job_wrong",
            repository_id="repo_wrong",
            owner_session_id="sess_comp_malformed",
            status="ready",
            current_step="Ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
    ],
)
def test_run_github_indexing_worker_setup_failure_malformed_result_prevents_repo_compensation(
    malformed_result: Any,
) -> None:
    now = datetime.now(UTC)
    owner_id = "sess_comp_malformed"
    repo_id = "repo_comp_malformed"
    job_id = "job_comp_malformed"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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

    job_repo.transition_status = MagicMock(return_value=malformed_result)
    repo_repo.transition_status = MagicMock()

    def failing_provider_factory() -> Any:
        raise RuntimeError("Setup error")

    run_github_indexing(
        owner_session_id=owner_id,
        repository_id=repo_id,
        job_id=job_id,
        repository_repo=repo_repo,
        job_repo=job_repo,
        provider_factory=failing_provider_factory,
    )

    repo_repo.transition_status.assert_not_called()


def test_run_github_indexing_static_mode_bomb_factory(tmp_path: Any) -> None:
    """Bomb-factory test: static mode worker completes without calling raising provider_factory."""
    now = datetime.now(UTC)
    owner_id = "sess_bomb"
    repo_id = "repo_bomb"
    job_id = "job_bomb"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
        index_mode="static",
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

    bomb_factory = MagicMock(side_effect=AssertionError("Bomb factory was invoked in static mode!"))

    mock_zip_file = tmp_path / "fake.zip"
    mock_zip_file.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    mock_download = MagicMock()
    mock_download.__enter__.return_value = MagicMock(archive_path=mock_zip_file)

    mock_extract = MagicMock()
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir()
    (extracted_dir / "main.py").write_text("def hello(): pass")

    mock_manifest = MagicMock(
        relative_paths=("main.py",),
        total_files=1,
        total_bytes=17,
        skipped_unsupported_extensions=0,
        skipped_ignored_paths=0,
        skipped_secret_files=0,
        skipped=(),
    )
    mock_extract.__enter__.return_value = MagicMock(
        target_dir=extracted_dir, manifest=mock_manifest
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_download_github_archive",
            lambda **kw: mock_download,
        )
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_extract_zip",
            lambda **kw: mock_extract,
        )

        run_github_indexing(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            repository_repo=repo_repo,
            job_repo=job_repo,
            code_chunk_repo=code_chunk_repo,
            provider_factory=bomb_factory,
        )

    # Factory must NOT have been called!
    bomb_factory.assert_not_called()

    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    assert final_repo is not None
    assert final_repo.status == "ready"


def test_run_github_indexing_cloud_mode_calls_provider_factory(tmp_path: Any) -> None:
    """Verify cloud_ai mode repository calls provider_factory exactly once."""
    now = datetime.now(UTC)
    owner_id = "sess_cloud"
    repo_id = "repo_cloud"
    job_id = "job_cloud"

    repo = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="test-repo",
        source_type="github",
        status="pending",
        created_at=now,
        updated_at=now,
        github_url="https://github.com/octocat/Hello-World",
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
    provider = FakeEmbeddingProvider()

    factory = MagicMock(return_value=provider)

    mock_zip_file = tmp_path / "fake.zip"
    mock_zip_file.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    mock_download = MagicMock()
    mock_download.__enter__.return_value = MagicMock(archive_path=mock_zip_file)

    mock_extract = MagicMock()
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir()
    (extracted_dir / "main.py").write_text("def hello(): pass")

    mock_manifest = MagicMock(
        relative_paths=("main.py",),
        total_files=1,
        total_bytes=17,
        skipped_unsupported_extensions=0,
        skipped_ignored_paths=0,
        skipped_secret_files=0,
        skipped=(),
    )
    mock_extract.__enter__.return_value = MagicMock(
        target_dir=extracted_dir, manifest=mock_manifest
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_download_github_archive",
            lambda **kw: mock_download,
        )
        mp.setattr(
            "sourcetrace.ingestion.acquisition.safe_extract_zip",
            lambda **kw: mock_extract,
        )

        run_github_indexing(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            repository_repo=repo_repo,
            job_repo=job_repo,
            code_chunk_repo=code_chunk_repo,
            provider_factory=factory,
        )

    factory.assert_called_once()
