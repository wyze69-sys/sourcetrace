"""Static Change Impact Preview API route (IMPACT-001).

Zero-token: this route makes no LLM/provider calls. The impact preview is
produced entirely from flow evidence stored on indexed code chunks.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from sourcetrace.api.dependencies import (
    CurrentOwnerId,
    get_code_chunk_repository,
    get_repository_repository,
)
from sourcetrace.api.schemas import UNAUTHORIZED_RESPONSE, ErrorEnvelope
from sourcetrace.core.exceptions import StorageDataError, StorageOperationError
from sourcetrace.retrieval.diff import DiffParseError
from sourcetrace.retrieval.impact import (
    ChangeImpactResult,
    ChangeImpactService,
    DiffImpactResult,
)
from sourcetrace.storage.repositories import (
    CodeChunkRepository,
    RepositoryRepository,
)

router = APIRouter(prefix="/api/v1/repositories", tags=["impact"])


class ChangeImpactRequest(BaseModel):
    """Change impact preview request schema (static mode only)."""

    symbol: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="Symbol or search text identifying the change target",
    )
    max_depth: int | None = Field(
        default=None,
        ge=1,
        description="Requested traversal depth; clamped to the server-side maximum",
    )


class DiffImpactRequest(BaseModel):
    """Diff impact preview request schema (static mode only)."""

    diff: str = Field(
        ...,
        min_length=1,
        max_length=200_000,
        description="Unified diff text against the indexed repository baseline",
    )
    max_depth: int | None = Field(
        default=None,
        ge=1,
        description="Requested traversal depth; clamped to the server-side maximum",
    )


class ImpactTargetSchema(BaseModel):
    query: str
    resolved_node_id: str | None
    candidates: list[str]


class DiffTargetSchema(BaseModel):
    node_id: str
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    changed_lines: list[int]


class ImpactItemSchema(BaseModel):
    node_id: str
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    distance: int
    confidence: Literal["high", "medium", "low"]
    edge_kind: Literal["call", "http"]
    via_node_id: str
    evidence_node_id: str
    evidence_label: str
    evidence_line_start: int
    evidence_line_end: int


class AffectedEndpointSchema(BaseModel):
    http_method: str
    normalized_path: str
    node_id: str


class RiskFactorSchema(BaseModel):
    kind: str
    severity: Literal["low", "medium", "high"]
    detail: str


class ImpactGapSchema(BaseModel):
    kind: str
    detail: str
    node_id: str | None = None


class ChangeImpactResponse(BaseModel):
    repository_id: str
    target: ImpactTargetSchema
    upstream: list[ImpactItemSchema]
    downstream: list[ImpactItemSchema]
    affected_endpoints: list[AffectedEndpointSchema]
    affected_components: list[str]
    affected_tests: list[str]
    risk_level: Literal["low", "medium", "high", "unknown"]
    risk_factors: list[RiskFactorSchema]
    gaps: list[ImpactGapSchema]


class DiffImpactResponse(BaseModel):
    repository_id: str
    targets: list[DiffTargetSchema]
    upstream: list[ImpactItemSchema]
    downstream: list[ImpactItemSchema]
    affected_endpoints: list[AffectedEndpointSchema]
    affected_components: list[str]
    affected_tests: list[str]
    risk_level: Literal["low", "medium", "high", "unknown"]
    risk_factors: list[RiskFactorSchema]
    gaps: list[ImpactGapSchema]


def _items_to_schema(items) -> list[ImpactItemSchema]:
    return [
        ImpactItemSchema(
            node_id=item.node_id,
            relative_path=item.relative_path,
            symbol_name=item.symbol_name,
            symbol_type=item.symbol_type,
            start_line=item.start_line,
            end_line=item.end_line,
            distance=item.distance,
            confidence=item.confidence,
            edge_kind=item.edge_kind,
            via_node_id=item.via_node_id,
            evidence_node_id=item.evidence_node_id,
            evidence_label=item.evidence_label,
            evidence_line_start=item.evidence_line_start,
            evidence_line_end=item.evidence_line_end,
        )
        for item in items
    ]


def _diff_result_to_response(
    repository_id: str, result: DiffImpactResult
) -> DiffImpactResponse:
    return DiffImpactResponse(
        repository_id=repository_id,
        targets=[
            DiffTargetSchema(
                node_id=t.node_id,
                relative_path=t.relative_path,
                symbol_name=t.symbol_name,
                symbol_type=t.symbol_type,
                start_line=t.start_line,
                end_line=t.end_line,
                changed_lines=list(t.changed_lines),
            )
            for t in result.targets
        ],
        upstream=_items_to_schema(result.upstream),
        downstream=_items_to_schema(result.downstream),
        affected_endpoints=[
            AffectedEndpointSchema(
                http_method=e.http_method,
                normalized_path=e.normalized_path,
                node_id=e.node_id,
            )
            for e in result.affected_endpoints
        ],
        affected_components=list(result.affected_components),
        affected_tests=list(result.affected_tests),
        risk_level=result.risk_level,
        risk_factors=[
            RiskFactorSchema(kind=f.kind, severity=f.severity, detail=f.detail)
            for f in result.risk_factors
        ],
        gaps=[
            ImpactGapSchema(kind=g.kind, detail=g.detail, node_id=g.node_id)
            for g in result.gaps
        ],
    )


def _load_ready_repository(
    repository_id: str,
    owner_session_id: str,
    repository_repo: RepositoryRepository,
) -> str:
    """Validate + authorize the repository; return the cleaned id."""
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
        # get_by_id is owner-scoped: a repository owned by another session
        # resolves to None exactly like a nonexistent one (uniform 404).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found.",
        )

    if repo.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Repository is not ready for impact analysis (status: {repo.status}).",
        )

    return clean_repo_id


def _result_to_response(
    repository_id: str, result: ChangeImpactResult
) -> ChangeImpactResponse:
    return ChangeImpactResponse(
        repository_id=repository_id,
        target=ImpactTargetSchema(
            query=result.target.query,
            resolved_node_id=result.target.resolved_node_id,
            candidates=list(result.target.candidates),
        ),
        upstream=_items_to_schema(result.upstream),
        downstream=_items_to_schema(result.downstream),
        affected_endpoints=[
            AffectedEndpointSchema(
                http_method=e.http_method,
                normalized_path=e.normalized_path,
                node_id=e.node_id,
            )
            for e in result.affected_endpoints
        ],
        affected_components=list(result.affected_components),
        affected_tests=list(result.affected_tests),
        risk_level=result.risk_level,
        risk_factors=[
            RiskFactorSchema(kind=f.kind, severity=f.severity, detail=f.detail)
            for f in result.risk_factors
        ],
        gaps=[
            ImpactGapSchema(kind=g.kind, detail=g.detail, node_id=g.node_id)
            for g in result.gaps
        ],
    )


@router.post(
    "/{repository_id}/impact",
    response_model=ChangeImpactResponse,
    operation_id="previewChangeImpact",
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
def preview_change_impact(
    repository_id: str,
    body: ChangeImpactRequest,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
) -> ChangeImpactResponse:
    """Produce a deterministic static change impact preview for a symbol."""
    clean_repo_id = _load_ready_repository(
        repository_id, owner_session_id, repository_repo
    )

    service = ChangeImpactService(code_chunk_repo)
    try:
        result = service.preview(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
            symbol_query=body.symbol,
            max_depth=body.max_depth,
        )
    except StorageDataError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        ) from err
    except StorageOperationError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Impact analysis failed safely.",
        ) from err

    return _result_to_response(clean_repo_id, result)


@router.post(
    "/{repository_id}/impact/diff",
    response_model=DiffImpactResponse,
    operation_id="previewDiffImpact",
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
def preview_diff_impact(
    repository_id: str,
    body: DiffImpactRequest,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
) -> DiffImpactResponse:
    """Produce a deterministic static impact preview for a pasted unified diff."""
    clean_repo_id = _load_ready_repository(
        repository_id, owner_session_id, repository_repo
    )

    service = ChangeImpactService(code_chunk_repo)
    try:
        result = service.preview_diff(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
            diff_text=body.diff,
            max_depth=body.max_depth,
        )
    except DiffParseError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        ) from err
    except StorageDataError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        ) from err
    except StorageOperationError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Impact analysis failed safely.",
        ) from err

    return _diff_result_to_response(clean_repo_id, result)
