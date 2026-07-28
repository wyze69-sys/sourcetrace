"""Provider-neutral semantic code retrieval service."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Sequence
from typing import Any

from sourcetrace.core.exceptions import (
    RetrievalError,
    RetrievalValidationError,
)
from sourcetrace.embeddings.provider import EmbeddingProvider
from sourcetrace.models.domain import (
    CitationRecord,
    CodeChunk,
    EvidenceSnippetRecord,
    GroundedEvidenceResult,
    RepositoryRecord,
    RetrievalResult,
    RetrievedEvidence,
)
from sourcetrace.storage.repositories import (
    CodeChunkRepository,
    RepositoryRepository,
)


class SemanticRetrievalService:
    """Injectable provider-neutral semantic code retrieval service."""

    def __init__(
        self,
        repository_repo: RepositoryRepository,
        code_chunk_repo: CodeChunkRepository,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        max_evidence_results: int = 5,
        max_snippet_chars: int = 2000,
        max_total_evidence_chars: int = 10000,
        vector_search_max_limit: int = 50,
    ) -> None:
        self._repository_repo = repository_repo
        self._code_chunk_repo = code_chunk_repo
        self._embedding_provider = embedding_provider

        if (
            type(max_evidence_results) is not int
            or type(max_evidence_results) is bool
            or max_evidence_results <= 0
        ):
            raise RetrievalError("Retrieval failed safely.")
        self._max_evidence_results = max_evidence_results

        if (
            type(max_snippet_chars) is not int
            or type(max_snippet_chars) is bool
            or max_snippet_chars <= 0
        ):
            raise RetrievalError("Retrieval failed safely.")
        self._max_snippet_chars = max_snippet_chars

        if (
            type(max_total_evidence_chars) is not int
            or type(max_total_evidence_chars) is bool
            or max_total_evidence_chars <= 0
        ):
            raise RetrievalError("Retrieval failed safely.")
        self._max_total_evidence_chars = max_total_evidence_chars

        if (
            type(vector_search_max_limit) is not int
            or type(vector_search_max_limit) is bool
            or vector_search_max_limit <= 0
        ):
            raise RetrievalError("Retrieval failed safely.")
        self._vector_search_max_limit = vector_search_max_limit

    def retrieve(
        self,
        owner_session_id: str,
        repository_id: str,
        query: str,
        *,
        limit: int = 5,
    ) -> GroundedEvidenceResult:
        """Retrieve grounded code evidence for an owner-scoped ready repository."""
        # 1. Query Validation
        query_text = self._validate_query(query)

        # 2. Parameter & Scope Pre-checks
        valid_limit = self._validate_limit(limit)
        self._validate_identifiers(owner_session_id, repository_id)

        # 3. Repository Readiness Validation
        repo_record = self._validate_repository_readiness(owner_session_id, repository_id)
        active_gen = getattr(repo_record, "active_generation_id", None)

        # 4. Embed Query or Lexical Search
        is_static = (
            self._embedding_provider is None
            or getattr(self._embedding_provider, "embedding_dimensions", -1) == 0
            or getattr(self._embedding_provider, "model_identifier", "") == "none"
        )

        if is_static:
            if hasattr(self._code_chunk_repo, "search_lexical"):
                search_results = self._code_chunk_repo.search_lexical(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    query_text=query_text,
                    limit=valid_limit,
                    generation_id=active_gen,
                )
            else:
                search_results = []
        else:
            query_vector = self._embed_query(query_text)
            search_results = self._execute_vector_search(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                query_vector=query_vector,
                limit=valid_limit,
                generation_id=active_gen,
            )
            if not search_results and hasattr(self._code_chunk_repo, "search_lexical"):
                search_results = self._code_chunk_repo.search_lexical(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    query_text=query_text,
                    limit=valid_limit,
                    generation_id=active_gen,
                )

        if not search_results:
            return GroundedEvidenceResult(items=(), total_retrieved=0)

        # 6. Validate Search Results
        validated_results = self._validate_search_results(
            results=search_results,
            expected_owner_session_id=owner_session_id,
            expected_repository_id=repository_id,
        )

        # 7. Deterministic Ranking & Evidence Construction
        return self._construct_grounded_evidence(validated_results)

    def _validate_query(self, query: Any) -> str:
        if type(query) is not str:
            raise RetrievalValidationError("Retrieval request is invalid.")

        if len(query) > 4000:
            raise RetrievalValidationError("Retrieval request is invalid.")

        if "\x00" in query:
            raise RetrievalValidationError("Retrieval request is invalid.")

        for char in query:
            cat = unicodedata.category(char)
            if cat.startswith("C") and char not in ("\n", "\r", "\t"):
                raise RetrievalValidationError("Retrieval request is invalid.")
            code = ord(char)
            if code < 32 and char not in ("\n", "\r", "\t"):
                raise RetrievalValidationError("Retrieval request is invalid.")
            if code == 127:
                raise RetrievalValidationError("Retrieval request is invalid.")

        clean_query = query.strip()
        if not clean_query:
            raise RetrievalValidationError("Retrieval request is invalid.")

        return clean_query

    def _validate_limit(self, limit: Any) -> int:
        if type(limit) is not int or type(limit) is bool:
            raise RetrievalValidationError("Retrieval request is invalid.")

        if limit < 1 or limit > self._vector_search_max_limit:
            raise RetrievalValidationError("Retrieval request is invalid.")

        return limit

    def _validate_identifiers(self, owner_session_id: Any, repository_id: Any) -> None:
        if type(owner_session_id) is not str or not owner_session_id.strip():
            raise RetrievalError("Retrieval failed safely.")
        if type(repository_id) is not str or not repository_id.strip():
            raise RetrievalError("Retrieval failed safely.")

    def _validate_repository_readiness(
        self, owner_session_id: str, repository_id: str
    ) -> RepositoryRecord:
        try:
            repo_record = self._repository_repo.get_by_id(owner_session_id, repository_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RetrievalError("Retrieval failed safely.") from None

        if type(repo_record) is not RepositoryRecord:
            raise RetrievalError("Retrieval failed safely.")

        if repo_record.owner_session_id != owner_session_id:
            raise RetrievalError("Retrieval failed safely.")

        if repo_record.repository_id != repository_id:
            raise RetrievalError("Retrieval failed safely.")

        if repo_record.status != "ready":
            raise RetrievalError("Retrieval failed safely.")

        return repo_record

    def _embed_query(self, query_text: str) -> tuple[float, ...]:
        try:
            model_id = getattr(self._embedding_provider, "model_identifier", None)
            expected_dim = getattr(self._embedding_provider, "embedding_dimensions", None)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RetrievalError("Retrieval failed safely.") from None

        if type(model_id) is not str or not model_id.strip():
            raise RetrievalError("Retrieval failed safely.")

        if type(expected_dim) is not int or type(expected_dim) is bool or expected_dim <= 0:
            raise RetrievalError("Retrieval failed safely.")

        try:
            raw_vectors = self._embedding_provider.embed((query_text,))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RetrievalError("Retrieval failed safely.") from None

        if type(raw_vectors) is not tuple or len(raw_vectors) != 1:
            raise RetrievalError("Retrieval failed safely.")

        vec = raw_vectors[0]
        if type(vec) is not tuple or len(vec) != expected_dim:
            raise RetrievalError("Retrieval failed safely.")

        float_vector: list[float] = []
        for val in vec:
            if (
                val is None
                or type(val) is bool
                or type(val) not in (int, float)
                or math.isnan(val)
                or math.isinf(val)
            ):
                raise RetrievalError("Retrieval failed safely.")
            float_vector.append(float(val))

        return tuple(float_vector)

    def _execute_vector_search(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: tuple[float, ...],
        limit: int,
        generation_id: str | None = None,
    ) -> Sequence[Any]:
        try:
            results = self._code_chunk_repo.search_vectors(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                query_vector=list(query_vector),
                limit=limit,
                generation_id=generation_id,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RetrievalError("Retrieval failed safely.") from None

        if type(results) is not list and type(results) is not tuple:
            raise RetrievalError("Retrieval failed safely.")

        return results

    def _validate_search_results(
        self,
        results: Sequence[Any],
        expected_owner_session_id: str,
        expected_repository_id: str,
    ) -> list[RetrievalResult]:
        validated_results: list[RetrievalResult] = []
        seen_chunk_ids: set[str] = set()

        for item in results:
            if type(item) is not RetrievalResult:
                raise RetrievalError("Retrieval failed safely.")

            score = item.score
            if (
                score is None
                or type(score) is bool
                or type(score) not in (int, float)
                or math.isnan(score)
                or math.isinf(score)
            ):
                raise RetrievalError("Retrieval failed safely.")

            chunk = item.chunk
            if type(chunk) is not CodeChunk:
                raise RetrievalError("Retrieval failed safely.")

            if chunk.owner_session_id != expected_owner_session_id:
                raise RetrievalError("Retrieval failed safely.")

            if chunk.repository_id != expected_repository_id:
                raise RetrievalError("Retrieval failed safely.")

            chunk_id = chunk.chunk_id
            if type(chunk_id) is not str or not chunk_id.strip():
                raise RetrievalError("Retrieval failed safely.")

            if chunk_id in seen_chunk_ids:
                raise RetrievalError("Retrieval failed safely.")
            seen_chunk_ids.add(chunk_id)

            path = chunk.relative_path
            if (
                type(path) is not str
                or not path.strip()
                or path.startswith("/")
                or path.startswith("\\")
                or ":" in path
            ):
                raise RetrievalError("Retrieval failed safely.")

            norm_parts = path.replace("\\", "/").split("/")
            if any(p in ("", ".", "..") for p in norm_parts):
                raise RetrievalError("Retrieval failed safely.")

            content = chunk.content
            if type(content) is not str or not content:
                raise RetrievalError("Retrieval failed safely.")

            start_line = chunk.start_line
            end_line = chunk.end_line
            if type(start_line) is not int or type(start_line) is bool or start_line < 1:
                raise RetrievalError("Retrieval failed safely.")

            if type(end_line) is not int or type(end_line) is bool or end_line < start_line:
                raise RetrievalError("Retrieval failed safely.")

            sym_name = chunk.symbol_name
            sym_type = chunk.symbol_type
            if (
                type(sym_name) is not str
                or not sym_name.strip()
                or type(sym_type) is not str
                or not sym_type.strip()
            ):
                raise RetrievalError("Retrieval failed safely.")

            validated_results.append(item)

        return validated_results

    def _construct_grounded_evidence(
        self, results: list[RetrievalResult]
    ) -> GroundedEvidenceResult:
        sorted_results = sorted(
            results,
            key=lambda r: (
                -float(r.score),
                r.chunk.relative_path,
                r.chunk.start_line,
                r.chunk.end_line,
                r.chunk.chunk_id,
            ),
        )

        evidence_items: list[RetrievedEvidence] = []
        accumulated_chars = 0

        for res in sorted_results[: self._max_evidence_results]:
            raw_content = res.chunk.content

            # Truncate single snippet if larger than max_snippet_chars
            if len(raw_content) > self._max_snippet_chars:
                marker = "\n[truncated...]"
                keep_len = max(0, self._max_snippet_chars - len(marker))
                snippet_text = raw_content[:keep_len] + marker
            else:
                snippet_text = raw_content

            # Enforce total evidence character limit across snippets
            remaining_budget = self._max_total_evidence_chars - accumulated_chars
            if remaining_budget <= 0:
                break

            if len(snippet_text) > remaining_budget:
                marker = "\n[truncated...]"
                keep_len = max(0, remaining_budget - len(marker))
                snippet_text = snippet_text[:keep_len] + marker

            if not snippet_text:
                break

            accumulated_chars += len(snippet_text)

            citation = CitationRecord(
                relative_path=res.chunk.relative_path,
                start_line=res.chunk.start_line,
                end_line=res.chunk.end_line,
                symbol_name=res.chunk.symbol_name,
                symbol_type=res.chunk.symbol_type,
            )
            snippet_record = EvidenceSnippetRecord(
                snippet=snippet_text,
                relative_path=res.chunk.relative_path,
                start_line=res.chunk.start_line,
                end_line=res.chunk.end_line,
                symbol_name=res.chunk.symbol_name,
                symbol_type=res.chunk.symbol_type,
            )
            item = RetrievedEvidence(
                chunk_id=res.chunk.chunk_id,
                score=float(res.score),
                citation=citation,
                snippet=snippet_record,
            )
            evidence_items.append(item)

        return GroundedEvidenceResult(
            items=tuple(evidence_items),
            total_retrieved=len(evidence_items),
        )
