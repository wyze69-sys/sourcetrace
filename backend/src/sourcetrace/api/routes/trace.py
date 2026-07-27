"""Static Feature Flow Trace API route (TRACE-003).

Zero-token: this route makes no LLM/provider calls. The trace is produced
entirely from flow evidence stored on indexed code chunks.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from sourcetrace.api.dependencies import (
    CurrentOwnerId,
    get_code_chunk_repository,
    get_repository_repository,
    get_trace_explainer_factory,
)
from sourcetrace.api.schemas import UNAUTHORIZED_RESPONSE, ErrorEnvelope
from sourcetrace.core.capabilities import evaluate_capabilities
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import StorageDataError, StorageOperationError
from sourcetrace.generation.trace_explanation import TraceExplanationService
from sourcetrace.retrieval.trace import FlowTraceResult, FlowTraceService, TraceGap
from sourcetrace.storage.repositories import (
    CodeChunkRepository,
    RepositoryRepository,
)

router = APIRouter(prefix="/api/v1/repositories", tags=["trace"])


class FlowTraceRequest(BaseModel):
    """Flow trace request schema (static mode only until explain mode ships)."""

    entry: str = Field(
        ..., min_length=1, max_length=300, description="Symbol or search text to trace from"
    )
    mode: Literal["static", "explain"] = Field(
        default="static",
        description=(
            "static = zero-token deterministic trace; explain = same trace "
            "plus an LLM narration grounded in the trace steps"
        ),
    )
    max_depth: int | None = Field(
        default=None,
        ge=1,
        description="Requested traversal depth; clamped to the server-side maximum",
    )


class TraceEntrySchema(BaseModel):
    query: str
    resolved_node_id: str | None
    candidates: list[str]


class TraceNodeSchema(BaseModel):
    node_id: str
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    snippet: str


class TraceEdgeSchema(BaseModel):
    from_node_id: str
    to_node_id: str
    kind: Literal["call", "import", "http"]
    confidence: Literal["high", "medium", "low"]
    evidence_label: str
    evidence_line_start: int
    evidence_line_end: int
    alternatives: list[str]


class TraceGapSchema(BaseModel):
    kind: str
    detail: str
    node_id: str | None = None


class TraceExplanationSchema(BaseModel):
    text: str
    cited_steps: list[int]


class FlowTraceResponse(BaseModel):
    repository_id: str
    entry: TraceEntrySchema
    nodes: list[TraceNodeSchema]
    edges: list[TraceEdgeSchema]
    steps: list[str]
    gaps: list[TraceGapSchema]
    explanation: TraceExplanationSchema | None = None


def _result_to_response(
    repository_id: str,
    result: FlowTraceResult,
    explanation: TraceExplanationSchema | None = None,
) -> FlowTraceResponse:
    return FlowTraceResponse(
        repository_id=repository_id,
        entry=TraceEntrySchema(
            query=result.entry.query,
            resolved_node_id=result.entry.resolved_node_id,
            candidates=list(result.entry.candidates),
        ),
        nodes=[
            TraceNodeSchema(
                node_id=n.node_id,
                relative_path=n.relative_path,
                symbol_name=n.symbol_name,
                symbol_type=n.symbol_type,
                start_line=n.start_line,
                end_line=n.end_line,
                snippet=n.snippet,
            )
            for n in result.nodes
        ],
        edges=[
            TraceEdgeSchema(
                from_node_id=e.from_node_id,
                to_node_id=e.to_node_id,
                kind=e.kind,
                confidence=e.confidence,
                evidence_label=e.evidence_label,
                evidence_line_start=e.evidence_line_start,
                evidence_line_end=e.evidence_line_end,
                alternatives=list(e.alternatives),
            )
            for e in result.edges
        ],
        steps=list(result.steps),
        gaps=[TraceGapSchema(kind=g.kind, detail=g.detail, node_id=g.node_id) for g in result.gaps],
        explanation=explanation,
    )


def _with_explanation_failed_gap(result: FlowTraceResult) -> FlowTraceResult:
    """Append an explanation_failed gap, preserving the sorted-gaps invariant."""
    gaps = sorted(
        [
            *result.gaps,
            TraceGap(
                kind="explanation_failed",
                detail=(
                    "The explanation was discarded (provider failure or invalid "
                    "step citations); the static trace below is unaffected."
                ),
            ),
        ],
        key=lambda g: (g.kind, g.node_id or "", g.detail),
    )
    return FlowTraceResult(
        entry=result.entry,
        nodes=result.nodes,
        edges=result.edges,
        steps=result.steps,
        gaps=tuple(gaps),
    )


@router.post(
    "/{repository_id}/trace",
    response_model=FlowTraceResponse,
    operation_id="traceFeatureFlow",
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
def trace_feature_flow(
    repository_id: str,
    body: FlowTraceRequest,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    explainer_factory: Annotated[
        Callable[[], TraceExplanationService], Depends(get_trace_explainer_factory)
    ],
) -> FlowTraceResponse:
    """Produce a deterministic static flow trace from stored chunk evidence."""
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
            detail=f"Repository is not ready for tracing (status: {repo.status}).",
        )

    if body.mode == "explain":
        caps = evaluate_capabilities(settings)
        if not caps.generation_available:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Explain mode requires a configured LLM provider; "
                    "this server only supports static traces."
                ),
            )

    service = FlowTraceService(code_chunk_repo)
    try:
        result = service.trace(
            owner_session_id=owner_session_id,
            repository_id=clean_repo_id,
            entry_query=body.entry,
            max_depth=body.max_depth,
            generation_id=repo.active_generation_id,
            repository=repo,
        )
    except StorageDataError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        ) from err
    except StorageOperationError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Trace operation failed safely.",
        ) from err

    explanation: TraceExplanationSchema | None = None
    if body.mode == "explain" and result.steps:
        try:
            explained = explainer_factory().explain(result)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            explained = None
        if explained is None:
            result = _with_explanation_failed_gap(result)
        else:
            explanation = TraceExplanationSchema(text=explained[0], cited_steps=list(explained[1]))

    return _result_to_response(clean_repo_id, result, explanation)
