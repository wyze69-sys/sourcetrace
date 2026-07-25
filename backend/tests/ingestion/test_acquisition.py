"""Offline unit and integration tests for managed acquired-source handoff."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sourcetrace.core.exceptions import (
    AcquisitionError,
    DisallowedRedirectError,
    InvalidArchiveError,
    InvalidRepositoryURLError,
)
from sourcetrace.ingestion.acquisition import (
    AcquiredSource,
    AcquisitionRunner,
    acquire_github_source,
    acquire_zip_source,
)
from sourcetrace.models.domain import IndexingJobRecord, RepositoryRecord
from sourcetrace.storage.repositories import (
    IndexingJobRepository,
    RepositoryRepository,
)


class DummyStorageError(Exception):
    """Raw storage exception for testing failure masking."""


class ConfigurableRepositoryRepository(RepositoryRepository):
    """Repository test double capable of selective failure injection."""

    def __init__(self) -> None:
        self._repos: dict[tuple[str, str], RepositoryRecord] = {}
        self.fail_get_by_id = False
        self.fail_save_status: set[str] = set()
        self.save_calls: list[RepositoryRecord] = []

    def get_by_id(
        self, owner_session_id: str, repository_id: str
    ) -> RepositoryRecord | None:
        if self.fail_get_by_id:
            raise DummyStorageError("Raw MongoDB repository query failure: secret_db_uri")
        return self._repos.get((owner_session_id, repository_id))

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [
            repo
            for (sid, _), repo in self._repos.items()
            if sid == owner_session_id
        ]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        if repository.status in self.fail_save_status:
            raise DummyStorageError(
                f"Raw MongoDB repository save failure for status {repository.status}"
            )
        self.save_calls.append(repository)
        key = (repository.owner_session_id, repository.repository_id)
        self._repos[key] = repository
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
        if new_status in self.fail_save_status:
            raise DummyStorageError(
                f"Raw MongoDB repository transition failure for status {new_status}"
            )
        key = (owner_session_id, repository_id)
        if key not in self._repos:
            return None
        existing = self._repos[key]
        expected_set = (
            {expected_status}
            if isinstance(expected_status, str)
            else set(expected_status)
        )
        if existing.status not in expected_set:
            return None

        updated = RepositoryRecord(
            repository_id=existing.repository_id,
            owner_session_id=existing.owner_session_id,
            name=existing.name,
            source_type=existing.source_type,
            status=new_status,
            created_at=existing.created_at,
            updated_at=updated_at,
            github_url=existing.github_url,
            file_count=file_count if file_count is not None else existing.file_count,
            chunk_count=chunk_count if chunk_count is not None else existing.chunk_count,
        )
        self.save_calls.append(updated)
        self._repos[key] = updated
        return updated

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        key = (owner_session_id, repository_id)
        if key in self._repos:
            del self._repos[key]
            return True
        return False


class ConfigurableIndexingJobRepository(IndexingJobRepository):
    """Indexing job test double capable of selective failure injection."""

    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], IndexingJobRecord] = {}
        self.fail_get_by_id = False
        self.fail_save_status: set[str] = set()
        self.save_calls: list[IndexingJobRecord] = []

    def get_by_id(
        self, owner_session_id: str, job_id: str
    ) -> IndexingJobRecord | None:
        if self.fail_get_by_id:
            raise DummyStorageError("Raw MongoDB job query failure: secret_db_uri")
        return self._jobs.get((owner_session_id, job_id))

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        for (sid, _), job in self._jobs.items():
            if sid == owner_session_id and job.repository_id == repository_id:
                return job
        return None

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        if job.status in self.fail_save_status:
            raise DummyStorageError(
                f"Raw MongoDB job save failure for status {job.status}"
            )

        self.save_calls.append(job)
        key = (job.owner_session_id, job.job_id)
        self._jobs[key] = job
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
        if new_status in self.fail_save_status:
            raise DummyStorageError(
                f"Raw MongoDB job transition failure for status {new_status}"
            )
        key = (owner_session_id, job_id)
        if key not in self._jobs:
            return None
        existing = self._jobs[key]
        if existing.repository_id != repository_id:
            return None
        expected_set = (
            {expected_status}
            if isinstance(expected_status, str)
            else set(expected_status)
        )
        if existing.status not in expected_set:
            return None

        updated = IndexingJobRecord(
            job_id=existing.job_id,
            repository_id=existing.repository_id,
            owner_session_id=existing.owner_session_id,
            status=new_status,
            current_step=current_step,
            created_at=existing.created_at,
            updated_at=updated_at,
            progress_percentage=(
                progress_percentage
                if progress_percentage is not None
                else existing.progress_percentage
            ),
            error_message=error_message if error_message is not None else existing.error_message,
            completed_at=completed_at if completed_at is not None else existing.completed_at,
        )
        self.save_calls.append(updated)
        self._jobs[key] = updated
        return updated

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:
        keys_to_del = [
            k
            for k, j in self._jobs.items()
            if k[0] == owner_session_id and j.repository_id == repository_id
        ]
        for k in keys_to_del:
            del self._jobs[k]
        return len(keys_to_del)



def create_valid_zip_bytes(files: dict[str, str] | None = None) -> bytes:
    """Helper to construct a valid in-memory ZIP archive."""
    if files is None:
        files = {"example.py": "def hello():\n    return 'world'\n"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, contents in files.items():
            zf.writestr(path, contents)
    return buf.getvalue()


def mock_dns_resolver(hostname: str) -> list[str]:
    """Mock DNS resolver returning a valid public IP."""
    return ["93.184.216.34"]


class MockResponse:
    """Mock httpx response streaming valid ZIP content."""

    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {
            "Content-Type": "application/zip",
            "Content-Length": str(len(content)),
        }

    def iter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]


class MockHTTPClient:
    """Mock httpx.Client for offline GitHub archive download testing."""

    def __init__(self, zip_data: bytes, status_code: int = 200) -> None:
        self._zip_data = zip_data
        self._status_code = status_code

    def stream(self, method: str, url: str, **kwargs):
        class MockStreamContext:
            def __init__(self, response: MockResponse) -> None:
                self.response = response

            def __enter__(self) -> MockResponse:
                return self.response

            def __exit__(self, exc_type, exc_val, exc_tb) -> None:
                pass

        return MockStreamContext(MockResponse(self._zip_data, self._status_code))

    def close(self) -> None:
        pass


def test_github_composition_invokes_consumer_inside_both_managed_contexts(tmp_path: Path):
    """Verify GitHub acquisition downloads, extracts, and yields acquired source."""
    zip_bytes = create_valid_zip_bytes({"main.py": "print('hello')\n"})
    client = MockHTTPClient(zip_bytes)

    consumer_called = False
    extracted_path: Path | None = None

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_called, extracted_path
        consumer_called = True
        extracted_path = source.extraction_root

        assert source.source_type == "github"
        assert source.extraction_root.exists()
        assert source.manifest.file_count == 1
        assert "main.py" in source.manifest.relative_paths

        target_file = source.extraction_root / "main.py"
        assert target_file.exists()
        assert target_file.read_text(encoding="utf-8") == "print('hello')\n"

    acquire_github_source(
        url="https://github.com/test-owner/test-repo",
        consumer=consumer,
        parent_dir=tmp_path,
        client=client,  # type: ignore[arg-type]
        resolver=mock_dns_resolver,
    )

    assert consumer_called is True
    assert extracted_path is not None
    assert not extracted_path.exists()
    assert len(list(tmp_path.glob("sourcetrace_*"))) == 0


def test_zip_composition_invokes_consumer_inside_extraction_context(tmp_path: Path):
    """Verify ZIP acquisition extracts archive and yields acquired source."""
    zip_bytes = create_valid_zip_bytes({"utils.py": "x = 42\n"})

    consumer_called = False
    extracted_path: Path | None = None

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_called, extracted_path
        consumer_called = True
        extracted_path = source.extraction_root

        assert source.source_type == "zip"
        assert source.extraction_root.exists()
        assert source.manifest.file_count == 1
        assert "utils.py" in source.manifest.relative_paths

        target_file = source.extraction_root / "utils.py"
        assert target_file.exists()
        assert target_file.read_text(encoding="utf-8") == "x = 42\n"

    acquire_zip_source(
        archive_source=zip_bytes,
        consumer=consumer,
        parent_dir=tmp_path,
    )

    assert consumer_called is True
    assert extracted_path is not None
    assert not extracted_path.exists()
    assert len(list(tmp_path.glob("sourcetrace_*"))) == 0


def test_download_and_extraction_directories_removed_after_consumer_exception(tmp_path: Path):
    """Verify temporary directories are removed even when consumer raises an exception."""
    zip_bytes = create_valid_zip_bytes({"app.py": "import sys\n"})
    client = MockHTTPClient(zip_bytes)

    extracted_dir: Path | None = None

    def failing_consumer(source: AcquiredSource) -> None:
        nonlocal extracted_dir
        extracted_dir = source.extraction_root
        assert source.extraction_root.exists()
        raise RuntimeError("Simulated consumer failure")

    with pytest.raises(RuntimeError, match="Simulated consumer failure"):
        acquire_github_source(
            url="https://github.com/test-owner/test-repo",
            consumer=failing_consumer,
            parent_dir=tmp_path,
            client=client,  # type: ignore[arg-type]
            resolver=mock_dns_resolver,
        )

    assert extracted_dir is not None
    assert not extracted_dir.exists()
    assert len(list(tmp_path.glob("sourcetrace_*"))) == 0


def test_consumer_never_called_for_invalid_archive(tmp_path: Path):
    """Verify consumer is not invoked if ZIP archive is invalid."""
    consumer_called = False

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_called
        consumer_called = True

    with pytest.raises(InvalidArchiveError):
        acquire_zip_source(
            archive_source=b"not a valid zip file content",
            consumer=consumer,
            parent_dir=tmp_path,
        )

    assert consumer_called is False
    assert len(list(tmp_path.glob("sourcetrace_*"))) == 0


def test_consumer_never_called_for_rejected_url_redirect_ip(tmp_path: Path):
    """Verify consumer is not invoked if GitHub URL scheme or IP is disallowed."""
    consumer_called = False

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_called
        consumer_called = True

    with pytest.raises(InvalidRepositoryURLError):
        acquire_github_source(
            url="http://github.com/test-owner/test-repo",
            consumer=consumer,
            parent_dir=tmp_path,
            resolver=mock_dns_resolver,
        )
    assert consumer_called is False

    with pytest.raises(InvalidRepositoryURLError):
        acquire_github_source(
            url="https://evil.com/test-owner/test-repo",
            consumer=consumer,
            parent_dir=tmp_path,
            resolver=mock_dns_resolver,
        )
    assert consumer_called is False


def test_github_acquisition_disallowed_redirect_rejection(tmp_path: Path):
    """Verify consumer is not invoked if GitHub archive download encounters disallowed redirect."""
    consumer_called = False

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_called
        consumer_called = True

    class RedirectMockHTTPClient:
        def stream(self, method: str, url: str, **kwargs):
            class StreamContext:
                def __enter__(self):
                    class Resp:
                        status_code = 302
                        headers = {"Location": "https://evil.com/archive.zip"}
                    return Resp()

                def __exit__(self, *args):
                    pass
            return StreamContext()

        def close(self):
            pass

    with pytest.raises(DisallowedRedirectError):
        acquire_github_source(
            url="https://github.com/test-owner/test-repo",
            consumer=consumer,
            parent_dir=tmp_path,
            client=RedirectMockHTTPClient(),  # type: ignore[arg-type]
            resolver=mock_dns_resolver,
        )
    assert consumer_called is False


def test_acquisition_runner_state_transitions_and_handoff_success(tmp_path: Path):
    """Verify runner transitions repo/job to indexing/acquiring -> scanning -> consumer."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_test123"
    repository_id = "repo_test123"
    job_id = "job_test123"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="test-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for archive processing",
            created_at=now,
            updated_at=now,
            progress_percentage=0,
        )
    )

    zip_bytes = create_valid_zip_bytes({"test.py": "x = 1\n"})
    consumer_ran = False
    consumer_repo_status = None
    consumer_job_status = None
    consumer_job_step = None

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_ran, consumer_repo_status, consumer_job_status, consumer_job_step
        consumer_ran = True

        r = repo_storage.get_by_id(owner_session_id, repository_id)
        j = job_storage.get_by_id(owner_session_id, job_id)

        assert r is not None
        assert j is not None
        consumer_repo_status = r.status
        consumer_job_status = j.status
        consumer_job_step = j.current_step

    runner.run_acquisition(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        job_id=job_id,
        source_input=zip_bytes,
        consumer=consumer,
        parent_dir=tmp_path,
        now=now,
    )

    assert consumer_ran is True
    assert consumer_repo_status == "indexing"
    assert consumer_job_status == "scanning"
    assert consumer_job_step == "Scanning source files"

    final_repo = repo_storage.get_by_id(owner_session_id, repository_id)
    final_job = job_storage.get_by_id(owner_session_id, job_id)
    assert final_repo is not None and final_repo.status == "indexing"
    assert final_job is not None and final_job.status == "scanning"


# ---------------------------------------------------------------------------
# Section 5 Correction & Integrity Tests
# ---------------------------------------------------------------------------


def test_stored_canonical_github_url_used_for_acquisition(tmp_path: Path):
    """Verify GitHub acquisition strictly uses stored canonical repository.github_url."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_gh_stored"
    repository_id = "repo_gh_stored"
    job_id = "job_gh_stored"
    now = datetime.now(UTC)

    stored_github_url = "https://github.com/stored-owner/stored-repo"

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="stored-repo",
            source_type="github",
            status="pending",
            created_at=now,
            updated_at=now,
            github_url=stored_github_url,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for acquisition",
            created_at=now,
            updated_at=now,
        )
    )

    zip_bytes = create_valid_zip_bytes({"stored.py": "a = 10\n"})
    client = MockHTTPClient(zip_bytes)

    consumer_ran = False

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_ran
        consumer_ran = True
        assert source.source_type == "github"

    runner.run_acquisition(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        job_id=job_id,
        source_input=None,  # No caller-provided URL passed
        consumer=consumer,
        parent_dir=tmp_path,
        client=client,  # type: ignore[arg-type]
        resolver=mock_dns_resolver,
        now=now,
    )

    assert consumer_ran is True


def test_missing_stored_github_url_fails_before_state_mutation(tmp_path: Path):
    """Verify GitHub repository with missing/empty github_url fails before state mutation."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_no_url"
    repository_id = "repo_no_url"
    job_id = "job_no_url"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="no-url-repo",
            source_type="github",
            status="pending",
            created_at=now,
            updated_at=now,
            github_url=None,  # Missing stored URL
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for acquisition",
            created_at=now,
            updated_at=now,
        )
    )

    # Save counts before preflight
    initial_repo_saves = len(repo_storage.save_calls)
    initial_job_saves = len(job_storage.save_calls)

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=None,
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    # No saves attempted
    assert len(repo_storage.save_calls) == initial_repo_saves
    assert len(job_storage.save_calls) == initial_job_saves


def test_invalid_persisted_source_type_fails_before_state_mutation(tmp_path: Path):
    """Verify repository with invalid source_type fails before state mutation."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_inv_type"
    repository_id = "repo_inv_type"
    job_id = "job_inv_type"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="invalid-repo",
            source_type="unsupported_source_type",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for processing",
            created_at=now,
            updated_at=now,
        )
    )

    initial_repo_saves = len(repo_storage.save_calls)
    initial_job_saves = len(job_storage.save_calls)

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    assert len(repo_storage.save_calls) == initial_repo_saves
    assert len(job_storage.save_calls) == initial_job_saves


def test_repository_lookup_exception_becomes_fixed_safe_error(tmp_path: Path):
    """Verify raw exception during repository lookup becomes fixed safe AcquisitionError."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    repo_storage.fail_get_by_id = True

    with pytest.raises(AcquisitionError, match="Acquisition failed safely.") as exc_info:
        runner.run_acquisition(
            owner_session_id="sess_1",
            repository_id="repo_1",
            job_id="job_1",
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    assert "secret_db_uri" not in str(exc_info.value)


def test_job_lookup_exception_becomes_fixed_safe_error(tmp_path: Path):
    """Verify raw exception during job lookup becomes fixed safe AcquisitionError."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    job_storage.fail_get_by_id = True

    with pytest.raises(AcquisitionError, match="Acquisition failed safely.") as exc_info:
        runner.run_acquisition(
            owner_session_id="sess_1",
            repository_id="repo_1",
            job_id="job_1",
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    assert "secret_db_uri" not in str(exc_info.value)


def test_initial_repository_save_failure_is_masked(tmp_path: Path):
    """Verify raw failure saving initial repository status is masked and finalization runs."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_mask1"
    repository_id = "repo_mask1"
    job_id = "job_mask1"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="mask-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for archive processing",
            created_at=now,
            updated_at=now,
        )
    )

    # Fail saving status "indexing"
    repo_storage.fail_save_status = {"indexing"}

    with pytest.raises(AcquisitionError, match="Acquisition failed safely.") as exc_info:
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    assert "Raw MongoDB" not in str(exc_info.value)
    # Failed job state was attempted and saved in finalization
    failed_job = job_storage.get_by_id(owner_session_id, job_id)
    assert failed_job is not None and failed_job.status == "failed"


def test_initial_job_save_failure_is_masked(tmp_path: Path):
    """Verify raw failure saving initial job status is masked and finalization runs."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_mask2"
    repository_id = "repo_mask2"
    job_id = "job_mask2"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="mask-repo2",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for archive processing",
            created_at=now,
            updated_at=now,
        )
    )

    # Fail saving job status "acquiring"
    job_storage.fail_save_status = {"acquiring"}

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    # Pre-claim failure MUST NOT run finalization (repository remains pending)
    repo = repo_storage.get_by_id(owner_session_id, repository_id)
    assert repo is not None and repo.status == "pending"



def test_scanning_state_save_failure_is_masked_and_consumer_not_called(tmp_path: Path):
    """Verify failure saving scanning job state is masked and consumer is not invoked."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_scan_fail"
    repository_id = "repo_scan_fail"
    job_id = "job_scan_fail"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="scan-fail-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for processing",
            created_at=now,
            updated_at=now,
        )
    )

    # Fail saving status "scanning"
    job_storage.fail_save_status = {"scanning"}
    consumer_ran = False

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_ran
        consumer_ran = True

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=consumer,
            parent_dir=tmp_path,
        )

    assert consumer_ran is False
    assert len(list(tmp_path.glob("sourcetrace_*"))) == 0


def test_failure_saving_failed_repository_does_not_prevent_failed_job_attempt(tmp_path: Path):
    """Verify failure to save failed repository does not block attempting failed job save."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_best_effort"
    repository_id = "repo_best_effort"
    job_id = "job_best_effort"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="be-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for processing",
            created_at=now,
            updated_at=now,
        )
    )

    # Fail saving repository status "failed"
    repo_storage.fail_save_status = {"failed"}

    def failing_consumer(source: AcquiredSource) -> None:
        raise ValueError("Consumer unexpected error")

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=failing_consumer,
            parent_dir=tmp_path,
        )

    # Job save as failed succeeded despite repository save failing
    failed_job = job_storage.get_by_id(owner_session_id, job_id)
    assert failed_job is not None and failed_job.status == "failed"


def test_failure_saving_failed_job_does_not_leak_raw_storage_details(tmp_path: Path):
    """Verify failure saving failed job suppresses raw error and returns clean AcquisitionError."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_job_fail"
    repository_id = "repo_job_fail"
    job_id = "job_job_fail"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="job-fail-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for processing",
            created_at=now,
            updated_at=now,
        )
    )

    # Fail saving job status "failed"
    job_storage.fail_save_status = {"failed"}

    with pytest.raises(AcquisitionError, match="Acquisition failed safely.") as exc_info:
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: (_ for _ in ()).throw(ValueError("Raw error")),
            parent_dir=tmp_path,
        )

    assert "Raw error" not in str(exc_info.value)
    assert "Raw MongoDB" not in str(exc_info.value)


def test_consumer_raised_acquisition_error_replaced_by_fixed_safe_error(tmp_path: Path):
    """Verify consumer-raised AcquisitionError with sensitive details is replaced by safe error."""

    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_cons_err"
    repository_id = "repo_cons_err"
    job_id = "job_cons_err"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="cons-err-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for processing",
            created_at=now,
            updated_at=now,
        )
    )

    def consumer_with_sensitive_error(source: AcquiredSource) -> None:
        raise AcquisitionError("Unsafe internal detail: /var/secrets/key.pem at 10.0.0.5")

    with pytest.raises(AcquisitionError, match="Acquisition failed safely.") as exc_info:
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=consumer_with_sensitive_error,
            parent_dir=tmp_path,
        )

    assert "key.pem" not in str(exc_info.value)
    assert "10.0.0.5" not in str(exc_info.value)


def test_managed_temporary_directories_removed_for_every_failure_path(tmp_path: Path):
    """Verify temp extraction folders are removed across all failure conditions."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_cleanup_all"
    repository_id = "repo_cleanup_all"
    job_id = "job_cleanup_all"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="cleanup-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued for processing",
            created_at=now,
            updated_at=now,
        )
    )

    def failing_consumer(source: AcquiredSource) -> None:
        assert source.extraction_root.exists()
        raise Exception("Consumer crash")

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=failing_consumer,
            parent_dir=tmp_path,
        )

    # Ensure no leftover sourcetrace_* directories
    assert len(list(tmp_path.glob("sourcetrace_*"))) == 0


def test_only_one_worker_claims_queued_job_and_second_runner_invokes_no_consumer(tmp_path: Path):
    """Verify only one worker can claim a queued job; second runner fails without callback."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_concurrent"
    repository_id = "repo_concurrent"
    job_id = "job_concurrent"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="conc-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    zip_bytes = create_valid_zip_bytes()
    worker1_ran = False
    worker2_ran = False

    def consumer_worker1(source: AcquiredSource) -> None:
        nonlocal worker1_ran
        worker1_ran = True

    def consumer_worker2(source: AcquiredSource) -> None:
        nonlocal worker2_ran
        worker2_ran = True

    runner.run_acquisition(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        job_id=job_id,
        source_input=zip_bytes,
        consumer=consumer_worker1,
        parent_dir=tmp_path,
    )

    assert worker1_ran is True

    with pytest.raises(AcquisitionError):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=zip_bytes,
            consumer=consumer_worker2,
            parent_dir=tmp_path,
        )

    assert worker2_ran is False



def test_already_acquiring_or_ready_or_failed_job_cannot_be_claimed(tmp_path: Path):
    """Verify an acquiring/ready/failed repository cannot be restarted through runner."""

    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_restart"
    repository_id = "repo_restart"
    job_id = "job_restart"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="restart-repo",
            source_type="zip",
            status="indexing",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="acquiring",
            current_step="Acquiring",
            created_at=now,
            updated_at=now,
        )
    )

    zip_bytes = create_valid_zip_bytes()
    consumer_ran = False

    with pytest.raises(AcquisitionError, match="Resource missing or owned by another session."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=zip_bytes,
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    assert consumer_ran is False


def test_repository_transition_failure_marks_claimed_job_failed_best_effort(tmp_path: Path):
    """Verify failure to transition repository to indexing marks claimed job failed."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_repo_trans_fail"
    repository_id = "repo_repo_trans_fail"
    job_id = "job_repo_trans_fail"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="trans-fail-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    repo_storage.fail_save_status = {"indexing"}
    consumer_ran = False

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_ran
        consumer_ran = True

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=consumer,
            parent_dir=tmp_path,
        )

    assert consumer_ran is False
    job_record = job_storage.get_by_id(owner_session_id, job_id)
    assert job_record is not None
    assert job_record.status == "failed"


def test_deleted_repository_not_recreated_during_transition_or_finalization(tmp_path: Path):
    """Verify a repository deleted during processing is not recreated via upsert."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_del_repo"
    repository_id = "repo_del_repo"
    job_id = "job_del_repo"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="del-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    zip_bytes = create_valid_zip_bytes()

    def consumer_deletes_repo(source: AcquiredSource) -> None:
        repo_storage.delete(owner_session_id, repository_id)
        raise RuntimeError("Consumer failure after repository deletion")

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=zip_bytes,
            consumer=consumer_deletes_repo,
            parent_dir=tmp_path,
        )

    assert repo_storage.get_by_id(owner_session_id, repository_id) is None


def test_deleted_job_not_recreated_during_scanning_or_finalization(tmp_path: Path):
    """Verify a job deleted during processing is not recreated via upsert."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_del_job"
    repository_id = "repo_del_job"
    job_id = "job_del_job"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="del-job-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    zip_bytes = create_valid_zip_bytes()

    def consumer_deletes_job(source: AcquiredSource) -> None:
        job_storage.delete_by_repository(owner_session_id, repository_id)
        raise RuntimeError("Consumer crash after job deletion")

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=zip_bytes,
            consumer=consumer_deletes_job,
            parent_dir=tmp_path,
        )

    assert job_storage.get_by_id(owner_session_id, job_id) is None


def test_conditional_transition_returning_none_does_not_expose_ids(tmp_path: Path):
    """Verify transition failure returning None produces clean error without leaking IDs."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_no_leak_id"
    repository_id = "repo_no_leak_id"
    job_id = "job_no_leak_id"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="no-leak-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.delete_by_repository(owner_session_id, repository_id)

    with pytest.raises(AcquisitionError) as exc_info:
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    err_str = str(exc_info.value)
    assert repository_id not in err_str
    assert job_id not in err_str
    assert owner_session_id not in err_str


def test_claim_loss_race_worker2_returns_none_without_finalization_or_mutation(tmp_path: Path):
    """Verify Worker 2 getting None on claim raises AcquisitionError without mutating state."""
    repo_storage = ConfigurableRepositoryRepository()

    job_storage = ConfigurableIndexingJobRepository()

    owner_session_id = "sess_race_interleave"
    repository_id = "repo_race_interleave"
    job_id = "job_race_interleave"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="race-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    # Worker 2 preflight sees repository=pending and job=queued
    repo_preflight = repo_storage.get_by_id(owner_session_id, repository_id)
    job_preflight = job_storage.get_by_id(owner_session_id, job_id)
    assert repo_preflight is not None and repo_preflight.status == "pending"
    assert job_preflight is not None and job_preflight.status == "queued"

    # Worker 1 wins the queued -> acquiring claim in between Worker 2 preflight and claim
    worker1_claimed = job_storage.transition_status(
        owner_session_id=owner_session_id,
        job_id=job_id,
        repository_id=repository_id,
        expected_status="queued",
        new_status="acquiring",
        current_step="Acquiring source repository",
        progress_percentage=15,
        updated_at=now,
    )
    assert worker1_claimed is not None

    # Worker 2's claim transition returns None because job status is now acquiring
    worker2_claim_result = job_storage.transition_status(
        owner_session_id=owner_session_id,
        job_id=job_id,
        repository_id=repository_id,
        expected_status="queued",
        new_status="acquiring",
        current_step="Acquiring source repository",
        progress_percentage=15,
        updated_at=now,
    )
    assert worker2_claim_result is None

    # Simulate Worker 2 running acquisition where preflight succeeded but claim returns None
    class ClaimLossJobRepo(ConfigurableIndexingJobRepository):
        def transition_status(self, *args, **kwargs) -> IndexingJobRecord | None:
            return None

    loss_job_storage = ClaimLossJobRepo()
    loss_job_storage._jobs = dict(job_storage._jobs)
    # Ensure preflight returns the queued job for Worker 2
    loss_job_storage._jobs[(owner_session_id, job_id)] = IndexingJobRecord(
        job_id=job_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        status="queued",
        current_step="Queued",
        created_at=now,
        updated_at=now,
    )
    loss_runner = AcquisitionRunner(repo_storage, loss_job_storage)

    worker2_consumer_ran = False

    def consumer_worker2(source: AcquiredSource) -> None:
        nonlocal worker2_consumer_ran
        worker2_consumer_ran = True

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        loss_runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=consumer_worker2,
            parent_dir=tmp_path,
        )

    # Prove Worker 2:
    # 1. raises fixed safe AcquisitionError
    # 2. does not invoke consumer callback
    assert worker2_consumer_ran is False

    # 3. does not alter repository pending state or attempt repo failure finalization
    repo_after = repo_storage.get_by_id(owner_session_id, repository_id)
    assert repo_after is not None
    assert repo_after.status == "pending"
    assert len([r for r in repo_storage.save_calls if r.status == "failed"]) == 0

    # 4. does not call job failure transition on Worker 1's acquiring job
    assert len([j for j in loss_job_storage.save_calls if j.status == "failed"]) == 0



def test_atomic_claim_exception_causes_no_finalization_attempts(tmp_path: Path):
    """Verify claim exception raises safe error without running finalization."""
    repo_storage = ConfigurableRepositoryRepository()

    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_claim_exc"
    repository_id = "repo_claim_exc"
    job_id = "job_claim_exc"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="claim-exc-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    # Fail job claim transition to 'acquiring'
    job_storage.fail_save_status = {"acquiring"}
    consumer_ran = False

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    assert consumer_ran is False

    # Finalization must NOT have been called (no status='failed' saves recorded)
    failed_job_saves = [
        job for job in job_storage.save_calls if job.status == "failed"
    ]
    failed_repo_saves = [
        repo for repo in repo_storage.save_calls if repo.status == "failed"
    ]
    assert len(failed_job_saves) == 0
    assert len(failed_repo_saves) == 0


def test_confirmed_claim_followed_by_repo_transition_failure_performs_finalization(tmp_path: Path):
    """Verify confirmed claim followed by repository transition failure executes finalization."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_confirm_fail"
    repository_id = "repo_confirm_fail"
    job_id = "job_confirm_fail"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="confirm-fail-repo",
            source_type="zip",
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
    )

    # Repository transition to 'indexing' will fail
    repo_storage.fail_save_status = {"indexing"}

    with pytest.raises(AcquisitionError, match="Acquisition failed safely."):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda src: None,
            parent_dir=tmp_path,
        )

    # Claim succeeded, repo transition failed -> finalization MUST mark job failed
    job_after = job_storage.get_by_id(owner_session_id, job_id)
    assert job_after is not None
    assert job_after.status == "failed"
    assert job_after.error_message == "Acquisition failed safely."


@pytest.mark.parametrize(
    "ineligible_repo_status,ineligible_job_status",
    [
        ("indexing", "acquiring"),
        ("indexing", "scanning"),
        ("indexing", "parsing"),
        ("indexing", "embedding"),
        ("indexing", "storing"),
        ("ready", "ready"),
        ("failed", "failed"),
        ("pending", "acquiring"),
        ("indexing", "queued"),
    ],
)
def test_ineligible_starting_states_cannot_be_claimed_or_restarted(
    tmp_path: Path, ineligible_repo_status: str, ineligible_job_status: str
):
    """Verify resources in any non-pending/queued state cannot be restarted or claimed."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_ineligible"
    repository_id = "repo_ineligible"
    job_id = "job_ineligible"
    now = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="ineligible-repo",
            source_type="zip",
            status=ineligible_repo_status,
            created_at=now,
            updated_at=now,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status=ineligible_job_status,
            current_step="Step",
            created_at=now,
            updated_at=now,
        )
    )

    consumer_ran = False

    def consumer(source: AcquiredSource) -> None:
        nonlocal consumer_ran
        consumer_ran = True

    with pytest.raises(
        AcquisitionError, match="Resource missing or owned by another session."
    ):
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=consumer,
            parent_dir=tmp_path,
        )

    assert consumer_ran is False


class HostileNow:

    @property
    def tzinfo(self) -> None:
        raise RuntimeError("Hostile tzinfo access: secret_key_999")


@pytest.mark.parametrize(
    "invalid_now",
    [
        "2026-07-24T18:00:00Z",
        123456789,
        True,
        False,
        HostileNow(),
    ],
)
def test_invalid_now_raises_acquisition_error_before_claim(
    tmp_path: Path, invalid_now: object
) -> None:
    """Verify invalid now values raise fixed safe AcquisitionError before making any claim."""
    repo_storage = ConfigurableRepositoryRepository()
    job_storage = ConfigurableIndexingJobRepository()
    runner = AcquisitionRunner(repo_storage, job_storage)

    owner_session_id = "sess_now"
    repository_id = "repo_now"
    job_id = "job_now"
    now_valid = datetime.now(UTC)

    repo_storage.save(
        RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="now-repo",
            source_type="zip",
            status="pending",
            created_at=now_valid,
            updated_at=now_valid,
        )
    )

    job_storage.save(
        IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step="Queued",
            created_at=now_valid,
            updated_at=now_valid,
        )
    )

    with pytest.raises(AcquisitionError) as exc_info:
        runner.run_acquisition(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            source_input=create_valid_zip_bytes(),
            consumer=lambda s: None,
            parent_dir=tmp_path,
            now=invalid_now,  # type: ignore
        )

    err_str = str(exc_info.value)
    assert err_str == "Acquisition failed safely."
    assert "secret_key_999" not in err_str

    repo_record = repo_storage.get_by_id(owner_session_id, repository_id)
    assert repo_record is not None
    assert repo_record.status == "pending"

    job_record = job_storage.get_by_id(owner_session_id, job_id)
    assert job_record is not None
    assert job_record.status == "queued"




