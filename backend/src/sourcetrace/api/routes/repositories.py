import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from sourcetrace.api.dependencies import (
    CurrentOwnerId,
    GitHubIndexingScheduler,
    GitHubRefreshScheduler,
    ZipIndexingScheduler,
    get_code_chunk_repository,
    get_conversation_repository,
    get_github_indexing_scheduler,
    get_github_refresh_scheduler,
    get_indexing_job_repository,
    get_ingestion_service,
    get_message_repository,
    get_repository_repository,
    get_session_repository,
    get_upload_staging_store,
    get_zip_indexing_scheduler,
)
from sourcetrace.api.schemas import (
    UNAUTHORIZED_RESPONSE,
    CreateGitHubRepositoryRequest,
    CreateRepositoryResponse,
    DeleteRepositoryResponse,
    ErrorEnvelope,
    Repository,
    RepositoryFileContentResponse,
    RepositoryFileItem,
    RepositoryFileListResponse,
    RepositoryListResponse,
    job_record_to_schema,
    repository_record_to_schema,
)
from sourcetrace.core.capabilities import evaluate_capabilities
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import (
    RepositoryQuotaExceededError,
    RepositoryValidationError,
)
from sourcetrace.ingestion.freshness import check_github_freshness
from sourcetrace.ingestion.service import IngestionService, RepositoryCreationResult
from sourcetrace.ingestion.upload_staging import (
    StagedUpload,
    UploadPayloadTooLargeError,
    UploadStagingStore,
    UploadValidationError,
)
from sourcetrace.models.domain import (
    IndexingJobRecord,
    RepositoryRecord,
)
from sourcetrace.storage.repositories import (
    AnonymousSessionRepository,
    CodeChunkRepository,
    ConversationRepository,
    IndexingJobRepository,
    MessageRepository,
    RepositoryRepository,
)

router = APIRouter(tags=["repositories"])

_ALLOWED_MEDIA_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}


async def _close_upload_quietly(file: UploadFile) -> None:
    try:
        await file.close()
    except Exception:  # noqa: BLE001
        pass


def _delete_staging_quietly(
    staging_store: UploadStagingStore,
    token: str | None,
) -> None:
    """Safely delete staged upload, suppressing all errors and ignoring None/invalid tokens."""
    if not token or type(token) is not str:
        return
    try:
        staging_store.delete(token)
    except Exception:  # noqa: BLE001
        pass


def _validate_canonical_creation_result(
    result: Any,
    expected_owner_session_id: str,
    expected_source_type: str,
    expected_github_url: str | None = None,
) -> RepositoryCreationResult:
    """Strictly validate that result is a canonical RepositoryCreationResult."""
    if (
        type(result) is not RepositoryCreationResult
        or type(result.repository) is not RepositoryRecord
        or type(result.indexing_job) is not IndexingJobRecord
    ):
        raise ValueError("Invalid creation result type.")

    repo = result.repository
    job = result.indexing_job

    if (
        repo.owner_session_id != expected_owner_session_id
        or type(repo.repository_id) is not str
        or not repo.repository_id
        or repo.source_type != expected_source_type
        or repo.status != "pending"
        or type(repo.file_count) is not int
        or isinstance(repo.file_count, bool)
        or repo.file_count < 0
        or type(repo.chunk_count) is not int
        or isinstance(repo.chunk_count, bool)
        or repo.chunk_count < 0
    ):
        raise ValueError("Invalid repository record.")

    if expected_source_type == "github":
        if (
            repo.github_url != expected_github_url
            or type(repo.github_url) is not str
            or not repo.github_url
        ):
            raise ValueError("Invalid GitHub repository URL record.")
    elif expected_source_type == "zip":
        if repo.github_url is not None:
            raise ValueError("ZIP repository record cannot have a GitHub URL.")

    if (
        job.owner_session_id != expected_owner_session_id
        or type(job.job_id) is not str
        or not job.job_id
        or job.repository_id != repo.repository_id
        or job.status != "queued"
        or type(job.progress_percentage) is not int
        or isinstance(job.progress_percentage, bool)
        or job.progress_percentage != 0
        or job.completed_at is not None
        or job.error_message is not None
    ):
        raise ValueError("Invalid indexing job record.")

    return result


def _compensate_creation_failure(
    owner_session_id: str,
    repository_id: str,
    job_id: str,
    job_repo: IndexingJobRepository,
    repository_repo: RepositoryRepository,
) -> None:
    """Race-safe job-first status failure compensation helper."""
    now_fail = datetime.now(UTC)
    failed_job: IndexingJobRecord | None = None
    try:
        failed_job = job_repo.transition_status(
            owner_session_id=owner_session_id,
            job_id=job_id,
            repository_id=repository_id,
            expected_status="queued",
            new_status="failed",
            current_step="Scheduling failed",
            progress_percentage=None,
            updated_at=now_fail,
            error_message="Indexing could not be scheduled safely.",
            completed_at=now_fail,
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
            repository_repo.transition_status(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                expected_status="pending",
                new_status="failed",
                updated_at=now_fail,
            )
        except Exception:  # noqa: BLE001
            pass


@router.post(
    "/repositories/upload",
    response_model=CreateRepositoryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="uploadZipRepository",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "model": ErrorEnvelope,
            "description": "Upload archive exceeds maximum limit (25 MB)",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Validation error or invalid ZIP upload",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorEnvelope,
            "description": "Repository quota exceeded",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
async def upload_zip_repository(
    request: Request,
    background_tasks: BackgroundTasks,
    owner_session_id: CurrentOwnerId,
    ingestion_service: Annotated[IngestionService, Depends(get_ingestion_service)],
    zip_scheduler: Annotated[ZipIndexingScheduler, Depends(get_zip_indexing_scheduler)],
    staging_store: Annotated[UploadStagingStore, Depends(get_upload_staging_store)],
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    job_repo: Annotated[IndexingJobRepository, Depends(get_indexing_job_repository)],
    file: Annotated[UploadFile, File(...)],
    name: Annotated[str | None, Form()] = None,
) -> CreateRepositoryResponse:
    """Stream a ZIP archive to staging and schedule background indexing."""
    if not file or not file.filename or not file.filename.strip():
        await _close_upload_quietly(file)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Filename must be a non-empty string.",
        )
    filename = file.filename.strip()
    if len(filename) > 255:
        await _close_upload_quietly(file)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Filename exceeds maximum length limit.",
        )
    if re.search(r"[\x00-\x1f\x7f-\x9f]", filename):
        await _close_upload_quietly(file)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Filename contains control characters.",
        )
    if (
        "/" in filename
        or "\\" in filename
        or ":" in filename
        or ".." in filename
        or filename in (".", "..")
    ):
        await _close_upload_quietly(file)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Filename contains unsafe path components.",
        )
    if not filename.lower().endswith(".zip"):
        await _close_upload_quietly(file)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded file must have a .zip extension.",
        )

    if file.content_type and file.content_type.strip():
        c_type = file.content_type.lower().strip()
        if c_type not in _ALLOWED_MEDIA_TYPES:
            await _close_upload_quietly(file)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid media type for ZIP archive upload.",
            )

    form_data = await request.form()
    supplied_name: str | None = None
    if "name" in form_data:
        raw_val = form_data["name"]
        if isinstance(raw_val, str):
            supplied_name = raw_val
        elif raw_val is None:
            supplied_name = ""

    display_name: str
    if supplied_name is not None:
        if not supplied_name.strip():
            await _close_upload_quietly(file)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Display name cannot be empty or whitespace-only.",
            )
        if re.search(r"[\x00-\x1f\x7f-\x9f]", supplied_name):
            await _close_upload_quietly(file)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Display name contains control characters.",
            )
        if len(supplied_name) > 256 or len(supplied_name.strip()) > 256:
            await _close_upload_quietly(file)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Display name exceeds 256 characters.",
            )
        display_name = supplied_name.strip()
    else:
        stem = Path(filename).stem.strip()
        if not stem or stem in (".", "..") or re.search(r"[\x00-\x1f\x7f-\x9f]", stem):
            await _close_upload_quietly(file)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Could not derive a safe display name from filename.",
            )
        if len(stem) > 256:
            await _close_upload_quietly(file)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Derived display name exceeds 256 characters.",
            )
        display_name = stem

    staged_upload: StagedUpload | None = None
    try:
        staged_upload = await staging_store.stage(file)
    except UploadPayloadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except UploadValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    staging_token = staged_upload.token

    result: RepositoryCreationResult | None = None
    try:
        raw_result = ingestion_service.create_pending_repository(
            owner_session_id=owner_session_id,
            source_type="zip",
            name=display_name,
            github_url=None,
        )
        result = _validate_canonical_creation_result(
            raw_result,
            expected_owner_session_id=owner_session_id,
            expected_source_type="zip",
            expected_github_url=None,
        )
    except RepositoryValidationError as exc:
        _delete_staging_quietly(staging_store, staging_token)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except RepositoryQuotaExceededError as exc:
        _delete_staging_quietly(staging_store, staging_token)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except Exception:
        _delete_staging_quietly(staging_store, staging_token)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    # Construct response payload BEFORE scheduling
    try:
        response_payload = CreateRepositoryResponse(
            repository=repository_record_to_schema(result.repository),
            indexing_job=job_record_to_schema(result.indexing_job),
        )
    except Exception:
        _delete_staging_quietly(staging_store, staging_token)
        _compensate_creation_failure(
            owner_session_id=owner_session_id,
            repository_id=result.repository.repository_id,
            job_id=result.indexing_job.job_id,
            job_repo=job_repo,
            repository_repo=repository_repo,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    # Schedule background processing
    try:
        zip_scheduler.schedule(
            background_tasks=background_tasks,
            owner_session_id=owner_session_id,
            repository_id=result.repository.repository_id,
            job_id=result.indexing_job.job_id,
            staging_token=staging_token,
        )
    except Exception:
        _delete_staging_quietly(staging_store, staging_token)
        _compensate_creation_failure(
            owner_session_id=owner_session_id,
            repository_id=result.repository.repository_id,
            job_id=result.indexing_job.job_id,
            job_repo=job_repo,
            repository_repo=repository_repo,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    return response_payload


@router.get(
    "/repositories",
    response_model=RepositoryListResponse,
    operation_id="listRepositories",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def list_repositories(
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
) -> RepositoryListResponse:
    """List all repositories owned by the caller's session."""
    try:
        records = repository_repo.list_by_owner(owner_session_id)
        repos = [repository_record_to_schema(r) for r in records]
        return RepositoryListResponse(repositories=repos)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc


@router.get(
    "/repositories/{repository_id}",
    response_model=Repository,
    operation_id="getRepository",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Repository not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def get_repository(
    repository_id: str,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    check_freshness: bool = False,
) -> Repository:
    """Fetch details of a single repository owned by caller's session."""
    try:
        record = repository_repo.get_by_id(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if (
        check_freshness
        and settings.enable_stale_check
        and record.source_type == "github"
        and record.github_url
        and record.indexed_branch
        and record.indexed_commit_sha
    ):
        now = datetime.now(UTC)
        is_stale, _remote_sha = check_github_freshness(
            github_url=record.github_url,
            indexed_branch=record.indexed_branch,
            indexed_commit_sha=record.indexed_commit_sha,
            now=now,
        )
        if is_stale is not None:
            try:
                updated_record = repository_repo.update_staleness(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    is_stale=is_stale,
                    stale_checked_at=now,
                )
                if updated_record is not None:
                    record = updated_record
            except Exception:  # noqa: BLE001
                pass

    return repository_record_to_schema(record)


@router.get(
    "/repositories/{repository_id}/files",
    response_model=RepositoryFileListResponse,
    operation_id="listRepositoryFiles",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Repository not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def list_repository_files(
    repository_id: str,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
) -> RepositoryFileListResponse:
    """List unique indexed files for a repository owned by the caller's session."""
    clean_repo_id = repository_id.strip()

    try:
        record = repository_repo.get_by_id(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    if record is None or record.owner_session_id != owner_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    try:
        chunks = code_chunk_repo.list_by_repository(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
            generation_id=record.active_generation_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    files_map: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        path = chunk.relative_path
        if path not in files_map:
            files_map[path] = {
                "path": path,
                "language": chunk.language or "unknown",
                "chunk_count": 1,
            }
        else:
            files_map[path]["chunk_count"] += 1

    sorted_files = [
        RepositoryFileItem(
            path=file_info["path"],
            language=file_info["language"],
            chunk_count=file_info["chunk_count"],
        )
        for file_info in sorted(files_map.values(), key=lambda f: f["path"])
    ]

    return RepositoryFileListResponse(
        repository_id=clean_repo_id,
        files=sorted_files,
    )


def _validate_safe_relative_path(path: str) -> str:
    """Validate relative path to prevent traversal, absolute paths, or drive prefixes."""
    if not path or not path.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query parameter 'path' is required.",
        )

    clean_path = path.strip()

    if "\x00" in clean_path or "%00" in clean_path.lower() or ":" in clean_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path format.",
        )

    if clean_path.startswith("/") or clean_path.startswith("\\"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Absolute file paths are not allowed.",
        )

    normalized = clean_path.replace("\\", "/")
    segments = normalized.split("/")

    for seg in segments:
        if seg == "..":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path traversal segments are not allowed.",
            )

    return normalized


@router.get(
    "/repositories/{repository_id}/files/content",
    response_model=RepositoryFileContentResponse,
    operation_id="getRepositoryFileContent",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorEnvelope,
            "description": "Invalid query parameter or path traversal attempt",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Repository or file not found",
        },
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {
            "model": ErrorEnvelope,
            "description": "File content exceeds maximum payload size",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def get_repository_file_content(
    repository_id: str,
    path: str,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
) -> RepositoryFileContentResponse:
    """Retrieve reconstructed source code text for an indexed file in a repository."""
    clean_repo_id = repository_id.strip()
    safe_path = _validate_safe_relative_path(path)

    try:
        record = repository_repo.get_by_id(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    if record is None or record.owner_session_id != owner_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository or file not found",
        )

    try:
        chunks = code_chunk_repo.list_by_repository(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
            generation_id=record.active_generation_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    file_chunks = [c for c in chunks if c.relative_path == safe_path]
    if not file_chunks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository or file not found",
        )

    file_chunks.sort(key=lambda c: (c.start_line, c.end_line, c.chunk_id))
    language = file_chunks[0].language or "plaintext"

    line_map: dict[int, str] = {}
    for chunk in file_chunks:
        snippet_lines = chunk.content.splitlines()
        for idx, line_text in enumerate(snippet_lines):
            line_no = chunk.start_line + idx
            line_map[line_no] = line_text

    if not line_map:
        return RepositoryFileContentResponse(
            repository_id=clean_repo_id,
            path=safe_path,
            language=language,
            content="",
            line_count=0,
            is_complete=False,
            completeness_reason="source_boundary_unavailable",
        )

    min_line = min(line_map.keys())
    max_line = max(line_map.keys())

    has_gaps = (min_line != 1) or not all(
        line_num in line_map for line_num in range(1, max_line + 1)
    )

    if has_gaps:
        is_complete = False
        completeness_reason = "unindexed_line_gaps"
    else:
        is_complete = False
        completeness_reason = "source_boundary_unavailable"

    reconstructed_lines: list[str] = []
    for line_num in range(1, max_line + 1):
        reconstructed_lines.append(line_map.get(line_num, ""))

    reconstructed_content = "\n".join(reconstructed_lines)

    MAX_FILE_BYTES = 2 * 1024 * 1024
    if len(reconstructed_content.encode("utf-8")) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File content exceeds maximum viewer payload size limit (2 MB).",
        )

    return RepositoryFileContentResponse(
        repository_id=clean_repo_id,
        path=safe_path,
        language=language,
        content=reconstructed_content,
        line_count=max_line,
        is_complete=is_complete,
        completeness_reason=completeness_reason,
    )


@router.delete(
    "/repositories/{repository_id}",
    response_model=DeleteRepositoryResponse,
    operation_id="deleteRepository",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Repository not found",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def delete_repository(
    repository_id: str,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    job_repo: Annotated[IndexingJobRepository, Depends(get_indexing_job_repository)],
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
    conversation_repo: Annotated[ConversationRepository, Depends(get_conversation_repository)],
    message_repo: Annotated[MessageRepository, Depends(get_message_repository)],
    session_repo: Annotated[AnonymousSessionRepository, Depends(get_session_repository)],
) -> DeleteRepositoryResponse:
    """Delete a repository and all its associated data owned by the caller's session."""
    clean_repo_id = repository_id.strip()

    try:
        repo = repository_repo.get_by_id(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    if repo is None or repo.owner_session_id != owner_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    try:
        # Delete associated records in strict dependency order
        message_repo.delete_by_repository(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
        conversation_repo.delete_by_repository(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
        code_chunk_repo.delete_by_repository(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
        job_repo.delete_by_repository(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
        repository_repo.delete(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
        session_repo.release_repository_slot(owner_session_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    return DeleteRepositoryResponse(
        message="Repository deleted successfully.",
        repository_id=clean_repo_id,
    )


@router.post(
    "/repositories",
    response_model=CreateRepositoryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="createGitHubRepository",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Validation error or invalid GitHub URL",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorEnvelope,
            "description": "Repository quota exceeded",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def create_github_repository(
    request: CreateGitHubRepositoryRequest,
    background_tasks: BackgroundTasks,
    owner_session_id: CurrentOwnerId,
    ingestion_service: Annotated[IngestionService, Depends(get_ingestion_service)],
    scheduler: Annotated[GitHubIndexingScheduler, Depends(get_github_indexing_scheduler)],
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    job_repo: Annotated[IndexingJobRepository, Depends(get_indexing_job_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreateRepositoryResponse:
    caps = evaluate_capabilities(settings)
    requested_mode = (
        request.index_mode if request.index_mode is not None else caps.default_index_mode
    )

    if requested_mode not in caps.allowed_index_modes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Requested index mode {requested_mode!r} is not allowed by server configuration."
            ),
        )

    req_mode = requested_mode

    result: RepositoryCreationResult | None = None
    try:
        raw_result = ingestion_service.create_pending_repository(
            owner_session_id=owner_session_id,
            source_type="github",
            github_url=request.github_url,
            index_mode=req_mode,
        )
        result = _validate_canonical_creation_result(
            raw_result,
            expected_owner_session_id=owner_session_id,
            expected_source_type="github",
            expected_github_url=request.github_url,
        )
    except RepositoryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except RepositoryQuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    # Construct response payload BEFORE scheduling
    try:
        response_payload = CreateRepositoryResponse(
            repository=repository_record_to_schema(result.repository),
            indexing_job=job_record_to_schema(result.indexing_job),
        )
    except Exception:
        _compensate_creation_failure(
            owner_session_id=owner_session_id,
            repository_id=result.repository.repository_id,
            job_id=result.indexing_job.job_id,
            job_repo=job_repo,
            repository_repo=repository_repo,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    # Schedule background processing
    try:
        scheduler.schedule(
            background_tasks=background_tasks,
            owner_session_id=owner_session_id,
            repository_id=result.repository.repository_id,
            job_id=result.indexing_job.job_id,
        )
    except Exception:
        _compensate_creation_failure(
            owner_session_id=owner_session_id,
            repository_id=result.repository.repository_id,
            job_id=result.indexing_job.job_id,
            job_repo=job_repo,
            repository_repo=repository_repo,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    return response_payload


@router.post(
    "/repositories/{repository_id}/refresh",
    response_model=CreateRepositoryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="refreshRepository",
    responses={
        **UNAUTHORIZED_RESPONSE,
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Repository not found",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Repository refresh or indexing is already in progress",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Refresh is only supported for ready GitHub repositories",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def refresh_repository(
    repository_id: str,
    background_tasks: BackgroundTasks,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    job_repo: Annotated[IndexingJobRepository, Depends(get_indexing_job_repository)],
    scheduler: Annotated[GitHubRefreshScheduler, Depends(get_github_refresh_scheduler)],
) -> CreateRepositoryResponse:
    """Trigger a generation-safe refresh for an existing ready GitHub repository."""
    clean_repo_id = repository_id.strip()

    try:
        repo = repository_repo.get_by_id(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    if repo is None or repo.owner_session_id != owner_session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if repo.source_type != "github" or repo.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Repository refresh is only supported for ready GitHub repositories.",
        )

    try:
        existing_job = job_repo.get_by_repository(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    if existing_job is not None and existing_job.status in (
        "queued",
        "acquiring",
        "scanning",
        "parsing",
        "embedding",
        "storing",
        "processing",
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Repository refresh or indexing is already in progress.",
        )

    import uuid

    job_id = f"job_ref_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC)
    job_record = IndexingJobRecord(
        job_id=job_id,
        repository_id=clean_repo_id,
        owner_session_id=owner_session_id,
        status="queued",
        job_type="refresh",
        current_step="Queued repository refresh",
        progress_percentage=0,
        created_at=now,
        updated_at=now,
    )

    try:
        job_repo.save(job_record)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from exc

    try:
        scheduler.schedule(
            background_tasks=background_tasks,
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
            job_id=job_id,
        )
    except Exception:
        try:
            job_repo.transition_status(
                owner_session_id=owner_session_id,
                job_id=job_id,
                repository_id=clean_repo_id,
                expected_status="queued",
                new_status="failed",
                current_step="Refresh scheduling failed",
                progress_percentage=None,
                error_message="Could not schedule refresh task safely.",
                updated_at=now,
                completed_at=now,
            )
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal server error occurred.",
        ) from None

    return CreateRepositoryResponse(
        repository=repository_record_to_schema(repo),
        indexing_job=job_record_to_schema(job_record),
    )
