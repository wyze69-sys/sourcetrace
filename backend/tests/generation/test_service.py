"""Offline unit tests for GroundedAnswerService."""

from collections.abc import Sequence
from typing import Any

import pytest

from sourcetrace.core.exceptions import (
    GenerationError,
    GenerationValidationError,
    RetrievalError,
    RetrievalValidationError,
)
from sourcetrace.generation.client import GenerationMessage
from sourcetrace.generation.service import (
    INSUFFICIENT_EVIDENCE_ANSWER,
    GroundedAnswerService,
)
from sourcetrace.models.domain import (
    CitationRecord,
    EvidenceSnippetRecord,
    GroundedEvidenceResult,
    RetrievedEvidence,
)


class FakeRetrievalService:
    """Offline fake retrieval service for generation orchestration testing."""

    def __init__(self, result: GroundedEvidenceResult | Exception | None = None) -> None:
        self.result = (
            result
            if result is not None
            else GroundedEvidenceResult(items=(), total_retrieved=0)
        )
        self.retrieve_calls: list[dict[str, Any]] = []

    def retrieve(
        self,
        owner_session_id: str,
        repository_id: str,
        query: str,
        *,
        limit: int = 5,
    ) -> GroundedEvidenceResult:
        self.retrieve_calls.append(
            {
                "owner_session_id": owner_session_id,
                "repository_id": repository_id,
                "query": query,
                "limit": limit,
            }
        )
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeGenerationProvider:
    """Offline fake generation provider for answer service testing."""

    def __init__(self, answer: str | BaseException = "Generated answer [E1]") -> None:
        self.answer = answer
        self.generate_calls: list[tuple[GenerationMessage, ...]] = []

    @property
    def model_identifier(self) -> str:
        return "fake-llm-model"

    def generate(self, messages: Sequence[GenerationMessage]) -> str:
        self.generate_calls.append(tuple(messages))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _make_evidence(
    chunk_id: str = "chunk_001",
    score: float = 0.9,
    relative_path: str = "sourcetrace/core/config.py",
    start_line: int = 10,
    end_line: int = 40,
    symbol_name: str = "Settings",
    symbol_type: str = "class",
    snippet_content: str = "class Settings:\n    pass",
) -> RetrievedEvidence:
    citation = CitationRecord(
        relative_path=relative_path,
        start_line=start_line,
        end_line=end_line,
        symbol_name=symbol_name,
        symbol_type=symbol_type,
    )
    snippet = EvidenceSnippetRecord(
        snippet=snippet_content,
        relative_path=relative_path,
        start_line=start_line,
        end_line=end_line,
        symbol_name=symbol_name,
        symbol_type=symbol_type,
    )
    return RetrievedEvidence(
        chunk_id=chunk_id,
        score=score,
        citation=citation,
        snippet=snippet,
    )


# ---------------------------------------------------------------------------
# 1. No Evidence Behavior Tests
# ---------------------------------------------------------------------------


def test_no_evidence_does_not_call_generation_provider() -> None:
    retrieval_service = FakeRetrievalService(
        GroundedEvidenceResult(items=(), total_retrieved=0)
    )
    provider = FakeGenerationProvider()
    service = GroundedAnswerService(retrieval_service, provider)

    res = service.generate_answer("sess_001", "repo_001", "How does config work?")

    assert res.insufficient_evidence is True
    assert res.answer == INSUFFICIENT_EVIDENCE_ANSWER
    assert res.citations == ()
    assert res.evidence == ()

    # Retrieval service called with exact arguments
    assert len(retrieval_service.retrieve_calls) == 1
    call = retrieval_service.retrieve_calls[0]
    assert call["owner_session_id"] == "sess_001"
    assert call["repository_id"] == "repo_001"
    assert call["query"] == "How does config work?"

    # Provider MUST NOT be called when 0 evidence is retrieved
    assert len(provider.generate_calls) == 0


# ---------------------------------------------------------------------------
# 2. Grounded Answer & Citation Control Tests
# ---------------------------------------------------------------------------


def test_valid_answer_with_valid_citation_marker() -> None:
    ev1 = _make_evidence(chunk_id="c1", relative_path="sourcetrace/core/config.py")
    ev2 = _make_evidence(chunk_id="c2", relative_path="sourcetrace/core/security.py")
    retrieval_result = GroundedEvidenceResult(items=(ev1, ev2), total_retrieved=2)

    retrieval_service = FakeRetrievalService(retrieval_result)
    provider = FakeGenerationProvider(
        "Config is loaded in sourcetrace/core/config.py [E1]."
    )
    service = GroundedAnswerService(retrieval_service, provider)

    res = service.generate_answer("sess_001", "repo_001", "How does config work?")

    assert res.insufficient_evidence is False
    assert res.answer == "Config is loaded in sourcetrace/core/config.py [E1]."
    assert len(res.citations) == 1
    assert res.citations[0].relative_path == "sourcetrace/core/config.py"
    assert len(res.evidence) == 1
    assert res.evidence[0].relative_path == "sourcetrace/core/config.py"

    assert len(provider.generate_calls) == 1


def test_duplicate_citation_markers_deduplicated() -> None:
    ev1 = _make_evidence(chunk_id="c1", relative_path="a.py")
    retrieval_result = GroundedEvidenceResult(items=(ev1,), total_retrieved=1)

    retrieval_service = FakeRetrievalService(retrieval_result)
    provider = FakeGenerationProvider("Explanation [E1] and more explanation [E1].")
    service = GroundedAnswerService(retrieval_service, provider)

    res = service.generate_answer("sess_001", "repo_001", "Question")

    assert res.insufficient_evidence is False
    assert len(res.citations) == 1
    assert res.citations[0].relative_path == "a.py"


def test_unknown_evidence_marker_returns_insufficient_evidence() -> None:
    ev1 = _make_evidence(chunk_id="c1", relative_path="a.py")
    retrieval_result = GroundedEvidenceResult(items=(ev1,), total_retrieved=1)

    retrieval_service = FakeRetrievalService(retrieval_result)
    # Model generates [E999] which refers to no retrieved evidence
    provider = FakeGenerationProvider("Answer referring to [E999].")
    service = GroundedAnswerService(retrieval_service, provider)

    res = service.generate_answer("sess_001", "repo_001", "Question")

    assert res.insufficient_evidence is True
    assert res.answer == INSUFFICIENT_EVIDENCE_ANSWER
    assert res.citations == ()
    assert res.evidence == ()


def test_no_evidence_marker_in_generated_answer() -> None:
    ev1 = _make_evidence(chunk_id="c1", relative_path="a.py")
    retrieval_result = GroundedEvidenceResult(items=(ev1,), total_retrieved=1)

    retrieval_service = FakeRetrievalService(retrieval_result)
    # Model generates an uncited answer with no [E1] marker
    provider = FakeGenerationProvider("An uncited general answer.")
    service = GroundedAnswerService(retrieval_service, provider)

    res = service.generate_answer("sess_001", "repo_001", "Question")

    assert res.insufficient_evidence is True
    assert res.answer == INSUFFICIENT_EVIDENCE_ANSWER
    assert res.citations == ()


def test_model_invented_paths_ignored_server_metadata_used() -> None:
    ev1 = _make_evidence(
        chunk_id="c1",
        relative_path="real/path.py",
        start_line=10,
        end_line=20,
    )
    retrieval_result = GroundedEvidenceResult(items=(ev1,), total_retrieved=1)

    retrieval_service = FakeRetrievalService(retrieval_result)
    # Model text claims fake/path.py:100-200 but cites [E1]
    provider = FakeGenerationProvider("Found in fake/path.py lines 100-200 [E1].")
    service = GroundedAnswerService(retrieval_service, provider)

    res = service.generate_answer("sess_001", "repo_001", "Question")

    assert res.insufficient_evidence is False
    assert len(res.citations) == 1
    # Server metadata is enforced regardless of model claim in text
    assert res.citations[0].relative_path == "real/path.py"
    assert res.citations[0].start_line == 10
    assert res.citations[0].end_line == 20


# ---------------------------------------------------------------------------
# 3. Input & Validation Error Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_question",
    [None, 123, True, "", "   "],
)
def test_invalid_question_validation(invalid_question: Any) -> None:
    retrieval_service = FakeRetrievalService()
    provider = FakeGenerationProvider()
    service = GroundedAnswerService(retrieval_service, provider)

    with pytest.raises(GenerationValidationError) as exc_info:
        service.generate_answer("sess_001", "repo_001", invalid_question)

    assert str(exc_info.value) == "Generation request is invalid."


def test_retrieval_validation_error_conversion() -> None:
    err = RetrievalValidationError("Retrieval request is invalid.")
    retrieval_service = FakeRetrievalService(err)

    provider = FakeGenerationProvider()
    service = GroundedAnswerService(retrieval_service, provider)

    with pytest.raises(GenerationValidationError) as exc_info:
        service.generate_answer("sess_001", "repo_001", "Question")

    assert str(exc_info.value) == "Generation request is invalid."


def test_retrieval_error_conversion() -> None:
    retrieval_service = FakeRetrievalService(RetrievalError("Retrieval failed safely."))
    provider = FakeGenerationProvider()
    service = GroundedAnswerService(retrieval_service, provider)

    with pytest.raises(GenerationError) as exc_info:
        service.generate_answer("sess_001", "repo_001", "Question")

    assert str(exc_info.value) == "Generation failed safely."


def test_provider_failure_conversion() -> None:
    ev = _make_evidence()
    retrieval_service = FakeRetrievalService(GroundedEvidenceResult(items=(ev,), total_retrieved=1))
    provider = FakeGenerationProvider(RuntimeError("API Secret Key Leak"))
    service = GroundedAnswerService(retrieval_service, provider)

    with pytest.raises(GenerationError) as exc_info:
        service.generate_answer("sess_001", "repo_001", "Question")

    assert str(exc_info.value) == "Generation failed safely."
    assert "Secret Key" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Process Control Pass-Through Tests
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_pass_through_from_retrieval() -> None:
    retrieval_service = FakeRetrievalService(KeyboardInterrupt())
    provider = FakeGenerationProvider()
    service = GroundedAnswerService(retrieval_service, provider)

    with pytest.raises(KeyboardInterrupt):
        service.generate_answer("sess_001", "repo_001", "Question")


def test_system_exit_pass_through_from_provider() -> None:
    ev = _make_evidence()
    retrieval_service = FakeRetrievalService(GroundedEvidenceResult(items=(ev,), total_retrieved=1))
    provider = FakeGenerationProvider(SystemExit(1))
    service = GroundedAnswerService(retrieval_service, provider)

    with pytest.raises(SystemExit):
        service.generate_answer("sess_001", "repo_001", "Question")
