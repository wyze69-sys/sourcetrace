"""Indexing job status API read routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from sourcetrace.api.dependencies import (
    CurrentOwnerId,
    get_indexing_job_repository,
)
from sourcetrace.api.schemas import (
    ErrorEnvelope,
    IndexingJob,
    job_record_to_schema,
)
from sourcetrace.storage.repositories import IndexingJobRepository

router = APIRouter(tags=["indexing-jobs"])


@router.get(
    "/indexing-jobs/{job_id}",
    response_model=IndexingJob,
    operation_id="getIndexingJobStatus",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "Authentication credentials are missing or invalid",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Resource missing or owned by another session",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def get_indexing_job_status(
    job_id: str,
    owner_session_id: CurrentOwnerId,
    job_repo: Annotated[IndexingJobRepository, Depends(get_indexing_job_repository)],
) -> IndexingJob:
    """Get details for a specific indexing job owned by the current anonymous session."""
    record = job_repo.get_by_id(owner_session_id, job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested resource was not found.",
        )
    return job_record_to_schema(record)
