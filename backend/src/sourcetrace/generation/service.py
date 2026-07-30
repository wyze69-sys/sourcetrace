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

        # 3. Check Orientation Intent
        from sourcetrace.generation.prompts import _is_orientation_prompt_question

        is_orientation = _is_orientation_prompt_question(question)

        # 4. Handle No Evidence / Re-index Required Case
        if getattr(evidence_result, "reindex_required", False):
            return GroundedAnswerResult(
                answer=(
                    "Re-indexing required: No valid indexed source code chunks "
                    "were found for this repository. "
                    "Please re-index the repository to restore codebase intelligence."
                ),
                citations=(),
                evidence=(),
                insufficient_evidence=True,
                chunks_retrieved=0,
                answer_mode="reindex_required",
            )

        if evidence_result.total_retrieved == 0 or not evidence_result.items:
            if is_orientation:
                return GroundedAnswerResult(
                    answer=(
                        "SourceTrace could not verify a clear starting path "
                        "from indexed source files."
                    ),
                    citations=(),
                    evidence=(),
                    insufficient_evidence=True,
                    chunks_retrieved=0,
                    answer_mode="insufficient_orientation",
                )
            return GroundedAnswerResult(
                answer=INSUFFICIENT_EVIDENCE_ANSWER,
                citations=(),
                evidence=(),
                insufficient_evidence=True,
                chunks_retrieved=0,
                answer_mode="insufficient_evidence",
            )

        # 5. Construct Grounded Prompt
        prompt_messages = build_grounded_prompt(
            question=question,
            evidence_items=evidence_result.items,
            conversation_context=conversation_context,
        )

        # 6. Call LLM Generation Provider
        raw_answer: str | None = None
        try:
            raw_answer = self._generation_provider.generate(prompt_messages)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raw_answer = None

        # 7. Validate Generated Answer String
        is_valid_answer = (
            raw_answer is not None
            and type(raw_answer) is str
            and bool(raw_answer.strip())
            and "\x00" not in raw_answer
            and len(raw_answer.strip()) <= self._max_answer_chars
        )

        if not is_valid_answer:
            static_text, mode_name = self._build_static_guidance(
                is_orientation, evidence_result.items
            )
            top_citations = tuple(item.citation for item in evidence_result.items[:4])
            top_snippets = tuple(item.snippet for item in evidence_result.items[:4])
            return GroundedAnswerResult(
                answer=static_text,
                citations=top_citations,
                evidence=top_snippets,
                insufficient_evidence=False,
                chunks_retrieved=evidence_result.total_retrieved,
                answer_mode=mode_name,
            )

        clean_answer = raw_answer.strip()

        # 8. Extract and Map Valid Evidence Markers
        matched_items = self._extract_valid_evidence_items(clean_answer, evidence_result.items)

        # 9. Citation Relevance Control Policy
        relevant_matched_items = [
            item for item in matched_items if self._is_evidence_relevant_to_question(item, question)
        ]

        if not relevant_matched_items:
            if is_orientation:
                return GroundedAnswerResult(
                    answer=(
                        "SourceTrace could not verify a clear starting path "
                        "from indexed source files."
                    ),
                    citations=(),
                    evidence=(),
                    insufficient_evidence=True,
                    chunks_retrieved=evidence_result.total_retrieved,
                    answer_mode="insufficient_orientation",
                )
            return GroundedAnswerResult(
                answer=INSUFFICIENT_EVIDENCE_ANSWER,
                citations=(),
                evidence=(),
                insufficient_evidence=True,
                chunks_retrieved=evidence_result.total_retrieved,
                answer_mode="insufficient_evidence",
            )

        citations: tuple[CitationRecord, ...] = tuple(
            item.citation for item in relevant_matched_items
        )
        evidence_snippets: tuple[EvidenceSnippetRecord, ...] = tuple(
            item.snippet for item in relevant_matched_items
        )

        final_mode = "orientation" if is_orientation else "normal"

        return GroundedAnswerResult(
            answer=clean_answer,
            citations=citations,
            evidence=evidence_snippets,
            insufficient_evidence=False,
            chunks_retrieved=evidence_result.total_retrieved,
            answer_mode=final_mode,
        )

    def _is_evidence_relevant_to_question(self, item: RetrievedEvidence, question: str) -> bool:
        from sourcetrace.generation.prompts import _is_orientation_prompt_question
        from sourcetrace.storage.mongo_repositories import _ENGLISH_STOP_WORDS, tokenize_identifier

        if _is_orientation_prompt_question(question):
            return True

        q_tokens = [
            t.lower() for t in tokenize_identifier(question) if t.lower() not in _ENGLISH_STOP_WORDS
        ]
        if not q_tokens:
            return True

        text_to_check = (
            f"{item.citation.relative_path} {item.citation.symbol_name} {item.snippet.snippet}"
        ).lower()
        chunk_tokens = set(t.lower() for t in tokenize_identifier(text_to_check))

        q_token_set = set(q_tokens)
        if q_token_set.intersection(
            {
                "auth",
                "authentication",
                "login",
                "session",
                "jwt",
                "token",
                "password",
                "security",
                "signin",
                "authenticate",
                "bearer",
            }
        ):
            auth_terms = {
                "auth",
                "authentication",
                "authenticate",
                "authenticated",
                "authorize",
                "authorization",
                "authorized",
                "credential",
                "credentials",
                "token",
                "tokens",
                "session",
                "sessions",
                "jwt",
                "password",
                "passwords",
                "security",
                "signin",
                "signout",
                "login",
                "logout",
                "bearer",
                "basicauth",
                "digestauth",
                "oauth",
                "passport",
                "permission",
                "permissions",
                "guard",
                "authroutes",
                "authmiddleware",
            }
            return bool(chunk_tokens.intersection(auth_terms))

        if q_token_set.intersection(
            {
                "start",
                "started",
                "starting",
                "entry",
                "entrypoint",
                "main",
                "bootstrap",
                "launch",
                "run",
                "running",
                "begin",
                "app",
                "application",
                "startup",
            }
        ):
            startup_terms = {
                "start",
                "started",
                "starting",
                "entry",
                "entrypoint",
                "main",
                "bootstrap",
                "launch",
                "run",
                "running",
                "app",
                "application",
                "startup",
                "server",
                "index",
                "wsgi",
                "asgi",
                "listen",
                "express",
                "mount",
            }
            return bool(chunk_tokens.intersection(startup_terms))

        return True

    def _build_static_guidance(
        self, is_orientation: bool, evidence_items: tuple[RetrievedEvidence, ...]
    ) -> tuple[str, str]:
        if is_orientation:
            lines = ["Start here to explore this repository:\n"]
            for i, item in enumerate(evidence_items[:4], 1):
                rel_path = item.citation.relative_path
                symbol = item.citation.symbol_name
                symbol_type = item.citation.symbol_type
                sym_desc = f" ({symbol_type} {symbol})" if symbol and symbol != "module" else ""
                lines.append(f"{i}. Read `{rel_path}` [E{i}]: Key codebase definitions{sym_desc}.")
            first_path = evidence_items[0].citation.relative_path
            lines.append(
                f"\nNext action: Read `{first_path}` [E1] to trace entrypoint or primary logic."
            )
            return "\n".join(lines), "static_guidance"

        lines = ["Start with these retrieved source locations:\n"]
        for i, item in enumerate(evidence_items[:4], 1):
            rel_path = item.citation.relative_path
            symbol = item.citation.symbol_name
            s_line = item.citation.start_line
            e_line = item.citation.end_line
            lines.append(f"{i}. Inspect `{rel_path}` lines {s_line}-{e_line} [E{i}] ({symbol}).")
        first_path = evidence_items[0].citation.relative_path
        lines.append(f"\nNext action: Select `{first_path}` [E1] to view full source.")
        return "\n".join(lines), "static_guidance"

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
