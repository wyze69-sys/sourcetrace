"""Core domain records shared across SourceTrace layers."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AnonymousSession:
    """Anonymous browser session domain record."""

    owner_session_id: str
    last_active_at: datetime
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    active_repository_count: int = 0


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    """Metadata for an indexed public GitHub repository or uploaded ZIP archive."""

    repository_id: str
    owner_session_id: str
    name: str
    source_type: str
    status: str
    created_at: datetime
    updated_at: datetime
    github_url: str | None = None
    file_count: int = 0
    chunk_count: int = 0
    index_mode: str = "static"
    active_generation_id: str | None = None
    last_indexed_at: datetime | None = None
    indexed_commit_sha: str | None = None
    indexed_branch: str | None = None
    parser_versions: tuple[str, ...] = ()
    flow_evidence_complete: bool = False
    indexed_file_count: int = 0
    indexed_chunk_count: int = 0
    consecutive_refresh_failures: int = 0
    is_stale: bool | None = None
    stale_checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IndexingJobRecord:
    """Asynchronous indexing job status domain record."""

    job_id: str
    repository_id: str
    owner_session_id: str
    status: str
    current_step: str
    created_at: datetime
    updated_at: datetime
    progress_percentage: int = 0
    error_message: str | None = None
    completed_at: datetime | None = None
    job_type: str = "initial"


@dataclass(frozen=True, slots=True)
class CitationRecord:
    """Citation metadata for a cited code symbol and file location."""

    relative_path: str
    start_line: int
    end_line: int
    symbol_name: str
    symbol_type: str


@dataclass(frozen=True, slots=True)
class EvidenceSnippetRecord:
    """Evidence snippet content alongside citation metadata."""

    snippet: str
    relative_path: str
    start_line: int
    end_line: int
    symbol_name: str
    symbol_type: str


@dataclass(frozen=True, slots=True)
class ReferenceEvidence:
    """An outbound identifier reference inside a chunk, with exact source lines."""

    local_name: str
    kind: str  # "call" | "attribute_call"
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class ImportEvidence:
    """An import binding visible to a chunk, with exact source lines."""

    local_name: str
    source_module: str
    imported_name: str
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class EndpointEvidence:
    """An HTTP endpoint literal a chunk declares or calls, with exact source lines."""

    kind: str  # "declares" | "calls"
    http_method: str
    path_literal: str
    normalized_path: str
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class ParsedCodeChunk:
    """Parser output before embedding — internal model for deterministic AST parsing."""

    chunk_id: str
    repository_id: str
    owner_session_id: str
    relative_path: str
    language: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    content: str
    content_hash: str
    parser_version: str
    references: tuple[ReferenceEvidence, ...] = ()
    imports: tuple[ImportEvidence, ...] = ()
    endpoints: tuple[EndpointEvidence, ...] = ()
    extraction_truncated: bool = False


@dataclass(frozen=True, slots=True)
class CodeChunk:
    """Parsed AST code symbol chunk with citation metadata,

    optional normalization, and optional vector embedding.
    """

    chunk_id: str
    repository_id: str
    owner_session_id: str
    relative_path: str
    language: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    content: str
    content_hash: str
    parser_version: str
    created_at: datetime
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    embedding: tuple[float, ...] | None = None
    symbol_name_normalized: str = ""
    relative_path_normalized: str = ""
    search_terms: tuple[str, ...] = ()
    search_text: str = ""
    references: tuple[ReferenceEvidence, ...] = ()
    imports: tuple[ImportEvidence, ...] = ()
    endpoints: tuple[EndpointEvidence, ...] = ()
    extraction_truncated: bool = False
    generation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """Repository-scoped chat thread domain record."""

    conversation_id: str
    repository_id: str
    owner_session_id: str
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """Grounded user message or assistant answer domain record."""

    message_id: str
    conversation_id: str
    repository_id: str
    owner_session_id: str
    role: str
    content: str
    created_at: datetime
    citations: tuple[CitationRecord, ...] = field(default_factory=tuple)
    evidence: tuple[EvidenceSnippetRecord, ...] = field(default_factory=tuple)
    insufficient_evidence: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Retrieved source chunk and its relevance score."""

    chunk: CodeChunk
    score: float


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    """Immutable public evidence item containing citation, snippet, score, and chunk identity."""

    chunk_id: str
    score: float
    citation: CitationRecord
    snippet: EvidenceSnippetRecord


@dataclass(frozen=True, slots=True)
class GroundedEvidenceResult:
    """Immutable retrieval evidence output for grounded answer generation."""

    items: tuple[RetrievedEvidence, ...] = field(default_factory=tuple)
    total_retrieved: int = 0


@dataclass(frozen=True, slots=True)
class GroundedAnswerResult:
    """Immutable result from grounded answer generation including citations and evidence."""

    answer: str
    citations: tuple[CitationRecord, ...] = field(default_factory=tuple)
    evidence: tuple[EvidenceSnippetRecord, ...] = field(default_factory=tuple)
    insufficient_evidence: bool = False
    chunks_retrieved: int = 0



