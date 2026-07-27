"""Ingestion orchestration service for repository and job creation."""

import secrets
from datetime import UTC, datetime
from typing import NamedTuple

from sourcetrace.core.exceptions import (
    IngestionServiceError,
    InvalidRepositoryURLError,
    RepositoryQuotaExceededError,
    RepositoryValidationError,
    SessionInvalidError,
)
from sourcetrace.ingestion.validation import validate_github_url
from sourcetrace.models.domain import IndexingJobRecord, RepositoryRecord
from sourcetrace.storage.repositories import (
    AnonymousSessionRepository,
    IndexingJobRepository,
    RepositoryRepository,
)


def generate_repository_id() -> str:
    """Generate a random opaque repository identifier."""
    return f"repo_{secrets.token_urlsafe(16)}"


def generate_job_id() -> str:
    """Generate a random opaque indexing job identifier."""
    return f"job_{secrets.token_urlsafe(16)}"


class RepositoryCreationResult(NamedTuple):
    """Result pair containing created RepositoryRecord and IndexingJobRecord."""

    repository: RepositoryRecord
    indexing_job: IndexingJobRecord


def _rollback_and_release(
    session_repo: AnonymousSessionRepository,
    repository_repo: RepositoryRepository,
    job_repo: IndexingJobRepository,
    owner_session_id: str,
    repository_id: str,
) -> None:
    """Attempt conservative child-to-parent cleanup after persistence failure."""
    # 1. Attempt indexing job cleanup
    try:
        job_repo.delete_by_repository(owner_session_id, repository_id)
    except Exception:  # noqa: BLE001
        pass

    # 2. Attempt repository cleanup
    try:
        repository_repo.delete(owner_session_id, repository_id)
    except Exception:  # noqa: BLE001
        pass

    # 3. Verify repository cleanup before releasing reserved slot
    repo_cleaned = False
    try:
        remaining_repo = repository_repo.get_by_id(owner_session_id, repository_id)
        if remaining_repo is None:
            repo_cleaned = True
    except Exception:  # noqa: BLE001
        repo_cleaned = False

    # 4. Release reserved slot ONLY when cleanup confirms no owned repository remains
    if repo_cleaned:
        try:
            session_repo.release_repository_slot(owner_session_id)
        except Exception:  # noqa: BLE001
            pass


class IngestionService:
    """Injectable service orchestrating repository slot reservation and record persistence."""

    def __init__(
        self,
        session_repo: AnonymousSessionRepository,
        repository_repo: RepositoryRepository,
        job_repo: IndexingJobRepository,
    ) -> None:
        self._session_repo = session_repo
        self._repository_repo = repository_repo
        self._job_repo = job_repo

    def create_pending_repository(
        self,
        owner_session_id: str,
        source_type: str,
        name: str | None = None,
        github_url: str | None = None,
        index_mode: str = "static",
        now: datetime | None = None,
    ) -> RepositoryCreationResult:
        """Atomically reserve quota slot, refresh activity, and persist repository/job pair."""
        # 1. Pre-reservation validation of inputs
        if not isinstance(source_type, str):
            raise RepositoryValidationError("Unsupported or invalid source type.")

        if not isinstance(index_mode, str) or index_mode not in ("static", "cloud_ai"):
            index_mode = "static"

        display_name: str
        canonical_github_url: str | None

        if source_type == "github":
            if github_url is None or not isinstance(github_url, str):
                raise RepositoryValidationError(
                    "GitHub repository URL is required for github source type."
                )
            try:
                owner, repo = validate_github_url(github_url)
            except InvalidRepositoryURLError as exc:
                raise RepositoryValidationError(str(exc)) from exc
            canonical_github_url = f"https://github.com/{owner}/{repo}"

            if name is None or (isinstance(name, str) and not name.strip()):
                display_name = repo
            elif isinstance(name, str):
                cleaned_name = name.strip()
                if len(cleaned_name) > 256 or "\x00" in cleaned_name:
                    raise RepositoryValidationError(
                        "Repository display name must be a non-empty safe string."
                    )
                display_name = cleaned_name
            else:
                raise RepositoryValidationError(
                    "Repository display name must be a non-empty safe string."
                )
        elif source_type == "zip":
            if github_url is not None:
                raise RepositoryValidationError("GitHub URL must be None for zip source type.")
            if not isinstance(name, str) or not name.strip() or len(name) > 256 or "\x00" in name:
                raise RepositoryValidationError(
                    "Repository display name must be a non-empty safe string."
                )
            display_name = name.strip()
            canonical_github_url = None
        else:
            raise RepositoryValidationError("Unsupported or invalid source type.")

        if now is None:
            now = datetime.now(UTC)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)

        # 2. Reserve quota slot atomically
        reserved_session = self._session_repo.reserve_repository_slot(
            owner_session_id=owner_session_id,
            now=now,
            max_quota=3,
            retention_days=7,
        )

        if reserved_session is None:
            existing_session = self._session_repo.get_by_id(owner_session_id)
            if existing_session is None or existing_session.expires_at <= now:
                raise SessionInvalidError("Session is missing or expired.")
            raise RepositoryQuotaExceededError(
                "Maximum active repository quota (3) exceeded for this session."
            )

        repository_id = generate_repository_id()
        job_id = generate_job_id()

        current_step = (
            "Queued for acquisition" if source_type == "github" else "Queued for archive processing"
        )

        repository = RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name=display_name,
            source_type=source_type,
            status="pending",
            created_at=now,
            updated_at=now,
            github_url=canonical_github_url,
            file_count=0,
            chunk_count=0,
            index_mode=index_mode,
        )

        indexing_job = IndexingJobRecord(
            job_id=job_id,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            status="queued",
            current_step=current_step,
            created_at=now,
            updated_at=now,
            progress_percentage=0,
            error_message=None,
            completed_at=None,
        )

        saved_repo: RepositoryRecord | None = None
        saved_job: IndexingJobRecord | None = None

        # 3. Persist repository and job records with conservative child-to-parent rollback
        try:
            saved_repo = self._repository_repo.save(repository)
            saved_job = self._job_repo.save(indexing_job)
        except Exception:
            _rollback_and_release(
                self._session_repo,
                self._repository_repo,
                self._job_repo,
                owner_session_id,
                repository_id,
            )
            raise IngestionServiceError(
                "Failed to create repository and indexing job record."
            ) from None

        return RepositoryCreationResult(
            repository=saved_repo,
            indexing_job=saved_job,
        )
