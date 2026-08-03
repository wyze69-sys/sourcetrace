"""Grounded answer generation service coordinating retrieval, prompts, and provider execution."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from sourcetrace.core.exceptions import (
    GenerationError,
    GenerationValidationError,
    RetrievalValidationError,
)
from sourcetrace.generation.client import GenerationMessage, GenerationProvider
from sourcetrace.generation.planning import (
    MAX_ANSWER_CHARS,
    EvidenceBundle,
    IntentName,
    IntentRouter,
    answer_mode_for_intent,
    safe_planning_metadata,
)
from sourcetrace.generation.prompts import build_general_chat_prompt, build_grounded_prompt
from sourcetrace.models.domain import (
    GroundedAnswerResult,
    GroundedEvidenceResult,
    RetrievedEvidence,
)
from sourcetrace.retrieval.planned import PlannedRetrievalService
from sourcetrace.retrieval.service import SemanticRetrievalService

INSUFFICIENT_EVIDENCE_ANSWER = (
    "I do not have enough retrieved evidence from the indexed repository to answer this question."
)
OFF_TOPIC_SCOPE_ANSWER = (
    "I’m focused on answering questions about this repository. "
    "Try asking about a file, function, data flow, or change."
)
MARKER_PATTERN = re.compile(r"\[E([1-9][0-9]*)\]")
MARKER_CANDIDATE_PATTERN = re.compile(r"\[E[^\]\r\n]*(?:\]|$)")


class GroundedAnswerService:
    """Service coordinating retrieval, prompt building, LLM generation, and citations."""

    def __init__(
        self,
        retrieval_service: SemanticRetrievalService,
        generation_provider: GenerationProvider,
        *,
        max_answer_chars: int = MAX_ANSWER_CHARS,
        planned_retrieval_service: PlannedRetrievalService | None = None,
    ) -> None:
        self._retrieval_service = retrieval_service
        self._generation_provider = generation_provider
        self._planned_retrieval_service = planned_retrieval_service
        self._intent_router = IntentRouter()

        if (
            type(max_answer_chars) is not int
            or type(max_answer_chars) is bool
            or max_answer_chars <= 0
        ):
            raise GenerationError("Generation failed safely.")
        self._max_answer_chars = max_answer_chars

    def _generate_general_chat_answer(
        self,
        question: str,
        conversation_context: Sequence[GenerationMessage] | None,
    ) -> GroundedAnswerResult:
        """Answer a non-codebase question with the LLM in normal chat mode.

        Routed here when the message has no code signal or is clearly off-topic.
        No retrieval, no citations, no grounding — just a helpful conversation.
        Falls back to a scope hint if the provider is unavailable.
        """
        try:
            prompt = build_general_chat_prompt(
                question=question, conversation_context=conversation_context
            )
            raw = self._generation_provider.generate(prompt)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raw = None

        answer = (raw or "").strip()
        if not answer:
            answer = OFF_TOPIC_SCOPE_ANSWER

        return GroundedAnswerResult(
            answer=answer[: self._max_answer_chars],
            citations=(),
            evidence=(),
            insufficient_evidence=False,
            chunks_retrieved=0,
            answer_mode="general_chat",
        )

    def generate_answer(
        self,
        owner_session_id: str,
        repository_id: str,
        question: str,
        *,
        limit: int = 5,
        conversation_context: Sequence[GenerationMessage] | None = None,
    ) -> GroundedAnswerResult:
        """Route, plan, retrieve, synthesize, and validate one answer."""
        if type(question) is not str or not question.strip():
            raise GenerationValidationError("Generation request is invalid.")
        if type(owner_session_id) is not str or not owner_session_id.strip():
            raise GenerationError("Generation failed safely.")
        if type(repository_id) is not str or not repository_id.strip():
            raise GenerationError("Generation failed safely.")

        try:
            decision = self._intent_router.route(question, conversation_context)
        except ValueError:
            raise GenerationValidationError("Generation request is invalid.") from None
        classification = decision.classification
        bundle: EvidenceBundle | None = None

        def finish(result: GroundedAnswerResult) -> GroundedAnswerResult:
            metadata = safe_planning_metadata(classification, bundle)
            return replace(
                result,
                intent=str(metadata.get("intent", classification.primary_intent.value)),
                confidence_bucket=str(
                    metadata.get("confidence_bucket", classification.confidence_bucket)
                ),
                evidence_count=(
                    len(result.evidence)
                    if bundle is None
                    else int(metadata.get("evidence_count", len(result.evidence)))
                ),
                hop_count=int(metadata.get("hop_count", 0)),
                source_categories=tuple(
                    str(value) for value in metadata.get("source_categories", ())
                ),
            )

        if classification.is_acknowledgement:
            return finish(
                GroundedAnswerResult(
                    answer=(
                        "Glad that helps. Ask me about any file, function, flow, or change "
                        "when you are ready."
                    ),
                    answer_mode="conversation",
                )
            )
        if classification.is_general_chat:
            return finish(self._generate_general_chat_answer(question, conversation_context))

        try:
            if self._planned_retrieval_service is not None and decision.plan is not None:
                bundle = self._planned_retrieval_service.retrieve(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    plan=decision.plan,
                )
                evidence_result = GroundedEvidenceResult(
                    items=bundle.items,
                    total_retrieved=len(bundle.items),
                    reindex_required=bundle.reindex_required,
                )
            else:
                retrieval_query = self._build_retrieval_query(question, conversation_context)
                evidence_result = self._retrieval_service.retrieve(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    query=retrieval_query,
                    limit=limit,
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except RetrievalValidationError:
            raise GenerationValidationError("Generation request is invalid.") from None
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        from sourcetrace.generation.prompts import _is_orientation_prompt_question

        is_orientation = (
            classification.primary_intent == IntentName.REPOSITORY_OVERVIEW
            or _is_orientation_prompt_question(question)
        )
        if getattr(evidence_result, "reindex_required", False):
            return finish(
                GroundedAnswerResult(
                    answer=(
                        "Re-indexing required: No valid indexed source code chunks were found "
                        "for this repository. Please re-index the repository to restore "
                        "codebase intelligence."
                    ),
                    insufficient_evidence=True,
                    answer_mode="reindex_required",
                )
            )

        if evidence_result.total_retrieved == 0 or not evidence_result.items:
            if is_orientation:
                return finish(
                    GroundedAnswerResult(
                        answer=(
                            "SourceTrace could not verify a clear starting path from indexed "
                            "source files."
                        ),
                        insufficient_evidence=True,
                        answer_mode="insufficient_orientation",
                    )
                )
            return finish(
                GroundedAnswerResult(
                    answer=INSUFFICIENT_EVIDENCE_ANSWER,
                    insufficient_evidence=True,
                    answer_mode="insufficient_evidence",
                )
            )

        prompt_messages = build_grounded_prompt(
            question=question,
            evidence_items=evidence_result.items,
            conversation_context=conversation_context,
            intent=classification.primary_intent.value,
            sub_questions=decision.plan.sub_questions if decision.plan else (),
            evidence_bundle=bundle,
        )
        try:
            raw_answer = self._generation_provider.generate(prompt_messages)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raw_answer = None

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
            return finish(
                GroundedAnswerResult(
                    answer=static_text,
                    citations=tuple(item.citation for item in evidence_result.items[:4]),
                    evidence=tuple(item.snippet for item in evidence_result.items[:4]),
                    chunks_retrieved=evidence_result.total_retrieved,
                    answer_mode=mode_name,
                )
            )

        clean_answer = raw_answer.strip()
        if self._has_invalid_evidence_marker(clean_answer, len(evidence_result.items)):
            matched_items: list[RetrievedEvidence] = []
        else:
            matched_items = self._extract_valid_evidence_items(clean_answer, evidence_result.items)
        relevant_matched_items = [
            item for item in matched_items if self._is_evidence_relevant_to_question(item, question)
        ]
        if not relevant_matched_items:
            mode = "insufficient_orientation" if is_orientation else "insufficient_evidence"
            answer = (
                "SourceTrace could not verify a clear starting path from indexed source files."
                if is_orientation
                else INSUFFICIENT_EVIDENCE_ANSWER
            )
            return finish(
                GroundedAnswerResult(
                    answer=answer,
                    insufficient_evidence=True,
                    chunks_retrieved=evidence_result.total_retrieved,
                    answer_mode=mode,
                )
            )

        final_mode = (
            "orientation"
            if is_orientation
            else (
                answer_mode_for_intent(classification.primary_intent)
                if bundle is not None
                else "normal"
            )
        )
        return finish(
            GroundedAnswerResult(
                answer=clean_answer,
                citations=tuple(item.citation for item in relevant_matched_items),
                evidence=tuple(item.snippet for item in relevant_matched_items),
                chunks_retrieved=evidence_result.total_retrieved,
                answer_mode=final_mode,
            )
        )

    @staticmethod
    def _build_retrieval_query(
        question: str,
        conversation_context: Sequence[GenerationMessage] | None,
    ) -> str:
        """Resolve short follow-ups against the most recent conversation subject."""
        if not conversation_context:
            return question

        recent_context = [
            message.content.strip()
            for message in conversation_context[-4:]
            if isinstance(message, GenerationMessage)
            # Assistant answers can be a fallback such as "not enough
            # evidence" and are not reliable search subjects.  Prior user
            # questions contain the actual repository topic we need to carry
            # into a pronoun-only follow-up.
            and message.role == "user"
            and message.content.strip()
        ]
        if not recent_context:
            return question

        # Keep the current question prominent and cap history so retrieval remains
        # focused instead of turning the entire chat transcript into a search query.
        context_text = " ".join(recent_context)[-1800:]
        return f"{question.strip()} {context_text}".strip()

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

    @staticmethod
    def _has_invalid_evidence_marker(answer_text: str, evidence_count: int) -> bool:
        """Reject zero, out-of-range, and otherwise untrusted evidence markers."""
        for candidate in MARKER_CANDIDATE_PATTERN.findall(answer_text):
            if not candidate.endswith("]"):
                return True
            raw_index = candidate[2:-1]
            if not raw_index.isdigit():
                return True
            index = int(raw_index)
            if index < 1 or index > evidence_count:
                return True
        return False
