"""Integration tests for POST /api/v1/repositories/upload route, staging, errors, and contracts."""

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_current_owner_id,
    get_indexing_job_repository,
    get_ingestion_service,
    get_repository_repository,
    get_session_repository,
    get_session_signer,
    get_upload_staging_store,
    get_zip_indexing_scheduler,
)
from sourcetrace.core.security import SessionSigner
from sourcetrace.ingestion.service import IngestionService, RepositoryCreationResult
from sourcetrace.ingestion.upload_staging import (
    FileSystemUploadStagingStore,
)
from sourcetrace.models.domain import (
    AnonymousSession,
    IndexingJobRecord,
    RepositoryRecord,
)

TEST_SECRET = "a_very_secret_key_that_is_at_least_32_bytes_long!"


def create_minimal_zip_bytes() -> bytes:
    """Create minimal valid in-memory ZIP archive bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", "print('hello world')\n")
    return buf.getvalue()


class RecordingZipIndexingScheduler:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.scheduled_calls: list[tuple[str, str, str, str]] = []

    def schedule(
        self,
        background_tasks: Any,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        staging_token: str,
    ) -> None:
        if self.should_fail:
            raise RuntimeError("Scheduler error")
        self.scheduled_calls.append((owner_session_id, repository_id, job_id, staging_token))


class InMemoryAnonymousSessionRepository:
    def __init__(self, sessions: list[AnonymousSession] | None = None) -> None:
        self.sessions: dict[str, AnonymousSession] = {
            s.owner_session_id: s for s in (sessions or [])
        }
        self.reserve_called_count = 0
        self.release_called_count = 0

    def get_by_id(self, owner_session_id: str) -> AnonymousSession | None:
        return self.sessions.get(owner_session_id)

    def save(self, session: AnonymousSession) -> AnonymousSession:
        self.sessions[session.owner_session_id] = session
        return session

    def delete(self, owner_session_id: str) -> bool:
        return self.sessions.pop(owner_session_id, None) is not None

    def reserve_repository_slot(
        self, owner_session_id: str, now: datetime, max_quota: int = 3, retention_days: int = 7
    ) -> AnonymousSession | None:
        self.reserve_called_count += 1
        sess = self.get_by_id(owner_session_id)
        if sess is None:
            sess = AnonymousSession(
                owner_session_id=owner_session_id,
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=now + timedelta(days=retention_days),
                active_repository_count=0,
            )
        if sess.active_repository_count >= max_quota:
            return None
        updated = AnonymousSession(
            owner_session_id=sess.owner_session_id,
            created_at=sess.created_at,
            updated_at=now,
            last_active_at=now,
            expires_at=now + timedelta(days=retention_days),
            active_repository_count=sess.active_repository_count + 1,
        )
        self.sessions[owner_session_id] = updated
        return updated

    def release_repository_slot(self, owner_session_id: str) -> bool:
        self.release_called_count += 1
        sess = self.get_by_id(owner_session_id)
        if sess is None or sess.active_repository_count <= 0:
            return False
        updated = AnonymousSession(
            owner_session_id=sess.owner_session_id,
            created_at=sess.created_at,
            updated_at=sess.updated_at,
            last_active_at=sess.last_active_at,
            expires_at=sess.expires_at,
            active_repository_count=sess.active_repository_count - 1,
        )
        self.sessions[owner_session_id] = updated
        return True


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
            github_url=rec.github_url,
            file_count=file_count if file_count is not None else rec.file_count,
            chunk_count=chunk_count if chunk_count is not None else rec.chunk_count,
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
            r
            for r in self.records
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
            j
            for j in self.records
            if not (j.owner_session_id == owner_session_id and j.repository_id == repository_id)
        ]
        return initial - len(self.records)


def setup_upload_test_app(
    session_repo: InMemoryAnonymousSessionRepository,
    repo_repo: InMemoryRepositoryRepository,
    job_repo: InMemoryIndexingJobRepository,
    staging_root: Path,
    scheduler: RecordingZipIndexingScheduler | None = None,
    active_owner_id: str | None = None,
) -> tuple[FastAPI, RecordingZipIndexingScheduler]:
    app = create_app()
    sched = scheduler or RecordingZipIndexingScheduler()
    staging_store = FileSystemUploadStagingStore(staging_root=staging_root)

    owner_id = active_owner_id or (
        list(session_repo.sessions.keys())[0] if session_repo.sessions else "sess_upload_user"
    )

    app.dependency_overrides[get_current_owner_id] = lambda: owner_id
    app.dependency_overrides[get_session_signer] = lambda: SessionSigner(secret=TEST_SECRET)
    app.dependency_overrides[get_session_repository] = lambda: session_repo
    app.dependency_overrides[get_repository_repository] = lambda: repo_repo
    app.dependency_overrides[get_indexing_job_repository] = lambda: job_repo
    app.dependency_overrides[get_ingestion_service] = lambda: IngestionService(
        session_repo=session_repo,
        repository_repo=repo_repo,
        job_repo=job_repo,
    )
    app.dependency_overrides[get_upload_staging_store] = lambda: staging_store
    app.dependency_overrides[get_zip_indexing_scheduler] = lambda: sched

    return app, sched


def test_upload_zip_repository_success(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_upload_user"

    session_repo = InMemoryAnonymousSessionRepository(
        [
            AnonymousSession(
                owner_session_id=owner_id,
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=exp,
            )
        ]
    )
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)
    client = TestClient(app)

    token = SessionSigner(TEST_SECRET).create_cookie_token(owner_id, exp)
    client.cookies.set("sourcetrace_session", token)

    zip_bytes = create_minimal_zip_bytes()
    files = {"file": ("my_project.zip", zip_bytes, "application/zip")}
    data = {"name": "Custom Project Name"}

    res = client.post("/api/v1/repositories/upload", files=files, data=data)
    assert res.status_code == 202
    body = res.json()

    assert "repository" in body
    assert "indexing_job" in body

    repo_data = body["repository"]
    assert repo_data["source_type"] == "zip"
    assert repo_data["status"] == "pending"
    assert repo_data["name"] == "Custom Project Name"
    assert repo_data["github_url"] is None

    job_data = body["indexing_job"]
    assert job_data["status"] == "queued"
    assert job_data["progress_percentage"] == 0

    assert len(scheduler.scheduled_calls) == 1
    sched_owner, sched_repo_id, sched_job_id, sched_token = scheduler.scheduled_calls[0]
    assert sched_owner == owner_id
    assert sched_repo_id == repo_data["repository_id"]
    assert sched_job_id == job_data["job_id"]
    assert sched_token.startswith("stg_")

    staged_path = tmp_path / f"{sched_token}.staged"
    assert staged_path.exists()


@pytest.mark.parametrize(
    "bad_name",
    ["", "   ", "A" * 257, "name\x00with_nul", "name\x07control"],
)
def test_upload_supplied_name_validation_returns_422(tmp_path: Path, bad_name: str) -> None:
    session_repo = InMemoryAnonymousSessionRepository()
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)
    client = TestClient(app)

    files = {"file": ("valid.zip", create_minimal_zip_bytes(), "application/zip")}
    data = {"name": bad_name}

    res = client.post("/api/v1/repositories/upload", files=files, data=data)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert len(scheduler.scheduled_calls) == 0


def test_upload_overlong_derived_stem_returns_422(tmp_path: Path) -> None:
    session_repo = InMemoryAnonymousSessionRepository()
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)
    client = TestClient(app)

    long_filename = ("A" * 260) + ".zip"
    files = {"file": (long_filename, create_minimal_zip_bytes(), "application/zip")}

    res = client.post("/api/v1/repositories/upload", files=files)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert len(scheduler.scheduled_calls) == 0


@pytest.mark.parametrize(
    "incompatible_type",
    ["application/pdf", "application/json", "image/png", "text/html", "audio/mp3"],
)
def test_upload_incompatible_media_type_returns_422(tmp_path: Path, incompatible_type: str) -> None:
    session_repo = InMemoryAnonymousSessionRepository()
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)
    client = TestClient(app)

    files = {"file": ("test.zip", create_minimal_zip_bytes(), incompatible_type)}
    res = client.post("/api/v1/repositories/upload", files=files)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert len(scheduler.scheduled_calls) == 0


def test_upload_missing_content_type_accepted(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    session_repo = InMemoryAnonymousSessionRepository(
        [
            AnonymousSession(
                owner_session_id="sess_no_type",
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=now + timedelta(days=7),
            )
        ]
    )
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)
    client = TestClient(app)

    token = SessionSigner(TEST_SECRET).create_cookie_token("sess_no_type", now + timedelta(days=7))
    client.cookies.set("sourcetrace_session", token)

    files = {"file": ("test.zip", create_minimal_zip_bytes(), "")}
    res = client.post("/api/v1/repositories/upload", files=files)
    assert res.status_code == 202
    assert len(scheduler.scheduled_calls) == 1


def test_upload_malformed_ingestion_service_result_does_not_schedule(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    session_repo = InMemoryAnonymousSessionRepository(
        [
            AnonymousSession(
                owner_session_id="sess_malformed",
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=now + timedelta(days=7),
            )
        ]
    )
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)

    # Return a result with mismatched job status or non-zero progress
    bad_job = IndexingJobRecord(
        job_id="job_bad",
        repository_id="repo_bad",
        owner_session_id="sess_malformed",
        status="ready",  # Malformed! Must be queued
        current_step="Step",
        created_at=now,
        updated_at=now,
        progress_percentage=50,  # Malformed! Must be 0
    )
    bad_repo = RepositoryRecord(
        repository_id="repo_bad",
        owner_session_id="sess_malformed",
        name="Name",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    bad_result = RepositoryCreationResult(repository=bad_repo, indexing_job=bad_job)

    mock_ingestion = MagicMock()
    mock_ingestion.create_pending_repository.return_value = bad_result
    app.dependency_overrides[get_ingestion_service] = lambda: mock_ingestion

    client = TestClient(app, raise_server_exceptions=False)
    exp = now + timedelta(days=7)
    token = SessionSigner(TEST_SECRET).create_cookie_token("sess_malformed", exp)
    client.cookies.set("sourcetrace_session", token)

    files = {"file": ("test.zip", create_minimal_zip_bytes(), "application/zip")}
    res = client.post("/api/v1/repositories/upload", files=files)
    assert res.status_code == 500
    assert len(scheduler.scheduled_calls) == 0
    assert len(list(tmp_path.glob("*.staged"))) == 0


def test_upload_schema_conversion_failure_does_not_schedule(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    session_repo = InMemoryAnonymousSessionRepository(
        [
            AnonymousSession(
                owner_session_id="sess_schema_fail",
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=now + timedelta(days=7),
            )
        ]
    )
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)

    client = TestClient(app, raise_server_exceptions=False)
    exp = now + timedelta(days=7)
    token = SessionSigner(TEST_SECRET).create_cookie_token("sess_schema_fail", exp)
    client.cookies.set("sourcetrace_session", token)

    files = {"file": ("test.zip", create_minimal_zip_bytes(), "application/zip")}

    target_func = "sourcetrace.api.routes.repositories.repository_record_to_schema"
    with patch(target_func, side_effect=ValueError("Schema conversion failed")):
        res = client.post("/api/v1/repositories/upload", files=files)
        assert res.status_code == 500
        assert len(scheduler.scheduled_calls) == 0
        assert len(list(tmp_path.glob("*.staged"))) == 0


def test_upload_zip_openapi_contract() -> None:
    app = create_app()
    spec = app.openapi()

    paths = spec.get("paths", {})
    assert "/api/v1/repositories/upload" in paths
    upload_path = paths["/api/v1/repositories/upload"]
    assert "post" in upload_path
    op = upload_path["post"]
    assert op.get("operationId") == "uploadZipRepository"

    request_body = op.get("requestBody", {})
    content = request_body.get("content", {})
    assert "multipart/form-data" in content

    schema = content["multipart/form-data"].get("schema", {})
    if "$ref" in schema:
        ref_parts = schema["$ref"].lstrip("#/").split("/")
        curr = spec
        for p in ref_parts:
            curr = curr[p]
        schema = curr

    props = schema.get("properties", {})
    assert "file" in props
    assert props["file"].get("type") == "string"
    assert (
        props["file"].get("format") == "binary"
        or props["file"].get("contentMediaType") == "application/octet-stream"
    )
    assert "file" in schema.get("required", [])

    responses = op.get("responses", {})
    for status_code in ["202", "413", "422", "429", "500"]:
        assert status_code in responses


@pytest.mark.parametrize(
    "bad_creation_result",
    [
        pytest.param({"repository": None, "indexing_job": None}, id="dict_result"),
        pytest.param(object(), id="object_result"),
        pytest.param(
            type(
                "ExplodingResult",
                (),
                {"repository": property(fget=lambda self: 1 / 0)},
            )(),
            id="exploding_property_result",
        ),
    ],
)
def test_upload_invalid_creation_result_types_fail_safe(
    tmp_path: Path, bad_creation_result: Any
) -> None:
    now = datetime.now(UTC)
    session_repo = InMemoryAnonymousSessionRepository(
        [
            AnonymousSession(
                owner_session_id="sess_bad_result",
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=now + timedelta(days=7),
            )
        ]
    )
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)

    mock_ingestion = MagicMock()
    mock_ingestion.create_pending_repository.return_value = bad_creation_result
    app.dependency_overrides[get_ingestion_service] = lambda: mock_ingestion

    client = TestClient(app, raise_server_exceptions=False)
    exp = now + timedelta(days=7)
    token = SessionSigner(TEST_SECRET).create_cookie_token("sess_bad_result", exp)
    client.cookies.set("sourcetrace_session", token)

    files = {"file": ("test.zip", create_minimal_zip_bytes(), "application/zip")}
    res = client.post("/api/v1/repositories/upload", files=files)
    assert res.status_code == 500
    assert len(scheduler.scheduled_calls) == 0
    assert len(list(tmp_path.glob("*.staged"))) == 0


def test_upload_subclass_creation_result_rejected(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    session_repo = InMemoryAnonymousSessionRepository(
        [
            AnonymousSession(
                owner_session_id="sess_subclass",
                created_at=now,
                updated_at=now,
                last_active_at=now,
                expires_at=now + timedelta(days=7),
            )
        ]
    )
    repo_repo = InMemoryRepositoryRepository()
    job_repo = InMemoryIndexingJobRepository()

    app, scheduler = setup_upload_test_app(session_repo, repo_repo, job_repo, tmp_path)

    class SubRepositoryRecord(RepositoryRecord):
        pass

    class SubIndexingJobRecord(IndexingJobRecord):
        pass

    sub_repo = SubRepositoryRecord(
        repository_id="repo_sub",
        owner_session_id="sess_subclass",
        name="Name",
        source_type="zip",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    sub_job = SubIndexingJobRecord(
        job_id="job_sub",
        repository_id="repo_sub",
        owner_session_id="sess_subclass",
        status="queued",
        current_step="Queued",
        created_at=now,
        updated_at=now,
        progress_percentage=0,
    )
    sub_result = RepositoryCreationResult(repository=sub_repo, indexing_job=sub_job)

    mock_ingestion = MagicMock()
    mock_ingestion.create_pending_repository.return_value = sub_result
    app.dependency_overrides[get_ingestion_service] = lambda: mock_ingestion

    client = TestClient(app, raise_server_exceptions=False)
    token = SessionSigner(TEST_SECRET).create_cookie_token("sess_subclass", now + timedelta(days=7))
    client.cookies.set("sourcetrace_session", token)

    files = {"file": ("test.zip", create_minimal_zip_bytes(), "application/zip")}
    res = client.post("/api/v1/repositories/upload", files=files)
    assert res.status_code == 500
    assert len(scheduler.scheduled_calls) == 0
    assert len(list(tmp_path.glob("*.staged"))) == 0
