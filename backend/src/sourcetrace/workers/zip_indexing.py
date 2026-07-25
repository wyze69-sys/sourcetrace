"""Background worker orchestrating ZIP repository acquisition and indexing."""

from collections.abc import Callable
from datetime import UTC, datetime

from sourcetrace.core.exceptions import UploadStagingError
from sourcetrace.embeddings.provider import EmbeddingProvider
from sourcetrace.ingestion.acquisition import AcquisitionRunner
from sourcetrace.ingestion.indexing import RepositoryIndexingService
from sourcetrace.ingestion.lifecycle import IndexingLifecycleCoordinator
from sourcetrace.ingestion.upload_staging import (
    FileSystemUploadStagingStore,
    UploadStagingStore,
)
from sourcetrace.models.domain import IndexingJobRecord
from sourcetrace.storage.mongo_repositories import (
    MongoCodeChunkRepository,
    MongoIndexingJobRepository,
    MongoRepositoryRepository,
)
from sourcetrace.storage.repositories import (
    CodeChunkRepository,
    IndexingJobRepository,
    RepositoryRepository,
)
from sourcetrace.workers.provider_selection import get_default_embedding_provider


def _safe_delete_staging(store: UploadStagingStore, token: str) -> None:
    """Idempotently delete staged upload, suppressing all cleanup errors."""
    try:
        store.delete(token)
    except Exception:  # noqa: BLE001
        pass


def run_zip_indexing(
    owner_session_id: str,
    repository_id: str,
    job_id: str,
    staging_token: str,
    repository_repo: RepositoryRepository | None = None,
    job_repo: IndexingJobRepository | None = None,
    code_chunk_repo: CodeChunkRepository | None = None,
    provider: EmbeddingProvider | None = None,
    provider_factory: Callable[[], EmbeddingProvider] | None = None,
    staging_store: UploadStagingStore | None = None,
) -> None:
    """Execute background acquisition and indexing for a staged ZIP repository.

    Resolves opaque staging token, performs setup safety, delegates to AcquisitionRunner,
    and guarantees staged file deletion on every terminal path.
    """
    store = (
        staging_store
        if staging_store is not None
        else FileSystemUploadStagingStore()
    )

    try:
        # 1. Composition setup block with safe pre-claim failure handling and token resolution
        try:
            staged_path = store.resolve(staging_token)
            if not staged_path.exists() or staged_path.is_dir():
                raise UploadStagingError("Staged ZIP archive file is unavailable.")

            r_repo = (
                repository_repo
                if repository_repo is not None
                else MongoRepositoryRepository()
            )
            j_repo = (
                job_repo if job_repo is not None else MongoIndexingJobRepository()
            )
            c_repo = (
                code_chunk_repo
                if code_chunk_repo is not None
                else MongoCodeChunkRepository()
            )

            repo_rec = r_repo.get_by_id(owner_session_id, repository_id)
            if repo_rec is None:
                raise RuntimeError(f"Repository record {repository_id} not found.")

            index_mode = getattr(repo_rec, "index_mode", "cloud_ai")
            if index_mode not in ("static", "cloud_ai"):
                raise RuntimeError(f"Invalid index_mode {index_mode!r} on repository record.")

            if index_mode == "static":
                emb_provider = None
            else:
                if provider is not None:
                    emb_provider = provider
                elif provider_factory is not None:
                    emb_provider = provider_factory()
                else:
                    emb_provider = get_default_embedding_provider()

            effective_index_mode = index_mode

            runner = AcquisitionRunner(repository_repo=r_repo, job_repo=j_repo)
            indexing_service = RepositoryIndexingService(
                provider=emb_provider, code_chunk_repo=c_repo
            )
            coordinator = IndexingLifecycleCoordinator(
                repository_repo=r_repo,
                job_repo=j_repo,
                indexing_service=indexing_service,
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                job_id=job_id,
                index_mode=effective_index_mode,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            now_fail = datetime.now(UTC)
            failed_job: IndexingJobRecord | None = None
            try:
                target_j_repo = (
                    job_repo if job_repo is not None else MongoIndexingJobRepository()
                )
                failed_job = target_j_repo.transition_status(
                    owner_session_id=owner_session_id,
                    job_id=job_id,
                    repository_id=repository_id,
                    expected_status="queued",
                    new_status="failed",
                    current_step="Indexing setup failed",
                    progress_percentage=None,
                    error_message="Indexing could not start safely.",
                    completed_at=now_fail,
                    updated_at=now_fail,
                )
            except Exception:  # noqa: BLE001
                failed_job = None

            if (
                isinstance(failed_job, IndexingJobRecord)
                and type(failed_job) is IndexingJobRecord
                and failed_job.owner_session_id == owner_session_id
                and failed_job.repository_id == repository_id
                and failed_job.job_id == job_id
                and failed_job.status == "failed"
            ):
                try:
                    target_r_repo = (
                        repository_repo
                        if repository_repo is not None
                        else MongoRepositoryRepository()
                    )
                    target_r_repo.transition_status(
                        owner_session_id=owner_session_id,
                        repository_id=repository_id,
                        expected_status="pending",
                        new_status="failed",
                        updated_at=now_fail,
                    )
                except Exception:  # noqa: BLE001
                    pass
            return

        # 2. Execution block delegating to runner
        try:
            runner.run_acquisition(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                job_id=job_id,
                source_type="zip",
                source_input=staged_path,
                consumer=coordinator.consume,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # Ordinary worker exception is contained at background boundary
            return
    finally:
        _safe_delete_staging(store, staging_token)
