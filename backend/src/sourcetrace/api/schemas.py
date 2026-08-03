"""HTTP request and response schemas."""

from datetime import datetime
from typing import Any, Literal

from fastapi import status
from pydantic import BaseModel, Field

from sourcetrace.models.domain import (
    CitationRecord,
    ConversationRecord,
    EvidenceSnippetRecord,
    IndexingJobRecord,
    MessageRecord,
    RepositoryRecord,
)


class ErrorDetail(BaseModel):
    """Machine-readable and human-readable error payload."""

    code: str
    message: str
    request_id: str | None = Field(default=None, description="Optional request tracking identifier")


class ErrorEnvelope(BaseModel):
    """Standard HTTP error response envelope."""

    error: ErrorDetail


class TokenResponse(BaseModel):
    """Response payload returned when a session token is provisioned."""

    access_token: str = Field(min_length=1, description="Stateless JWT access token")
    token_type: Literal["Bearer"] = Field(default="Bearer", description="Token type")
    expires_in: int = Field(gt=0, description="Token lifetime in seconds")


UNAUTHORIZED_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorEnvelope,
        "description": "Authentication credentials are missing or invalid",
    },
}


class Repository(BaseModel):
    """Public repository response schema."""

    repository_id: str
    name: str
    source_type: Literal["github", "zip"]
    github_url: str | None = None
    status: Literal["pending", "indexing", "ready", "failed"]
    file_count: int
    chunk_count: int
    created_at: datetime
    updated_at: datetime
    index_mode: Literal["static", "cloud_ai"] = "static"
    active_generation_id: str | None = None
    last_indexed_at: datetime | None = None
    indexed_commit_sha: str | None = None
    indexed_branch: str | None = None
    parser_versions: list[str] = Field(default_factory=list)
    flow_evidence_complete: bool = False
    indexed_file_count: int = 0
    indexed_chunk_count: int = 0
    consecutive_refresh_failures: int = 0
    is_stale: bool | None = None
    stale_checked_at: datetime | None = None


class IndexingJob(BaseModel):
    """Public indexing job status response schema."""

    job_id: str
    repository_id: str
    status: Literal[
        "queued",
        "acquiring",
        "scanning",
        "parsing",
        "embedding",
        "storing",
        "ready",
        "failed",
    ]
    progress_percentage: int = Field(ge=0, le=100)
    current_step: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    job_type: str = "initial"


class CreateGitHubRepositoryRequest(BaseModel):
    """Request payload for importing a public GitHub repository."""

    github_url: str = Field(
        json_schema_extra={
            "format": "uri",
            "example": "https://github.com/octocat/Hello-World",
        }
    )
    index_mode: Literal["static", "cloud_ai"] | None = Field(
        default=None,
        description="Requested indexing mode ('static' or 'cloud_ai'). Defaults to server default.",
    )


class CreateRepositoryResponse(BaseModel):
    """Response payload returned when a repository import or refresh is accepted."""

    repository: Repository
    indexing_job: IndexingJob


class RepositoryListResponse(BaseModel):
    """List of session repositories response schema."""

    repositories: list[Repository]


class DeleteRepositoryResponse(BaseModel):
    """Response payload returned when a repository is deleted."""

    message: str
    repository_id: str


class RepositoryFileItem(BaseModel):
    """Metadata for an indexed file within a repository."""

    path: str = Field(description="Relative file path within the repository")
    language: str = Field(description="Programming language or file type identifier")
    chunk_count: int = Field(ge=1, description="Number of indexed code chunks for this file")


class RepositoryFileListResponse(BaseModel):
    """Response payload containing a list of indexed files for a repository."""

    repository_id: str
    files: list[RepositoryFileItem]


class RepositoryFileContentResponse(BaseModel):
    """Response payload containing reconstructed source content for an indexed file."""

    repository_id: str = Field(description="Unique repository identifier")
    path: str = Field(description="Relative file path within the repository")
    language: str = Field(description="Programming language or file type identifier")
    content: str = Field(description="Reconstructed raw source code text")
    line_count: int = Field(ge=0, description="Total line count of the reconstructed content")
    is_complete: bool = Field(
        description="True if all lines from line 1 were reconstructed without gaps"
    )
    completeness_reason: str = Field(
        description="Reason for completeness status (e.g. source_boundary_unavailable)"
    )


class Citation(BaseModel):
    """Citation metadata for a cited code symbol and file location."""

    relative_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol_name: str
    symbol_type: str


class EvidenceSnippet(BaseModel):
    """Evidence snippet content alongside citation metadata."""

    snippet: str
    relative_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol_name: str
    symbol_type: str


class Message(BaseModel):
    """Grounded chat message public schema."""

    message_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    insufficient_evidence: bool = False
    answer_mode: Literal[
        "normal",
        "orientation",
        "architecture",
        "flow",
        "impact",
        "general_chat",
        "conversation",
        "static_guidance",
        "insufficient_orientation",
        "insufficient_evidence",
        "reindex_required",
    ] = "normal"
    intent: str | None = None
    confidence_bucket: Literal["high", "medium", "low"] | None = None
    evidence_count: int | None = Field(default=None, ge=0)
    hop_count: int | None = Field(default=None, ge=0)
    source_categories: list[str] | None = None
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[EvidenceSnippet] = Field(default_factory=list)


class RequestMetadata(BaseModel):
    """Execution latency and evidence count metadata for chat requests."""

    latency_ms: int = Field(ge=0)
    chunks_retrieved: int = Field(ge=0)
    retrieval_mode: Literal["static", "semantic"] = Field(
        default="static",
        description="Evidence retrieval mode used for this AI response ('static' or 'semantic')",
    )


class CreateConversationRequest(BaseModel):
    """Request payload to create a conversation and ask initial question."""

    question: str


class CreateConversationResponse(BaseModel):
    """Response payload returned when a conversation is created."""

    conversation_id: str
    repository_id: str
    user_message: Message
    assistant_message: Message
    request_metadata: RequestMetadata


class ConversationDetailResponse(BaseModel):
    """Response payload containing conversation detail and full message history."""

    conversation_id: str
    repository_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[Message]


class SendMessageRequest(BaseModel):
    """Request payload to send a message in an existing conversation."""

    question: str


class SendMessageResponse(BaseModel):
    """Response payload returned when sending a message."""

    conversation_id: str
    repository_id: str
    user_message: Message
    assistant_message: Message
    request_metadata: RequestMetadata


def repository_record_to_schema(record: RepositoryRecord) -> Repository:
    """Convert domain RepositoryRecord to public Repository schema."""
    idx_mode = record.index_mode if record.index_mode in ("static", "cloud_ai") else "static"
    return Repository(
        repository_id=record.repository_id,
        name=record.name,
        source_type=record.source_type,  # type: ignore[arg-type]
        github_url=record.github_url,
        status=record.status,  # type: ignore[arg-type]
        file_count=record.file_count,
        chunk_count=record.chunk_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
        index_mode=idx_mode,  # type: ignore[arg-type]
        active_generation_id=record.active_generation_id,
        last_indexed_at=record.last_indexed_at,
        indexed_commit_sha=record.indexed_commit_sha,
        indexed_branch=record.indexed_branch,
        parser_versions=list(record.parser_versions) if record.parser_versions else [],
        flow_evidence_complete=record.flow_evidence_complete,
        indexed_file_count=record.indexed_file_count,
        indexed_chunk_count=record.indexed_chunk_count,
        consecutive_refresh_failures=record.consecutive_refresh_failures,
        is_stale=record.is_stale,
        stale_checked_at=record.stale_checked_at,
    )


def job_record_to_schema(record: IndexingJobRecord) -> IndexingJob:
    """Convert domain IndexingJobRecord to public IndexingJob schema."""
    return IndexingJob(
        job_id=record.job_id,
        repository_id=record.repository_id,
        status=record.status,  # type: ignore[arg-type]
        progress_percentage=record.progress_percentage,
        current_step=record.current_step,
        error_message=record.error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
        job_type=record.job_type,
    )


def citation_record_to_schema(record: CitationRecord) -> Citation:
    """Convert domain CitationRecord to public Citation schema safely."""
    if record.start_line < 1 or record.end_line < record.start_line:
        raise ValueError("Invalid citation line numbers")
    return Citation(
        relative_path=record.relative_path,
        start_line=record.start_line,
        end_line=record.end_line,
        symbol_name=record.symbol_name,
        symbol_type=record.symbol_type,
    )


def evidence_record_to_schema(record: EvidenceSnippetRecord) -> EvidenceSnippet:
    """Convert domain EvidenceSnippetRecord to public EvidenceSnippet schema safely."""
    if record.start_line < 1 or record.end_line < record.start_line:
        raise ValueError("Invalid evidence line numbers")
    return EvidenceSnippet(
        snippet=record.snippet,
        relative_path=record.relative_path,
        start_line=record.start_line,
        end_line=record.end_line,
        symbol_name=record.symbol_name,
        symbol_type=record.symbol_type,
    )


def message_record_to_schema(record: MessageRecord) -> Message:
    """Convert domain MessageRecord to public Message schema safely."""
    if record.role not in ("user", "assistant"):
        raise ValueError(f"Invalid message role: {record.role}")
    mode = getattr(record, "answer_mode", "normal")
    valid_modes = (
        "normal",
        "orientation",
        "architecture",
        "flow",
        "impact",
        "general_chat",
        "conversation",
        "static_guidance",
        "insufficient_orientation",
        "insufficient_evidence",
        "reindex_required",
    )
    if mode not in valid_modes:
        mode = "normal"
    return Message(
        message_id=record.message_id,
        role=record.role,  # type: ignore[arg-type]
        content=record.content,
        created_at=record.created_at,
        insufficient_evidence=record.insufficient_evidence,
        answer_mode=mode,  # type: ignore[arg-type]
        intent=getattr(record, "intent", None),
        confidence_bucket=getattr(record, "confidence_bucket", None),
        evidence_count=getattr(record, "evidence_count", None),
        hop_count=getattr(record, "hop_count", None),
        source_categories=list(getattr(record, "source_categories", ()) or ()),
        citations=[citation_record_to_schema(c) for c in record.citations],
        evidence=[evidence_record_to_schema(e) for e in record.evidence],
    )


def conversation_record_to_schema(
    record: ConversationRecord, messages: list[MessageRecord]
) -> ConversationDetailResponse:
    """Convert domain ConversationRecord and MessageRecords to public schema."""
    sorted_msgs = sorted(messages, key=lambda m: (m.created_at, m.message_id))
    return ConversationDetailResponse(
        conversation_id=record.conversation_id,
        repository_id=record.repository_id,
        title=record.title,
        created_at=record.created_at,
        updated_at=record.updated_at,
        messages=[message_record_to_schema(m) for m in sorted_msgs],
    )


MAX_QUESTION_LENGTH = 2000


def validate_question(question: Any) -> str:
    """Validate user input question for chat routes."""
    if isinstance(question, bool) or not isinstance(question, str):
        raise ValueError("Question must be a string.")
    cleaned = question.strip()
    if not cleaned:
        raise ValueError("Question cannot be empty.")
    if "\x00" in cleaned:
        raise ValueError("Question contains forbidden characters.")
    if len(cleaned) > MAX_QUESTION_LENGTH:
        raise ValueError("Question exceeds maximum length.")
    return cleaned
