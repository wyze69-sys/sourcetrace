"""Grounded answer generation service coordinating retrieval, prompts, and provider execution."""

from __future__ import annotations

import re
from collections.abc import Sequence

from sourcetrace.core.exceptions import (
    GenerationError,
    GenerationValidationError,
    RetrievalValidationError,
)
from sourcetrace.generation.client import GenerationMessage, GenerationProvider
from sourcetrace.generation.prompts import build_grounded_prompt
from sourcetrace.models.domain import (
    CitationRecord,
    EvidenceSnippetRecord,
    GroundedAnswerResult,
    RetrievedEvidence,
)
from sourcetrace.retrieval.service import SemanticRetrievalService

INSUFFICIENT_EVIDENCE_ANSWER = (
    "I do not have enough retrieved evidence from the indexed repository to answer this question."
)
MARKER_PATTERN = re.compile(r"\[E([1-9][0-9]*)\]")


class GroundedAnswerService:
    """Service coordinating retrieval, prompt building, LLM generation, and citations."""

    def __init__(
        self,
        retrieval_service: SemanticRetrievalService,
        generation_provider: GenerationProvider,
        *,
        max_answer_chars: int = 8000,
    ) -> None:
        self._retrieval_service = retrieval_service
        self._generation_provider = generation_provider

        if (
            type(max_answer_chars) is not int
            or type(max_answer_chars) is bool
            or max_answer_chars <= 0
        ):
            raise GenerationError("Generation failed safely.")
        self._max_answer_chars = max_answer_chars

    def generate_answer(
        self,
        owner_session_id: str,
        repository_id: str,
        question: str,
        *,
        limit: int = 5,
        conversation_context: Sequence[GenerationMessage] | None = None,
    ) -> GroundedAnswerResult:
        """Generate a grounded repository answer with server-controlled citation metadata."""
        # 1. Question & Scope Pre-validation
        if type(question) is not str or not question.strip():
            raise GenerationValidationError("Generation request is invalid.")

        if type(owner_session_id) is not str or not owner_session_id.strip():
            raise GenerationError("Generation failed safely.")

        if type(repository_id) is not str or not repository_id.strip():
            raise GenerationError("Generation failed safely.")

        # 2. Retrieve Grounded Evidence
        try:
            evidence_result = self._retrieval_service.retrieve(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                query=question,
                limit=limit,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except RetrievalValidationError:
            raise GenerationValidationError("Generation request is invalid.") from None
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        # 3. Handle No Evidence Retrieved Case
        if evidence_result.total_retrieved == 0 or not evidence_result.items:
            return GroundedAnswerResult(
                answer=INSUFFICIENT_EVIDENCE_ANSWER,
                citations=(),
                evidence=(),
                insufficient_evidence=True,
                chunks_retrieved=0,
            )

        # 4. Construct Grounded Prompt
        prompt_messages = build_grounded_prompt(
            question=question,
            evidence_items=evidence_result.items,
            conversation_context=conversation_context,
        )

        # 5. Call LLM Generation Provider
        try:
            raw_answer = self._generation_provider.generate(prompt_messages)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        # 6. Validate Generated Answer String
        if type(raw_answer) is not str or not raw_answer.strip():
            raise GenerationError("Generation failed safely.")

        clean_answer = raw_answer.strip()
        if "\x00" in clean_answer or len(clean_answer) > self._max_answer_chars:
            raise GenerationError("Generation failed safely.")

        # 7. Extract and Map Valid Evidence Markers
        matched_items = self._extract_valid_evidence_items(clean_answer, evidence_result.items)

        # 8. Citation Control Policy when no valid evidence marker is cited
        if not matched_items:
            return GroundedAnswerResult(
                answer=INSUFFICIENT_EVIDENCE_ANSWER,
                citations=(),
                evidence=(),
                insufficient_evidence=True,
                chunks_retrieved=evidence_result.total_retrieved,
            )

        citations: tuple[CitationRecord, ...] = tuple(item.citation for item in matched_items)
        evidence_snippets: tuple[EvidenceSnippetRecord, ...] = tuple(
            item.snippet for item in matched_items
        )

        return GroundedAnswerResult(
            answer=clean_answer,
            citations=citations,
            evidence=evidence_snippets,
            insufficient_evidence=False,
            chunks_retrieved=evidence_result.total_retrieved,
        )

    def _extract_valid_evidence_items(
        self, answer_text: str, evidence_items: tuple[RetrievedEvidence, ...]
    ) -> list[RetrievedEvidence]:
        raw_markers = MARKER_PATTERN.findall(answer_text)
        if not raw_markers:
            return []

        matched: list[RetrievedEvidence] = []
        seen_indices: set[int] = set()
        num_items = len(evidence_items)

        for m_str in raw_markers:
            try:
                idx = int(m_str) - 1
            except ValueError:
                continue

            if 0 <= idx < num_items and idx not in seen_indices:
                seen_indices.add(idx)
                matched.append(evidence_items[idx])

        return matched
