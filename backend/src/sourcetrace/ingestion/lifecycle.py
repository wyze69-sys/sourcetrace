"""Synchronous indexing lifecycle coordinator connecting acquisition, indexing service,
and state transitions.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sourcetrace.core.exceptions import IndexingError
from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.indexing import (
    IndexingLifecycleObserver,
    IndexingResult,
    RepositoryIndexingService,
)
from sourcetrace.storage.repositories import (
    IndexingJobRepository,
    RepositoryRepository,
)


class _CoordinatorObserver(IndexingLifecycleObserver):
    """Internal lifecycle observer driving job and repository state transitions."""

    def __init__(
        self,
        job_repo: IndexingJobRepository,
        repository_repo: RepositoryRepository,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._job_repo = job_repo
        self._repository_repo = repository_repo
        self._owner_session_id = owner_session_id
        self._repository_id = repository_id
        self._job_id = job_id
        self._clock = clock

    def parsing_started(self) -> None:
        now_dt = self._clock()
        job = self._job_repo.transition_status(
            owner_session_id=self._owner_session_id,
            job_id=self._job_id,
            repository_id=self._repository_id,
            expected_status="scanning",
            new_status="parsing",
            current_step="Parsing Python source",
            progress_percentage=45,
            updated_at=now_dt,
        )
        if job is None:
            raise IndexingError("Indexing failed safely.")

    def embedding_started(self) -> None:
        now_dt = self._clock()
        job = self._job_repo.transition_status(
            owner_session_id=self._owner_session_id,
            job_id=self._job_id,
            repository_id=self._repository_id,
            expected_status="parsing",
            new_status="embedding",
            current_step="Generating chunk embeddings",
            progress_percentage=65,
            updated_at=now_dt,
        )
        if job is None:
            raise IndexingError("Indexing failed safely.")

    def storing_started(self) -> None:
        now_dt = self._clock()
        job = self._job_repo.transition_status(
            owner_session_id=self._owner_session_id,
            job_id=self._job_id,
            repository_id=self._repository_id,
            expected_status=("embedding", "parsing"),
            new_status="storing",
            current_step="Storing repository index",
            progress_percentage=85,
            updated_at=now_dt,
        )
        if job is None:
            raise IndexingError("Indexing failed safely.")

    def completed(self, result: IndexingResult) -> None:
        now_dt = self._clock()

        # 1. Transition repository: indexing -> ready
        repo = self._repository_repo.transition_status(
            owner_session_id=self._owner_session_id,
            repository_id=self._repository_id,
            expected_status="indexing",
            new_status="ready",
            updated_at=now_dt,
            file_count=result.parsed_file_count,
            chunk_count=result.chunk_count,
        )
        if repo is None:
            raise IndexingError("Indexing failed safely.")

        # 2. Transition job: storing -> ready
        job = self._job_repo.transition_status(
            owner_session_id=self._owner_session_id,
            job_id=self._job_id,
            repository_id=self._repository_id,
            expected_status="storing",
            new_status="ready",
            current_step="Repository ready",
            progress_percentage=100,
            updated_at=now_dt,
            completed_at=now_dt,
            error_message=None,
        )
        if job is None:
            raise IndexingError("Indexing failed safely.")


class IndexingLifecycleCoordinator:
    """Synchronous coordinator updating repository and job state transitions during indexing."""

    def __init__(
        self,
        repository_repo: RepositoryRepository,
        job_repo: IndexingJobRepository,
        indexing_service: RepositoryIndexingService,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        index_mode: str | None = None,
        now: datetime | Callable[[], datetime] | None = None,
    ) -> None:
        if (
            repository_repo is None
            or job_repo is None
            or indexing_service is None
            or type(owner_session_id) is not str
            or not owner_session_id.strip()
            or type(repository_id) is not str
            or not repository_id.strip()
            or type(job_id) is not str
            or not job_id.strip()
        ):
            raise IndexingError("Indexing failed safely.")

        self._repository_repo = repository_repo
        self._job_repo = job_repo
        self._indexing_service = indexing_service
        self._owner_session_id = owner_session_id
        self._repository_id = repository_id
        self._job_id = job_id
        self._index_mode = (
            index_mode
            if index_mode in ("static", "cloud_ai")
            else ("cloud_ai" if indexing_service._provider is not None else "static")
        )
        self._now_arg = now

    def _get_now(self) -> datetime:
        if callable(self._now_arg):
            n = self._now_arg()
        elif self._now_arg is not None:
            n = self._now_arg
        else:
            n = datetime.now(UTC)

        if type(n) is not datetime or isinstance(n, bool):
            raise IndexingError("Indexing failed safely.")
        if n.tzinfo is None:
            return n.replace(tzinfo=UTC)
        return n.astimezone(UTC)

    def consume(self, acquired_source: AcquiredSource) -> None:
        """Process acquired source and transition job and repository states synchronously."""
        if type(acquired_source) is not AcquiredSource:
            raise IndexingError("Indexing failed safely.")

        now_dt = self._get_now()
        observer = _CoordinatorObserver(
            job_repo=self._job_repo,
            repository_repo=self._repository_repo,
            owner_session_id=self._owner_session_id,
            repository_id=self._repository_id,
            job_id=self._job_id,
            clock=self._get_now,
        )

        try:
            self._indexing_service.index_acquired_source(
                acquired_source=acquired_source,
                owner_session_id=self._owner_session_id,
                repository_id=self._repository_id,
                index_mode=self._index_mode,
                now=now_dt,
                observer=observer,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise IndexingError("Indexing failed safely.") from None
