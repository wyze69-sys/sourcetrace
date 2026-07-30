"""Offline unit and integration tests for AI-CHAT-001 static-evidence chat fallback."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sourcetrace.api.dependencies import get_semantic_retrieval_service
from sourcetrace.core.config import Settings
from sourcetrace.generation.client import GenerationMessage
from sourcetrace.generation.service import (
    INSUFFICIENT_EVIDENCE_ANSWER,
    GroundedAnswerService,
)
from sourcetrace.models.domain import (
    CodeChunk,
    RepositoryRecord,
    RetrievalResult,
)
from sourcetrace.retrieval.service import SemanticRetrievalService

NOW = datetime.now(UTC)


class MockRepositoryRepository:
    """Mock repository repo returning an active ready repository."""

    def __init__(self, repo_record: RepositoryRecord | None = None) -> None:
        self._repo = repo_record or RepositoryRecord(
            repository_id="repo_001",
            owner_session_id="sess_001",
            name="TestRepo",
            source_type="github",
            status="ready",
            created_at=NOW,
            updated_at=NOW,
            file_count=10,
            chunk_count=50,
            index_mode="static",
            active_generation_id="gen_v1",
        )

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        if (
            self._repo.owner_session_id == owner_session_id
            and self._repo.repository_id == repository_id
        ):
            return self._repo
        return None


class MockCodeChunkRepository:
    """Mock chunk repo recording search_lexical and search_vectors calls."""

    def __init__(self, lexical_results: list[RetrievalResult] | None = None) -> None:
        self.lexical_results = lexical_results or []
        self.search_lexical_calls: list[dict[str, Any]] = []
        self.search_vectors_calls: list[dict[str, Any]] = []

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]:
        self.search_lexical_calls.append(
            {
                "owner_session_id": owner_session_id,
                "repository_id": repository_id,
                "query_text": query_text,
                "limit": limit,
                "generation_id": generation_id,
            }
        )
        return self.lexical_results

    def search_vectors(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: list[float],
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]:
        self.search_vectors_calls.append(
            {
                "owner_session_id": owner_session_id,
                "repository_id": repository_id,
                "query_vector": query_vector,
                "limit": limit,
                "generation_id": generation_id,
            }
        )
        return []


class MockFailingLLMProvider:
    """Mock LLM provider that raises an exception when generate() is called."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("OpenAI API Connection Failed")

    @property
    def model_identifier(self) -> str:
        return "mock-failing-llm"

    def generate(self, messages: Sequence[GenerationMessage]) -> str:
        raise self.exc


class MockWorkingLLMProvider:
    """Mock LLM provider returning cited answer."""

    def __init__(
        self,
        answer: str = "Based on static analysis, the user model is in models/user.py [E1].",
    ) -> None:
        self.answer = answer
        self.generate_calls: list[Any] = []

    @property
    def model_identifier(self) -> str:
        return "mock-working-llm"

    def generate(self, messages: Sequence[GenerationMessage]) -> str:
        self.generate_calls.append(messages)
        return self.answer


def _make_retrieval_result(
    chunk_id: str = "chunk_101",
    relative_path: str = "src/user.py",
    symbol_name: str = "getUser",
    symbol_type: str = "function",
    content: str = "def getUser(id):\n    return db.find(id)",
    start_line: int = 5,
    end_line: int = 10,
    score: float = 1.0,
    owner_session_id: str = "sess_001",
    repository_id: str = "repo_001",
    generation_id: str = "gen_v1",
) -> RetrievalResult:
    chunk = CodeChunk(
        chunk_id=chunk_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        relative_path=relative_path,
        language="python",
        symbol_name=symbol_name,
        symbol_type=symbol_type,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash="hash123",
        parser_version="python-ast-v3",
        created_at=NOW,
        generation_id=generation_id,
    )
    return RetrievalResult(chunk=chunk, score=score)


# ---------------------------------------------------------------------------
# 1. Dependency Provider Tests
# ---------------------------------------------------------------------------


def test_lexical_fallback_does_not_construct_embedding_adapter() -> None:
    """When semantic_search_available is False, returns service with embedding_provider=None."""
    settings = Settings(
        gemini_api_key=None,
        embedding_api_key=None,
        llm_api_key="sk-fake-key-for-test-long-enough-string",
        llm_provider="openai",
        embedding_provider="gemini",
    )
    repo_repo = MockRepositoryRepository()
    chunk_repo = MockCodeChunkRepository()

    svc = get_semantic_retrieval_service(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        settings=settings,
    )

    assert isinstance(svc, SemanticRetrievalService)
    assert svc._embedding_provider is None


def test_semantic_mode_constructed_when_embeddings_configured() -> None:
    """When embeddings key is configured, constructs the embedding provider adapter."""
    settings = Settings(
        gemini_api_key="AIzaSyFakeGeminiKeyForTest1234567",
        llm_provider="gemini",
        embedding_provider="gemini",
    )
    repo_repo = MockRepositoryRepository()
    chunk_repo = MockCodeChunkRepository()

    svc = get_semantic_retrieval_service(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        settings=settings,
    )

    assert isinstance(svc, SemanticRetrievalService)
    assert svc._embedding_provider is not None
    assert getattr(svc._embedding_provider, "model_identifier", "") == "gemini-embedding-001"


# ---------------------------------------------------------------------------
# 2. Retrieval Scoping & Lexical Execution Tests
# ---------------------------------------------------------------------------


def test_lexical_fallback_preserves_owner_repo_generation_scoping() -> None:
    """Retrieving without embedding provider calls search_lexical with correct scoping."""
    res1 = _make_retrieval_result()
    repo_repo = MockRepositoryRepository()
    chunk_repo = MockCodeChunkRepository(lexical_results=[res1])

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    result = retrieval_svc.retrieve(
        owner_session_id="sess_001",
        repository_id="repo_001",
        query="getUser",
        limit=5,
    )

    assert result.total_retrieved == 1
    assert len(chunk_repo.search_lexical_calls) == 1
    call = chunk_repo.search_lexical_calls[0]
    assert call["owner_session_id"] == "sess_001"
    assert call["repository_id"] == "repo_001"
    assert call["query_text"] == "getUser"
    assert call["generation_id"] == "gen_v1"
    assert len(chunk_repo.search_vectors_calls) == 0


# ---------------------------------------------------------------------------
# 3. Grounded Answer Fallback Tests
# ---------------------------------------------------------------------------


def test_lexical_fallback_returns_grounded_citations() -> None:
    """Static evidence lexical chat returns normal grounded citations when LLM succeeds."""
    res1 = _make_retrieval_result()
    repo_repo = MockRepositoryRepository()
    chunk_repo = MockCodeChunkRepository(lexical_results=[res1])

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )
    llm = MockWorkingLLMProvider("Found getUser function in src/user.py [E1].")

    answer_svc = GroundedAnswerService(
        retrieval_service=retrieval_svc,
        generation_provider=llm,
    )

    res = answer_svc.generate_answer(
        owner_session_id="sess_001",
        repository_id="repo_001",
        question="Where is getUser defined?",
    )

    assert res.insufficient_evidence is False
    assert res.answer == "Found getUser function in src/user.py [E1]."
    assert len(res.citations) == 1
    assert res.citations[0].relative_path == "src/user.py"
    assert res.citations[0].symbol_name == "getUser"


def test_lexical_fallback_no_evidence_returns_truthful_response() -> None:
    """When 0 lexical chunks match, returns insufficient_evidence=True without calling LLM."""
    repo_repo = MockRepositoryRepository()
    chunk_repo = MockCodeChunkRepository(lexical_results=[])

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )
    llm = MockWorkingLLMProvider()

    answer_svc = GroundedAnswerService(
        retrieval_service=retrieval_svc,
        generation_provider=llm,
    )

    res = answer_svc.generate_answer(
        owner_session_id="sess_001",
        repository_id="repo_001",
        question="Nonexistent symbol search query",
    )

    assert res.insufficient_evidence is True
    assert res.answer == INSUFFICIENT_EVIDENCE_ANSWER
    assert res.citations == ()
    assert res.evidence == ()
    assert len(llm.generate_calls) == 0


def test_lexical_fallback_llm_failure_returns_static_evidence_safely() -> None:
    """When LLM provider fails, returns retrieved static evidence with safe notice."""
    res1 = _make_retrieval_result(chunk_id="c1", relative_path="src/a.py", symbol_name="fnA")
    res2 = _make_retrieval_result(chunk_id="c2", relative_path="src/b.py", symbol_name="fnB")
    repo_repo = MockRepositoryRepository()
    chunk_repo = MockCodeChunkRepository(lexical_results=[res1, res2])

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )
    failing_llm = MockFailingLLMProvider(RuntimeError("HTTP 500 Connection Refused by Provider"))

    answer_svc = GroundedAnswerService(
        retrieval_service=retrieval_svc,
        generation_provider=failing_llm,
    )

    res = answer_svc.generate_answer(
        owner_session_id="sess_001",
        repository_id="repo_001",
        question="Explain system architecture",
    )

    assert res.insufficient_evidence is False
    assert "Start with these retrieved source locations:" in res.answer
    assert res.answer_mode == "static_guidance"
    assert len(res.citations) == 2
    assert res.citations[0].relative_path == "src/a.py"
    assert res.citations[1].relative_path == "src/b.py"
    assert len(res.evidence) == 2
    assert res.chunks_retrieved == 2
