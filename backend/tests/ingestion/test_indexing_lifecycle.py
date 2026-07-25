"""Offline integration tests for IndexingLifecycleCoordinator and lifecycle observer."""

from __future__ import annotations

import io
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sourcetrace.core.exceptions import AcquisitionError, IndexingError
from sourcetrace.ingestion.acquisition import AcquiredSource, AcquisitionRunner
from sourcetrace.ingestion.archive import ExtractionManifest
from sourcetrace.ingestion.indexing import RepositoryIndexingService
from sourcetrace.ingestion.lifecycle import IndexingLifecycleCoordinator
from sourcetrace.models.domain import CodeChunk, IndexingJobRecord, RepositoryRecord


class FakeEmbeddingProvider:

    def __init__(self, embedding_dimensions: int = 4) -> None:
        self.embedding_dimensions = embedding_dimensions
        self.model_identifier = "text-embedding-3-small"
        self.embed_calls = 0
        self.should_raise: Exception | None = None

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if self.should_raise is not None:
            raise self.should_raise
        self.embed_calls += 1
        return [tuple(0.1 for _ in range(self.embedding_dimensions)) for _ in texts]


class FakeCodeChunkRepository:

    def __init__(self) -> None:
        self.saved_chunks: list[CodeChunk] = []
        self.save_many_calls = 0

    def save_many(self, chunks: list[CodeChunk]) -> int:
        self.save_many_calls += 1
        self.saved_chunks.extend(chunks)
        return len(chunks)

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[CodeChunk]:
        return [
            c for c in self.saved_chunks
            if c.owner_session_id == owner_session_id and c.repository_id == repository_id
        ]

    def search_vectors(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: list[float],
        limit: int = 5,
    ) -> list[Any]:
        return []

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:
        return 0


class FakeRepositoryRepository:

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], RepositoryRecord] = {}
        self.transition_calls: list[dict[str, Any]] = []

    def get_by_id(
        self, owner_session_id: str, repository_id: str
    ) -> RepositoryRecord | None:
        return self.records.get((owner_session_id, repository_id))

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for (owner, _), r in self.records.items() if owner == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        self.records[(repository.owner_session_id, repository.repository_id)] = repository
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
        call_info = {
            "owner_session_id": owner_session_id,
            "repository_id": repository_id,
            "expected_status": expected_status,
            "new_status": new_status,
            "updated_at": updated_at,
            "file_count": file_count,
            "chunk_count": chunk_count,
        }
        self.transition_calls.append(call_info)
        rec = self.get_by_id(owner_session_id, repository_id)
        if rec is None:
            return None

        exp_set = (expected_status,) if isinstance(expected_status, str) else expected_status
        if rec.status not in exp_set:
            return None

        new_rec = RepositoryRecord(
            repository_id=rec.repository_id,
            owner_session_id=rec.owner_session_id,
            name=rec.name,
            source_type=rec.source_type,
            status=new_status,
            created_at=rec.created_at,
            updated_at=updated_at,
            github_url=rec.github_url,
            file_count=file_count if file_count is not None else rec.file_count,
            chunk_count=chunk_count if chunk_count is not None else rec.chunk_count,
            index_mode=rec.index_mode,
        )
        self.records[(owner_session_id, repository_id)] = new_rec
        return new_rec

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        return self.records.pop((owner_session_id, repository_id), None) is not None


class FakeIndexingJobRepository:

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], IndexingJobRecord] = {}
        self.transition_calls: list[dict[str, Any]] = []

    def get_by_id(
        self, owner_session_id: str, job_id: str
    ) -> IndexingJobRecord | None:
        return self.records.get((owner_session_id, job_id))

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        for (owner, _), j in self.records.items():
            if owner == owner_session_id and j.repository_id == repository_id:
                return j
        return None

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        self.records[(job.owner_session_id, job.job_id)] = job
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
        call_info = {
            "owner_session_id": owner_session_id,
            "job_id": job_id,
            "repository_id": repository_id,
            "expected_status": expected_status,
            "new_status": new_status,
            "current_step": current_step,
            "progress_percentage": progress_percentage,
            "updated_at": updated_at,
            "error_message": error_message,
            "completed_at": completed_at,
        }
        self.transition_calls.append(call_info)
        rec = self.get_by_id(owner_session_id, job_id)
        if rec is None or rec.repository_id != repository_id:
            return None

        exp_set = (expected_status,) if isinstance(expected_status, str) else expected_status
        if rec.status not in exp_set:
            return None

        new_prog = (
            progress_percentage
            if progress_percentage is not None
            else rec.progress_percentage
        )
        new_rec = IndexingJobRecord(
            job_id=rec.job_id,
            repository_id=rec.repository_id,
            owner_session_id=rec.owner_session_id,
            status=new_status,
            current_step=current_step,
            created_at=rec.created_at,
            updated_at=updated_at,
            progress_percentage=new_prog,
            error_message=error_message,
            completed_at=completed_at,
        )
        self.records[(owner_session_id, job_id)] = new_rec
        return new_rec

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:
        to_del = [
            k for k, v in self.records.items()
            if k[0] == owner_session_id and v.repository_id == repository_id
        ]
        for k in to_del:
            del self.records[k]
        return len(to_del)


def _create_zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_path, content in files.items():
            zf.writestr(rel_path, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test Cases for Indexing Lifecycle
# ---------------------------------------------------------------------------


def test_successful_full_lifecycle_integration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_bytes = _create_zip_bytes({"main.py": "def foo(): pass\n"})

        provider = FakeEmbeddingProvider()
        chunk_repo = FakeCodeChunkRepository()
        indexing_service = RepositoryIndexingService(
            provider=provider, code_chunk_repo=chunk_repo
        )

        repo_repo = FakeRepositoryRepository()
        job_repo = FakeIndexingJobRepository()

        owner_id = "sess_100"
        repo_id = "repo_200"
        job_id = "job_300"
        now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)

        repo_repo.save(RepositoryRecord(
            repository_id=repo_id,
            owner_session_id=owner_id,
            name="test_repo",
            source_type="zip",
            status="pending",
            created_at=now_dt,
            updated_at=now_dt,
        ))
        job_repo.save(IndexingJobRecord(
            job_id=job_id,
            repository_id=repo_id,
            owner_session_id=owner_id,
            status="queued",
            current_step="Queued for acquisition",
            created_at=now_dt,
            updated_at=now_dt,
            progress_percentage=0,
        ))

        coordinator = IndexingLifecycleCoordinator(
            repository_repo=repo_repo,
            job_repo=job_repo,
            indexing_service=indexing_service,
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            now=now_dt,
        )

        runner = AcquisitionRunner(repository_repo=repo_repo, job_repo=job_repo)
        runner.run_acquisition(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            source_input=zip_bytes,
            consumer=coordinator.consume,
            parent_dir=tmp,
            now=now_dt,
            source_type="zip",
        )

        # Check final repository state
        final_repo = repo_repo.get_by_id(owner_id, repo_id)
        assert final_repo is not None
        assert final_repo.status == "ready"
        assert final_repo.file_count == 1
        assert final_repo.chunk_count == 1

        # Check final job state
        final_job = job_repo.get_by_id(owner_id, job_id)
        assert final_job is not None
        assert final_job.status == "ready"
        assert final_job.current_step == "Repository ready"
        assert final_job.progress_percentage == 100
        assert final_job.completed_at == now_dt
        assert final_job.error_message is None

        # Verify job transition order
        job_statuses = [c["new_status"] for c in job_repo.transition_calls]
        expected_statuses = [
            "acquiring",
            "scanning",
            "parsing",
            "embedding",
            "storing",
            "ready",
        ]
        assert job_statuses == expected_statuses

        # Verify job progress values
        progress_values = [c["progress_percentage"] for c in job_repo.transition_calls]
        assert progress_values == [15, 30, 45, 65, 85, 100]


def test_empty_repository_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_bytes = _create_zip_bytes({"README.md": "text only"})

        provider = FakeEmbeddingProvider()
        chunk_repo = FakeCodeChunkRepository()
        indexing_service = RepositoryIndexingService(
            provider=provider, code_chunk_repo=chunk_repo
        )

        repo_repo = FakeRepositoryRepository()
        job_repo = FakeIndexingJobRepository()

        owner_id = "sess_empty"
        repo_id = "repo_empty"
        job_id = "job_empty"
        now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)

        repo_repo.save(RepositoryRecord(
            repository_id=repo_id,
            owner_session_id=owner_id,
            name="empty_repo",
            source_type="zip",
            status="pending",
            created_at=now_dt,
            updated_at=now_dt,
        ))
        job_repo.save(IndexingJobRecord(
            job_id=job_id,
            repository_id=repo_id,
            owner_session_id=owner_id,
            status="queued",
            current_step="Queued for acquisition",
            created_at=now_dt,
            updated_at=now_dt,
            progress_percentage=0,
        ))

        coordinator = IndexingLifecycleCoordinator(
            repository_repo=repo_repo,
            job_repo=job_repo,
            indexing_service=indexing_service,
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            now=now_dt,
        )

        runner = AcquisitionRunner(repository_repo=repo_repo, job_repo=job_repo)
        runner.run_acquisition(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            source_input=zip_bytes,
            consumer=coordinator.consume,
            parent_dir=tmp,
            now=now_dt,
            source_type="zip",
        )

        final_repo = repo_repo.get_by_id(owner_id, repo_id)
        assert final_repo is not None
        assert final_repo.status == "ready"
        assert final_repo.file_count == 0
        assert final_repo.chunk_count == 0

        final_job = job_repo.get_by_id(owner_id, job_id)
        assert final_job is not None
        assert final_job.status == "ready"
        assert final_job.progress_percentage == 100

        assert provider.embed_calls == 0
        assert chunk_repo.save_many_calls == 0


def test_transition_races() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_bytes = _create_zip_bytes({"main.py": "def foo(): pass\n"})

        now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)
        owner_id = "sess_race"
        repo_id = "repo_race"
        job_id = "job_race"

        steps = ("parsing", "embedding", "storing", "repo_ready", "job_ready")
        for fail_step in steps:
            provider = FakeEmbeddingProvider()
            chunk_repo = FakeCodeChunkRepository()
            indexing_service = RepositoryIndexingService(
                provider=provider, code_chunk_repo=chunk_repo
            )

            repo_repo = FakeRepositoryRepository()
            job_repo = FakeIndexingJobRepository()

            repo_repo.save(RepositoryRecord(
                repository_id=repo_id, owner_session_id=owner_id, name="race_repo",
                source_type="zip", status="pending", created_at=now_dt, updated_at=now_dt,
            ))
            job_repo.save(IndexingJobRecord(
                job_id=job_id, repository_id=repo_id, owner_session_id=owner_id,
                status="queued", current_step="Queued for acquisition",
                created_at=now_dt, updated_at=now_dt, progress_percentage=0,
            ))

            orig_job_trans = job_repo.transition_status
            orig_repo_trans = repo_repo.transition_status

            def make_bad_job(target_step: str, orig_fn: Any) -> Any:
                def bad_job_trans(*args: Any, **kwargs: Any) -> Any:
                    st = kwargs.get("new_status")
                    if st is None and len(args) >= 5:
                        st = args[4]
                    if st == target_step:
                        return None
                    return orig_fn(*args, **kwargs)
                return bad_job_trans

            def make_bad_repo(orig_fn: Any) -> Any:
                def bad_repo_trans(*args: Any, **kwargs: Any) -> Any:
                    st = kwargs.get("new_status")
                    if st is None and len(args) >= 4:
                        st = args[3]
                    if st == "ready":
                        return None
                    return orig_fn(*args, **kwargs)
                return bad_repo_trans

            target_step = "ready" if fail_step == "job_ready" else fail_step

            if fail_step in ("parsing", "embedding", "storing", "job_ready"):
                job_repo.transition_status = make_bad_job(target_step, orig_job_trans)  # type: ignore
            else:
                repo_repo.transition_status = make_bad_repo(orig_repo_trans)  # type: ignore

            coordinator = IndexingLifecycleCoordinator(
                repository_repo=repo_repo,
                job_repo=job_repo,
                indexing_service=indexing_service,
                owner_session_id=owner_id,
                repository_id=repo_id,
                job_id=job_id,
                now=now_dt,
            )
            runner = AcquisitionRunner(repository_repo=repo_repo, job_repo=job_repo)

            with pytest.raises((AcquisitionError, IndexingError)) as exc_info:
                runner.run_acquisition(
                    owner_session_id=owner_id,
                    repository_id=repo_id,
                    job_id=job_id,
                    source_input=zip_bytes,
                    consumer=coordinator.consume,
                    parent_dir=tmp,
                    now=now_dt,
                    source_type="zip",
                )
            assert "safely" in str(exc_info.value)

            final_job = job_repo.get_by_id(owner_id, job_id)
            assert final_job is not None
            assert final_job.status == "failed"
            assert final_job.error_message in (
                "Indexing failed safely.",
                "Acquisition failed safely.",
            )

            final_repo = repo_repo.get_by_id(owner_id, repo_id)
            assert final_repo is not None
            assert final_repo.status == "failed"


def test_phase_failures_preserve_progress() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_bytes = _create_zip_bytes({"main.py": "def foo(): pass\n"})

        now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)
        owner_id = "sess_fail"
        repo_id = "repo_fail"
        job_id = "job_fail"

        secret_exc = RuntimeError("Secret provider key sk-proj-999 failed at /etc/shadow")
        failing_provider = FakeEmbeddingProvider()
        failing_provider.should_raise = secret_exc

        chunk_repo = FakeCodeChunkRepository()
        indexing_service = RepositoryIndexingService(
            provider=failing_provider, code_chunk_repo=chunk_repo
        )

        repo_repo = FakeRepositoryRepository()
        job_repo = FakeIndexingJobRepository()

        repo_repo.save(RepositoryRecord(
            repository_id=repo_id, owner_session_id=owner_id, name="fail_repo",
            source_type="zip", status="pending", created_at=now_dt, updated_at=now_dt,
        ))
        job_repo.save(IndexingJobRecord(
            job_id=job_id, repository_id=repo_id, owner_session_id=owner_id,
            status="queued", current_step="Queued for acquisition",
            created_at=now_dt, updated_at=now_dt, progress_percentage=0,
        ))

        coordinator = IndexingLifecycleCoordinator(
            repository_repo=repo_repo,
            job_repo=job_repo,
            indexing_service=indexing_service,
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            now=now_dt,
        )
        runner = AcquisitionRunner(repository_repo=repo_repo, job_repo=job_repo)

        with pytest.raises((AcquisitionError, IndexingError)) as exc_info:
            runner.run_acquisition(
                owner_session_id=owner_id,
                repository_id=repo_id,
                job_id=job_id,
                source_input=zip_bytes,
                consumer=coordinator.consume,
                parent_dir=tmp,
                now=now_dt,
                source_type="zip",
            )

        err_str = str(exc_info.value)
        assert err_str in ("Indexing failed safely.", "Acquisition failed safely.")
        assert "sk-proj-999" not in err_str
        assert "/etc/shadow" not in err_str

        final_job = job_repo.get_by_id(owner_id, job_id)
        assert final_job is not None
        assert final_job.status == "failed"
        assert final_job.current_step == "Indexing failed"
        assert final_job.error_message in ("Indexing failed safely.", "Acquisition failed safely.")
        assert final_job.progress_percentage == 65


def test_acquisition_initial_claim_failures() -> None:
    now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)
    owner_id = "sess_claim"
    repo_id = "repo_claim"
    job_id = "job_claim"

    repo_repo = FakeRepositoryRepository()
    job_repo = FakeIndexingJobRepository()

    repo_repo.save(RepositoryRecord(
        repository_id=repo_id, owner_session_id=owner_id, name="claim_repo",
        source_type="zip", status="pending", created_at=now_dt, updated_at=now_dt,
    ))
    job_repo.save(IndexingJobRecord(
        job_id=job_id, repository_id=repo_id, owner_session_id=owner_id,
        status="queued", current_step="Queued for acquisition",
        created_at=now_dt, updated_at=now_dt, progress_percentage=0,
    ))

    job_repo.transition_status = lambda *a, **k: None  # type: ignore

    runner = AcquisitionRunner(repository_repo=repo_repo, job_repo=job_repo)
    with pytest.raises((AcquisitionError, IndexingError)) as exc_info:
        runner.run_acquisition(
            owner_session_id=owner_id,
            repository_id=repo_id,
            job_id=job_id,
            source_input=Path("/tmp/foo.zip"),
            consumer=lambda s: None,
            now=now_dt,
            source_type="zip",
        )

    assert "safely" in str(exc_info.value)
    final_repo = repo_repo.get_by_id(owner_id, repo_id)
    assert final_repo is not None
    assert final_repo.status == "pending"


def test_deletion_races_during_failure_finalization() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_bytes = _create_zip_bytes({"main.py": "def foo(): pass\n"})

        now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)
        owner_id = "sess_del"
        repo_id = "repo_del"
        job_id = "job_del"

        repo_repo = FakeRepositoryRepository()
        job_repo = FakeIndexingJobRepository()

        repo_repo.save(RepositoryRecord(
            repository_id=repo_id, owner_session_id=owner_id, name="del_repo",
            source_type="zip", status="pending", created_at=now_dt, updated_at=now_dt,
        ))
        job_repo.save(IndexingJobRecord(
            job_id=job_id, repository_id=repo_id, owner_session_id=owner_id,
            status="queued", current_step="Queued for acquisition",
            created_at=now_dt, updated_at=now_dt, progress_percentage=0,
        ))

        def failing_consumer(source: AcquiredSource) -> None:
            repo_repo.delete(owner_id, repo_id)
            job_repo.delete_by_repository(owner_id, repo_id)
            raise RuntimeError("Consumer failed after deletion")

        runner = AcquisitionRunner(repository_repo=repo_repo, job_repo=job_repo)
        with pytest.raises((AcquisitionError, IndexingError)) as exc_info:
            runner.run_acquisition(
                owner_session_id=owner_id,
                repository_id=repo_id,
                job_id=job_id,
                source_input=zip_bytes,
                consumer=failing_consumer,
                parent_dir=tmp,
                now=now_dt,
                source_type="zip",
            )

        assert "safely" in str(exc_info.value)
        assert repo_repo.get_by_id(owner_id, repo_id) is None
        assert job_repo.get_by_id(owner_id, job_id) is None


def test_standalone_coordinator_failures_do_not_mutate_failed_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)
        owner_id = "sess_stand"
        repo_id = "repo_stand"
        job_id = "job_stand"

        stages = ("parsing", "embedding", "storage", "repo_completion", "job_completion")
        for fail_stage in stages:
            failing_provider = FakeEmbeddingProvider()
            if fail_stage == "embedding":
                failing_provider.should_raise = RuntimeError("Secret provider key sk-999")

            chunk_repo = FakeCodeChunkRepository()
            if fail_stage == "storage":
                def raise_storage(*a: Any, **k: Any) -> Any:
                    raise RuntimeError("Storage crash secret_db_uri")
                chunk_repo.save_many = raise_storage  # type: ignore

            indexing_service = RepositoryIndexingService(
                provider=failing_provider, code_chunk_repo=chunk_repo
            )

            repo_repo = FakeRepositoryRepository()
            job_repo = FakeIndexingJobRepository()

            repo_repo.save(RepositoryRecord(
                repository_id=repo_id, owner_session_id=owner_id, name="stand_repo",
                source_type="zip", status="indexing", created_at=now_dt, updated_at=now_dt,
            ))
            job_repo.save(IndexingJobRecord(
                job_id=job_id, repository_id=repo_id, owner_session_id=owner_id,
                status="scanning", current_step="Scanning source files",
                created_at=now_dt, updated_at=now_dt, progress_percentage=30,
            ))

            orig_job_trans = job_repo.transition_status
            orig_repo_trans = repo_repo.transition_status

            def make_bad_repo(orig_fn: Any) -> Any:
                def bad_repo_trans(*args: Any, **kwargs: Any) -> Any:
                    st = kwargs.get("new_status")
                    if st is None and len(args) >= 4:
                        st = args[3]
                    if st == "ready":
                        return None
                    return orig_fn(*args, **kwargs)
                return bad_repo_trans

            def make_bad_job(orig_fn: Any) -> Any:
                def bad_job_trans(*args: Any, **kwargs: Any) -> Any:
                    st = kwargs.get("new_status")
                    if st is None and len(args) >= 5:
                        st = args[4]
                    if st == "ready":
                        return None
                    return orig_fn(*args, **kwargs)
                return bad_job_trans

            if fail_stage == "parsing":
                job_repo.transition_status = lambda *a, **k: None  # type: ignore
            elif fail_stage == "repo_completion":
                repo_repo.transition_status = make_bad_repo(orig_repo_trans)  # type: ignore
            elif fail_stage == "job_completion":
                job_repo.transition_status = make_bad_job(orig_job_trans)  # type: ignore

            coordinator = IndexingLifecycleCoordinator(
                repository_repo=repo_repo,
                job_repo=job_repo,
                indexing_service=indexing_service,
                owner_session_id=owner_id,
                repository_id=repo_id,
                job_id=job_id,
                now=now_dt,
            )

            tmp_path = Path(tmp)
            (tmp_path / "main.py").write_text("def foo(): pass\n", encoding="utf-8")

            manifest = ExtractionManifest(
                file_count=1, total_extracted_bytes=10, relative_paths=("main.py",)
            )
            acquired_source = AcquiredSource(
                extraction_root=tmp_path, manifest=manifest, source_type="zip"
            )

            with pytest.raises(IndexingError) as exc_info:
                coordinator.consume(acquired_source)

            err_str = str(exc_info.value)
            assert err_str == "Indexing failed safely."
            assert "sk-999" not in err_str
            assert "secret_db_uri" not in err_str

            # Coordinator did NOT issue any transition with new_status="failed"!
            job_failed_calls = [
                c for c in job_repo.transition_calls if c.get("new_status") == "failed"
            ]
            repo_failed_calls = [
                c for c in repo_repo.transition_calls if c.get("new_status") == "failed"
            ]
            assert len(job_failed_calls) == 0
            assert len(repo_failed_calls) == 0


def test_process_control_exceptions_passthrough() -> None:
    now_dt = datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)
    provider = FakeEmbeddingProvider()
    chunk_repo = FakeCodeChunkRepository()
    indexing_service = RepositoryIndexingService(
        provider=provider, code_chunk_repo=chunk_repo
    )

    repo_repo = FakeRepositoryRepository()
    job_repo = FakeIndexingJobRepository()

    coordinator = IndexingLifecycleCoordinator(
        repository_repo=repo_repo,
        job_repo=job_repo,
        indexing_service=indexing_service,
        owner_session_id="sess_proc",
        repository_id="repo_proc",
        job_id="job_proc",
        now=now_dt,
    )

    manifest = ExtractionManifest(
        file_count=1, total_extracted_bytes=10, relative_paths=("main.py",)
    )
    acquired_source = AcquiredSource(
        extraction_root=Path("/tmp"), manifest=manifest, source_type="zip"
    )

    def throw_kb(*a: Any, **k: Any) -> Any:
        raise KeyboardInterrupt()

    indexing_service.index_acquired_source = throw_kb  # type: ignore
    with pytest.raises(KeyboardInterrupt):
        coordinator.consume(acquired_source)

    def throw_se(*a: Any, **k: Any) -> Any:
        raise SystemExit(1)

    indexing_service.index_acquired_source = throw_se  # type: ignore
    with pytest.raises(SystemExit):
        coordinator.consume(acquired_source)
