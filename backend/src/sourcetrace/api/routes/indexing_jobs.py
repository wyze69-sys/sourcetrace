"""Indexing job status API read routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from sourcetrace.api.dependencies import (
    get_current_session,
    get_indexing_job_repository,
)
from sourcetrace.api.schemas import (
    ErrorEnvelope,
    IndexingJob,
    job_record_to_schema,
)
from sourcetrace.models.domain import AnonymousSession
from sourcetrace.storage.repositories import IndexingJobRepository

router = APIRouter(tags=["indexing-jobs"])


@router.get(
    "/indexing-jobs/{job_id}",
    response_model=IndexingJob,
    operation_id="getIndexingJobStatus",
    responses={
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
    current_session: Annotated[AnonymousSession, Depends(get_current_session)],
    job_repo: Annotated[IndexingJobRepository, Depends(get_indexing_job_repository)],
) -> IndexingJob:
    """Get details for a specific indexing job owned by the current anonymous session."""
    record = job_repo.get_by_id(current_session.owner_session_id, job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested resource was not found.",
        )
    return job_record_to_schema(record)
