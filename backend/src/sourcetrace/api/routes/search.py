"""Dedicated Evidence Search API route for token-free code citation search."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from sourcetrace.api.dependencies import (
    get_code_chunk_repository,
    get_current_session,
    get_repository_repository,
)
from sourcetrace.core.exceptions import StorageDataError, StorageOperationError
from sourcetrace.models.domain import AnonymousSession
from sourcetrace.storage.repositories import (
    CodeChunkRepository,
    RepositoryRepository,
)

router = APIRouter(prefix="/api/v1/repositories", tags=["search"])


class EvidenceSearchRequest(BaseModel):
    """Evidence search query request schema."""

    query: str = Field(..., min_length=1, max_length=500, description="Search term or symbol name")
    limit: int = Field(default=5, ge=1, le=50, description="Maximum evidence items to return")


class EvidenceSearchItem(BaseModel):
    """Citation item returned from evidence search."""

    chunk_id: str
    score: float
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    snippet: str


class EvidenceSearchResponse(BaseModel):
    """Response container for evidence search."""

    repository_id: str
    total: int
    items: list[EvidenceSearchItem]


@router.post("/{repository_id}/search", response_model=EvidenceSearchResponse)
def search_repository_evidence(
    repository_id: str,
    body: EvidenceSearchRequest,
    session: Annotated[AnonymousSession, Depends(get_current_session)],
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
) -> EvidenceSearchResponse:
    """Execute token-free lexical evidence search over ready static repository code chunks."""
    owner_session_id = session.owner_session_id
    clean_repo_id = repository_id.strip()

    if not clean_repo_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository ID is required.",
        )

    repo = repository_repo.get_by_id(
        owner_session_id=owner_session_id,
        repository_id=clean_repo_id,
    )

    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found.",
        )

    if repo.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Repository is not ready for search (status: {repo.status}).",
        )

    if getattr(repo, "index_mode", "static") != "static":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evidence search endpoint requires a static repository.",
        )

    try:
        results = code_chunk_repo.search_lexical(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
            query_text=body.query,
            limit=body.limit,
        )
    except StorageDataError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        ) from err
    except StorageOperationError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search operation failed safely.",
        ) from err

    items: list[EvidenceSearchItem] = []
    for res in results:
        chunk = res.chunk
        snippet = chunk.content
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "\n... [truncated]"

        items.append(
            EvidenceSearchItem(
                chunk_id=chunk.chunk_id,
                score=res.score,
                relative_path=chunk.relative_path,
                symbol_name=chunk.symbol_name,
                symbol_type=chunk.symbol_type,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                snippet=snippet,
            )
        )

    return EvidenceSearchResponse(
        repository_id=clean_repo_id,
        total=len(items),
        items=items,
    )
