"""Offline unit tests for IngestionService repository slot reservation and job creation."""

from datetime import UTC, datetime, timedelta

import pytest

from sourcetrace.core.exceptions import (
    IngestionServiceError,
    RepositoryQuotaExceededError,
    RepositoryValidationError,
    SessionInvalidError,
    SourceTraceError,
)
from sourcetrace.ingestion.service import IngestionService
from sourcetrace.models.domain import (
    AnonymousSession,
    IndexingJobRecord,
    RepositoryRecord,
)


class FakeSessionRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, AnonymousSession] = {}

    def get_by_id(self, owner_session_id: str) -> AnonymousSession | None:
        return self.sessions.get(owner_session_id)

    def save(self, session: AnonymousSession) -> AnonymousSession:
        self.sessions[session.owner_session_id] = session
        return session

    def delete(self, owner_session_id: str) -> bool:
        return self.sessions.pop(owner_session_id, None) is not None

    def reserve_repository_slot(
        self,
        owner_session_id: str,
        now: datetime,
        max_quota: int = 3,
        retention_days: int = 7,
    ) -> AnonymousSession | None:
        session = self.sessions.get(owner_session_id)
        if session is None or session.expires_at <= now:
            return None
        if session.active_repository_count >= max_quota:
            return None

        new_session = AnonymousSession(
            owner_session_id=session.owner_session_id,
            last_active_at=now,
            expires_at=now + timedelta(days=retention_days),
            created_at=session.created_at,
            updated_at=now,
            active_repository_count=session.active_repository_count + 1,
        )
        self.sessions[owner_session_id] = new_session
        return new_session

    def release_repository_slot(self, owner_session_id: str) -> bool:
        session = self.sessions.get(owner_session_id)
        if session is None or session.active_repository_count <= 0:
            return False
        new_session = AnonymousSession(
            owner_session_id=session.owner_session_id,
            last_active_at=session.last_active_at,
            expires_at=session.expires_at,
            created_at=session.created_at,
            updated_at=session.updated_at,
            active_repository_count=session.active_repository_count - 1,
        )
        self.sessions[owner_session_id] = new_session
        return True


class FakeRepositoryRepository:
    def __init__(self, fail_on_save: bool = False) -> None:
        self.repositories: dict[tuple[str, str], RepositoryRecord] = {}
        self.fail_on_save = fail_on_save
        self.deleted_keys: list[tuple[str, str]] = []

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.repositories.get((owner_session_id, repository_id))

    def list_by_owner(self, owner_session_id: str) -> list[RepositoryRecord]:
        return [r for (owner_id, _), r in self.repositories.items() if owner_id == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def save(self, repository: RepositoryRecord) -> RepositoryRecord:
        if self.fail_on_save:
            raise RuntimeError("Database write failure (Simulated Mongo Error)")
        key = (repository.owner_session_id, repository.repository_id)
        self.repositories[key] = repository
        return repository

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        key = (owner_session_id, repository_id)
        self.deleted_keys.append(key)
        return self.repositories.pop(key, None) is not None


class AmbiguousCleanupRepository(FakeRepositoryRepository):
    """Fakes a repository persistence failure where delete or get raises a storage exception."""

    def __init__(self, fail_on_save: bool = True, fail_on_cleanup: bool = True) -> None:
        super().__init__(fail_on_save=fail_on_save)
        self.fail_on_cleanup = fail_on_cleanup

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        if self.fail_on_cleanup:
            raise RuntimeError("Cleanup transport timeout (Simulated Mongo Error)")
        return super().delete(owner_session_id, repository_id)

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        if self.fail_on_cleanup:
            raise RuntimeError("Verification read timeout (Simulated Mongo Error)")
        return super().get_by_id(owner_session_id, repository_id)


class FakeIndexingJobRepository:
    def __init__(self, fail_on_save: bool = False) -> None:
        self.jobs: dict[tuple[str, str], IndexingJobRecord] = {}
        self.fail_on_save = fail_on_save

    def get_by_id(self, owner_session_id: str, job_id: str) -> IndexingJobRecord | None:
        return self.jobs.get((owner_session_id, job_id))

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        for (owner_id, _), j in self.jobs.items():
            if owner_id == owner_session_id and j.repository_id == repository_id:
                return j
        return None

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        if self.fail_on_save:
            raise RuntimeError("Job storage write failure (Simulated BSON error)")
        key = (job.owner_session_id, job.job_id)
        self.jobs[key] = job
        return job

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        keys_to_del = [
            k
            for k, j in self.jobs.items()
            if k[0] == owner_session_id and j.repository_id == repository_id
        ]
        for k in keys_to_del:
            del self.jobs[k]
        return len(keys_to_del)


@pytest.fixture
def session_repo() -> FakeSessionRepository:
    return FakeSessionRepository()


@pytest.fixture
def repository_repo() -> FakeRepositoryRepository:
    return FakeRepositoryRepository()


@pytest.fixture
def job_repo() -> FakeIndexingJobRepository:
    return FakeIndexingJobRepository()


@pytest.fixture
def active_session(session_repo: FakeSessionRepository) -> AnonymousSession:
    now = datetime.now(UTC)
    session = AnonymousSession(
        owner_session_id="sess_valid_user_123",
        last_active_at=now,
        expires_at=now + timedelta(days=7),
        created_at=now,
        updated_at=now,
        active_repository_count=0,
    )
    return session_repo.save(session)


def test_first_second_third_submission_reserve_slots_and_fourth_rejected(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)
    owner_id = active_session.owner_session_id

    r1 = service.create_pending_repository(
        owner_id, "github", "repo-1", github_url="https://github.com/org/repo-1"
    )
    r2 = service.create_pending_repository(
        owner_id, "github", "repo-2", github_url="https://github.com/org/repo-2"
    )
    r3 = service.create_pending_repository(owner_id, "zip", "repo-3.zip")

    assert r1.repository.status == "pending"
    assert r2.repository.status == "pending"
    assert r3.repository.status == "pending"
    assert session_repo.get_by_id(owner_id).active_repository_count == 3

    with pytest.raises(RepositoryQuotaExceededError) as exc_info:
        service.create_pending_repository(
            owner_id, "github", "repo-4", github_url="https://github.com/org/repo-4"
        )

    assert "Maximum active repository quota" in str(exc_info.value)
    assert session_repo.get_by_id(owner_id).active_repository_count == 3


def test_missing_or_expired_session_cannot_reserve_slot(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)

    # Missing session
    with pytest.raises(SessionInvalidError):
        service.create_pending_repository(
            "sess_non_existent", "github", "repo", github_url="https://github.com/o/r"
        )

    # Expired session
    now = datetime.now(UTC)
    expired_session = AnonymousSession(
        owner_session_id="sess_expired",
        last_active_at=now - timedelta(days=8),
        expires_at=now - timedelta(days=1),
        created_at=now - timedelta(days=8),
        updated_at=now - timedelta(days=8),
        active_repository_count=0,
    )
    session_repo.save(expired_session)

    with pytest.raises(SessionInvalidError):
        service.create_pending_repository(
            "sess_expired",
            "github",
            "repo",
            github_url="https://github.com/o/r",
            now=now,
        )


def test_pre_reservation_validation_rejection_consumes_no_quota(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)
    owner_id = active_session.owner_session_id

    # 1. source_type == "github" with missing github_url
    with pytest.raises(RepositoryValidationError) as exc_info:
        service.create_pending_repository(owner_id, "github", name="test")
    assert "sess_" not in str(exc_info.value)

    # 2. source_type == "github" with invalid URL
    with pytest.raises(SourceTraceError) as exc_info:
        service.create_pending_repository(
            owner_id, "github", name="test", github_url="http://insecure.com"
        )
    assert "insecure.com" not in str(exc_info.value)
    assert "sess_" not in str(exc_info.value)

    # 3. source_type == "zip" with non-None github_url
    with pytest.raises(RepositoryValidationError) as exc_info:
        service.create_pending_repository(
            owner_id,
            "zip",
            name="test.zip",
            github_url="https://github.com/owner/repo",
        )
    assert "owner/repo" not in str(exc_info.value)

    # 4. source_type == "zip" with non-string or empty name
    with pytest.raises(RepositoryValidationError) as exc_info:
        service.create_pending_repository(owner_id, "zip", name="")

    # 5. Non-string source_type or unsupported source_type
    with pytest.raises(RepositoryValidationError) as exc_info:
        service.create_pending_repository(owner_id, "unknown_type", name="test")

    # 6. Non-string name (no coercion)
    with pytest.raises(RepositoryValidationError) as exc_info:
        service.create_pending_repository(
            owner_id,
            "zip",
            name=12345,  # type: ignore[arg-type]
        )

    # Confirm quota counter remains 0 and no records persisted
    assert session_repo.get_by_id(owner_id).active_repository_count == 0
    assert repository_repo.count_by_owner(owner_id) == 0


def test_canonical_github_url_stored_and_name_derived(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)
    owner_id = active_session.owner_session_id

    res = service.create_pending_repository(
        owner_id, "github", github_url="https://github.com/myorg/myrepo/"
    )
    assert res.repository.github_url == "https://github.com/myorg/myrepo"
    assert res.repository.name == "myrepo"


def test_qualifying_reservation_updates_last_active_at_and_expires_at_together(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)
    owner_id = active_session.owner_session_id

    submission_time = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    service.create_pending_repository(
        owner_id,
        "github",
        "repo-new",
        github_url="https://github.com/o/repo-new",
        now=submission_time,
    )

    updated_session = session_repo.get_by_id(owner_id)
    assert updated_session is not None
    assert updated_session.last_active_at == submission_time
    assert updated_session.expires_at == submission_time + timedelta(days=7)


def test_counter_starts_at_zero_for_new_sessions(
    session_repo: FakeSessionRepository,
) -> None:
    now = datetime.now(UTC)
    new_session = AnonymousSession(
        owner_session_id="sess_new_fresh",
        last_active_at=now,
        expires_at=now + timedelta(days=7),
        created_at=now,
        updated_at=now,
    )
    session_repo.save(new_session)
    stored = session_repo.get_by_id("sess_new_fresh")
    assert stored is not None
    assert stored.active_repository_count == 0


def test_rollback_releases_slot_after_repository_save_failure(
    session_repo: FakeSessionRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    failing_repo_repo = FakeRepositoryRepository(fail_on_save=True)
    service = IngestionService(session_repo, failing_repo_repo, job_repo)
    owner_id = active_session.owner_session_id

    with pytest.raises(IngestionServiceError) as exc_info:
        service.create_pending_repository(
            owner_id, "github", "fail-repo", github_url="https://github.com/o/fail"
        )

    assert session_repo.get_by_id(owner_id).active_repository_count == 0
    assert "Simulated Mongo Error" not in str(exc_info.value)


def test_rollback_deletes_owned_partial_repository_record_after_job_save_failure(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    active_session: AnonymousSession,
) -> None:
    failing_job_repo = FakeIndexingJobRepository(fail_on_save=True)
    service = IngestionService(session_repo, repository_repo, failing_job_repo)
    owner_id = active_session.owner_session_id

    with pytest.raises(IngestionServiceError) as exc_info:
        service.create_pending_repository(
            owner_id, "github", "fail-job-repo", github_url="https://github.com/o/fail"
        )

    # Slot released
    assert session_repo.get_by_id(owner_id).active_repository_count == 0
    # Repository deletion recorded and clean
    assert len(repository_repo.deleted_keys) == 1
    assert repository_repo.count_by_owner(owner_id) == 0
    assert "Simulated BSON error" not in str(exc_info.value)


def test_uncertain_cleanup_retains_slot_conservatively(
    session_repo: FakeSessionRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    ambiguous_repo = AmbiguousCleanupRepository(fail_on_save=True, fail_on_cleanup=True)
    service = IngestionService(session_repo, ambiguous_repo, job_repo)
    owner_id = active_session.owner_session_id

    with pytest.raises(IngestionServiceError) as exc_info:
        service.create_pending_repository(
            owner_id,
            "github",
            "repo-test",
            github_url="https://github.com/owner/repo",
        )

    # Slot remains reserved conservatively (active_repository_count == 1)
    # because cleanup was uncertain
    assert session_repo.get_by_id(owner_id).active_repository_count == 1
    # Fixed safe service error raised with no raw storage details or submitted values
    err_str = str(exc_info.value)
    assert "Simulated Mongo Error" not in err_str
    assert "Cleanup transport timeout" not in err_str
    assert "sess_" not in err_str
    assert "owner/repo" not in err_str


def test_generated_repo_and_job_identifiers_are_opaque_and_distinct(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)
    owner_id = active_session.owner_session_id

    result = service.create_pending_repository(
        owner_id, "github", "opaque-test", github_url="https://github.com/o/opaque"
    )

    assert result.repository.repository_id.startswith("repo_")
    assert result.indexing_job.job_id.startswith("job_")
    assert result.repository.repository_id != result.indexing_job.job_id
    assert len(result.repository.repository_id) > 10
    assert len(result.indexing_job.job_id) > 10


def test_repository_and_job_share_submitting_session_owner_id(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)
    owner_id = active_session.owner_session_id

    result = service.create_pending_repository(
        owner_id, "github", "shared-owner", github_url="https://github.com/o/shared"
    )

    assert result.repository.owner_session_id == owner_id
    assert result.indexing_job.owner_session_id == owner_id
    assert result.indexing_job.repository_id == result.repository.repository_id


def test_no_code_acquisition_zip_extraction_or_network_call_occurs(
    session_repo: FakeSessionRepository,
    repository_repo: FakeRepositoryRepository,
    job_repo: FakeIndexingJobRepository,
    active_session: AnonymousSession,
) -> None:
    service = IngestionService(session_repo, repository_repo, job_repo)
    owner_id = active_session.owner_session_id

    result_gh = service.create_pending_repository(
        owner_id, "github", "gh-repo", github_url="https://github.com/owner/repo"
    )
    result_zip = service.create_pending_repository(owner_id, "zip", "upload.zip")

    assert result_gh.repository.status == "pending"
    assert result_gh.indexing_job.status == "queued"
    assert result_gh.indexing_job.progress_percentage == 0
    assert result_gh.indexing_job.current_step == "Queued for acquisition"

    assert result_zip.repository.status == "pending"
    assert result_zip.indexing_job.status == "queued"
    assert result_zip.indexing_job.progress_percentage == 0
    assert result_zip.indexing_job.current_step == "Queued for archive processing"


def test_no_raw_ids_mongo_details_or_exception_messages_leak_in_service_errors(
    session_repo: FakeSessionRepository,
    active_session: AnonymousSession,
) -> None:
    failing_repo = FakeRepositoryRepository(fail_on_save=True)
    failing_job = FakeIndexingJobRepository()
    service = IngestionService(session_repo, failing_repo, failing_job)

    with pytest.raises(IngestionServiceError) as exc_info:
        service.create_pending_repository(
            active_session.owner_session_id,
            "github",
            "test",
            github_url="https://github.com/o/t",
        )

    err_str = str(exc_info.value)
    assert "ObjectId" not in err_str
    assert "Simulated Mongo Error" not in err_str
    assert "sess_" not in err_str
    assert "https://github.com/o/t" not in err_str
