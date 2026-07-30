"""Background worker for generation-safe GitHub repository refresh."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sourcetrace.embeddings.provider import EmbeddingProvider
from sourcetrace.ingestion.acquisition import AcquiredSource, acquire_github_source
from sourcetrace.ingestion.indexing import (
    IndexingLifecycleObserver,
    IndexingResult,
    RepositoryIndexingService,
)
from sourcetrace.parsers.flow_evidence import is_flow_evidence_complete
from sourcetrace.storage.mongo_repositories import (
    MongoAnonymousSessionRepository,
    MongoCodeChunkRepository,
    MongoIndexingJobRepository,
    MongoRepositoryRepository,
)
from sourcetrace.storage.repositories import (
    AnonymousSessionRepository,
    CodeChunkRepository,
    IndexingJobRepository,
    RepositoryRepository,
)
from sourcetrace.workers.provider_selection import get_default_embedding_provider


class _RefreshJobObserver(IndexingLifecycleObserver):
    """Internal lifecycle observer tracking progress for refresh jobs."""

    def __init__(
        self,
        job_repo: IndexingJobRepository,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._job_repo = job_repo
        self._owner_session_id = owner_session_id
        self._repository_id = repository_id
        self._job_id = job_id
        self._clock = clock

    def parsing_started(self) -> None:
        now_dt = self._clock()
        self._job_repo.transition_status(
            owner_session_id=self._owner_session_id,
            job_id=self._job_id,
            repository_id=self._repository_id,
            expected_status=("queued", "acquiring", "scanning"),
            new_status="parsing",
            current_step="Parsing updated repository source",
            progress_percentage=45,
            updated_at=now_dt,
        )

    def embedding_started(self) -> None:
        now_dt = self._clock()
        self._job_repo.transition_status(
            owner_session_id=self._owner_session_id,
            job_id=self._job_id,
            repository_id=self._repository_id,
            expected_status="parsing",
            new_status="embedding",
            current_step="Generating updated chunk embeddings",
            progress_percentage=65,
            updated_at=now_dt,
        )

    def storing_started(self) -> None:
        now_dt = self._clock()
        self._job_repo.transition_status(
            owner_session_id=self._owner_session_id,
            job_id=self._job_id,
            repository_id=self._repository_id,
            expected_status=("embedding", "parsing"),
            new_status="storing",
            current_step="Storing updated repository generation",
            progress_percentage=85,
            updated_at=now_dt,
        )

    def completed(self, result: IndexingResult) -> None:
        pass


def run_github_refresh(
    owner_session_id: str,
    repository_id: str,
    job_id: str,
    repository_repo: RepositoryRepository | None = None,
    job_repo: IndexingJobRepository | None = None,
    code_chunk_repo: CodeChunkRepository | None = None,
    session_repo: AnonymousSessionRepository | None = None,
    provider: EmbeddingProvider | None = None,
    provider_factory: Callable[[], EmbeddingProvider] | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> None:
    """Execute background acquisition, re-indexing into a new generation,
    then perform atomic pointer switch and GC.
    """

    def _get_now() -> datetime:
        if callable(now):
            n = now()
        elif now is not None:
            n = now
        else:
            n = datetime.now(UTC)
        if type(n) is not datetime or isinstance(n, bool):
            n = datetime.now(UTC)
        if n.tzinfo is None:
            return n.replace(tzinfo=UTC)
        return n.astimezone(UTC)

    now_start = _get_now()

    # Repositories initialization
    r_repo = repository_repo if repository_repo is not None else MongoRepositoryRepository()
    j_repo = job_repo if job_repo is not None else MongoIndexingJobRepository()
    c_repo = code_chunk_repo if code_chunk_repo is not None else MongoCodeChunkRepository()
    s_repo = session_repo if session_repo is not None else MongoAnonymousSessionRepository()

    # 1. Fetch repository record and validate state
    try:
        repo_rec = r_repo.get_by_id(owner_session_id, repository_id)
        if (
            repo_rec is None
            or repo_rec.source_type != "github"
            or repo_rec.status != "ready"
            or not repo_rec.github_url
        ):
            j_repo.transition_status(
                owner_session_id=owner_session_id,
                job_id=job_id,
                repository_id=repository_id,
                expected_status="queued",
                new_status="failed",
                current_step="Refresh failed",
                progress_percentage=None,
                error_message="Repository is not a ready GitHub repository.",
                completed_at=now_start,
                updated_at=now_start,
            )
            return
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        try:
            j_repo.transition_status(
                owner_session_id=owner_session_id,
                job_id=job_id,
                repository_id=repository_id,
                expected_status="queued",
                new_status="failed",
                current_step="Refresh failed",
                progress_percentage=None,
                error_message=str(exc),
                completed_at=now_start,
                updated_at=now_start,
            )
        except Exception:  # noqa: BLE001
            pass
        return

    old_generation_id = repo_rec.active_generation_id
    new_generation_id = job_id

    # 2. Acquire and index into new generation
    try:
        # Transition job: queued -> acquiring
        now_acq = _get_now()
        j_repo.transition_status(
            owner_session_id=owner_session_id,
            job_id=job_id,
            repository_id=repository_id,
            expected_status="queued",
            new_status="acquiring",
            current_step="Acquiring GitHub repository archive",
            progress_percentage=15,
            updated_at=now_acq,
        )

        index_mode = getattr(repo_rec, "index_mode", "cloud_ai")
        if index_mode == "static":
            emb_provider = None
        else:
            if provider is not None:
                emb_provider = provider
            elif provider_factory is not None:
                emb_provider = provider_factory()
            else:
                emb_provider = get_default_embedding_provider()

        effective_mode = index_mode

        observer = _RefreshJobObserver(
            job_repo=j_repo,
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            clock=_get_now,
        )
        indexing_service = RepositoryIndexingService(provider=emb_provider, code_chunk_repo=c_repo)

        idx_result: IndexingResult | None = None
        resolved_branch: str | None = None
        resolved_commit_sha: str | None = None

        def _process_acquired_source(acq_source: AcquiredSource) -> None:
            nonlocal idx_result, resolved_branch, resolved_commit_sha
            resolved_branch = acq_source.resolved_branch
            resolved_commit_sha = acq_source.resolved_commit_sha
            now_idx = _get_now()
            idx_result = indexing_service.index_acquired_source(
                acquired_source=acq_source,
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                index_mode=effective_mode,
                now=now_idx,
                observer=observer,
                generation_id=new_generation_id,
            )

        acquire_github_source(
            url=repo_rec.github_url,
            consumer=_process_acquired_source,
        )

        if not isinstance(idx_result, IndexingResult):
            raise RuntimeError("Indexing service produced invalid result.")

        now_switch = _get_now()

        # 3. Atomic active_generation_id pointer switch + freshness metadata update
        updated_repo = r_repo.update_active_generation(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            active_generation_id=new_generation_id,
            updated_at=now_switch,
            indexed_branch=resolved_branch,
            indexed_commit_sha=resolved_commit_sha,
            file_count=idx_result.parsed_file_count,
            parser_versions=idx_result.parser_versions,
            flow_evidence_complete=is_flow_evidence_complete(idx_result.parser_versions),
            consecutive_refresh_failures=0,
            is_stale=False,
        )

        if updated_repo is None:
            raise RuntimeError("Failed to update active generation pointer.")

        # 4. Garbage collection of old generation ONLY AFTER pointer switch
        c_repo.delete_by_generation(owner_session_id, repository_id, old_generation_id)

        # 5. Transition job to ready
        j_repo.transition_status(
            owner_session_id=owner_session_id,
            job_id=job_id,
            repository_id=repository_id,
            expected_status="storing",
            new_status="ready",
            current_step="Repository refresh complete",
            progress_percentage=100,
            updated_at=now_switch,
            completed_at=now_switch,
            error_message=None,
        )

        # Extend session TTL on successful activity
        try:
            s_repo.reserve_repository_slot(owner_session_id=owner_session_id, now=now_switch)
        except Exception:  # noqa: BLE001
            pass

    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        now_fail = _get_now()
        # Rollback: Clean up orphaned new generation chunks
        try:
            c_repo.delete_by_generation(owner_session_id, repository_id, new_generation_id)
        except Exception:  # noqa: BLE001
            pass

        # Increment consecutive_refresh_failures on repository record (keeping status ready)
        try:
            curr = r_repo.get_by_id(owner_session_id, repository_id)
            current_failures = getattr(curr, "consecutive_refresh_failures", 0) or 0
            r_repo.transition_status(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                expected_status="ready",
                new_status="ready",
                updated_at=now_fail,
                consecutive_refresh_failures=current_failures + 1,
            )
        except Exception:  # noqa: BLE001
            pass

        # Mark job failed
        try:
            j_repo.transition_status(
                owner_session_id=owner_session_id,
                job_id=job_id,
                repository_id=repository_id,
                expected_status=(
                    "queued",
                    "acquiring",
                    "scanning",
                    "parsing",
                    "embedding",
                    "storing",
                ),
                new_status="failed",
                current_step="Repository refresh failed",
                progress_percentage=None,
                error_message=str(exc) or "Repository refresh failed safely.",
                updated_at=now_fail,
                completed_at=now_fail,
            )
        except Exception:  # noqa: BLE001
            pass
