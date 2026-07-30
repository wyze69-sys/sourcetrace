"""Provider-neutral semantic code retrieval service."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
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
    IndexingJobRepository,
    RepositoryRepository,
)


class SemanticRetrievalService:
    """Injectable provider-neutral semantic code retrieval service."""

    def __init__(
        self,
        repository_repo: RepositoryRepository,
        code_chunk_repo: CodeChunkRepository,
        embedding_provider: EmbeddingProvider | None = None,
        indexing_job_repo: IndexingJobRepository | None = None,
        *,
        max_evidence_results: int = 5,
        max_snippet_chars: int = 2000,
        max_total_evidence_chars: int = 10000,
        vector_search_max_limit: int = 50,
    ) -> None:
        self._repository_repo = repository_repo
        self._code_chunk_repo = code_chunk_repo
        self._embedding_provider = embedding_provider
        self._indexing_job_repo = indexing_job_repo

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

        # 3. Repository Readiness Validation & Active Generation Recovery
        repo_record = self._validate_repository_readiness(owner_session_id, repository_id)
        active_gen, has_valid_chunks = self._resolve_and_persist_active_generation(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            repo_record=repo_record,
        )

        if not has_valid_chunks:
            return GroundedEvidenceResult(items=(), total_retrieved=0, reindex_required=True)

        # 4. Embed Query or Lexical Search
        is_static = (
            self._embedding_provider is None
            or getattr(self._embedding_provider, "embedding_dimensions", -1) == 0
            or getattr(self._embedding_provider, "model_identifier", "") == "none"
        )

        if self._is_orientation_question(query_text):
            search_results = self._retrieve_orientation_evidence(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                limit=valid_limit,
                generation_id=active_gen,
            )
        elif is_static:
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

        # 5. Query Planning Fallback if direct retrieval produced no usable evidence
        # (top_score < 0.75 or top_score < 0.85 for domain intent expansion)
        is_orientation = self._is_orientation_question(query_text)
        is_intent_query = self._is_startup_question(query_text) or self._is_auth_question(
            query_text
        )
        top_score = max((float(getattr(r, "score", 0.0)) for r in search_results), default=0.0)
        if not is_orientation and (
            not search_results
            or (top_score < 0.75 and not is_intent_query)
            or (top_score < 0.85 and is_intent_query)
        ):
            fallback_results = self._execute_query_planning_fallback(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                query_text=query_text,
                limit=valid_limit,
                generation_id=active_gen,
            )
            if fallback_results:
                search_results = self._combine_search_results(
                    direct_results=search_results,
                    fallback_results=fallback_results,
                    limit=valid_limit,
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
        return self._construct_grounded_evidence(validated_results, query_text=query_text)

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

    def _resolve_and_persist_active_generation(
        self,
        owner_session_id: str,
        repository_id: str,
        repo_record: RepositoryRecord,
    ) -> tuple[str | None, bool]:
        """Resolve and auto-recover valid active generation containing indexed chunks.

        Rules:
        - Strictly scoped to same owner_session_id and repository_id.
        - Existing valid active generation with chunks is preserved unchanged.
        - Legacy chunks (generation_id=None) are migrated to a non-empty recovery generation ID.
        - Non-legacy recovery selects the newest completed indexing job with valid chunks.
        - Persistence failures are explicitly handled and raised as RetrievalError.
        - Repositories without valid source chunks return (None, False).
        """
        current_active = getattr(repo_record, "active_generation_id", None)

        if not hasattr(self._code_chunk_repo, "list_by_repository"):
            return current_active, True

        # 1. Preserve existing valid active generation if it has chunks
        if isinstance(current_active, str) and current_active.strip():
            chunks = self._code_chunk_repo.list_by_repository(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                generation_id=current_active.strip(),
                limit=1,
            )
            if chunks:
                return current_active.strip(), True

        # 2. Check for legacy chunks with generation_id = None
        legacy_chunks = self._code_chunk_repo.list_by_repository(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            generation_id=None,
            limit=1,
        )
        if legacy_chunks:
            recovery_gen_id = f"job_ref_legacy_{repository_id}"
            if hasattr(self._code_chunk_repo, "migrate_legacy_generation"):
                self._code_chunk_repo.migrate_legacy_generation(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    target_generation_id=recovery_gen_id,
                )
            if hasattr(self._repository_repo, "update_active_generation"):
                now_utc = datetime.now(UTC)
                updated = self._repository_repo.update_active_generation(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    active_generation_id=recovery_gen_id,
                    updated_at=now_utc,
                )
                if updated is None:
                    raise RetrievalError("Failed to persist resolved active generation safely.")
            return recovery_gen_id, True

        # 3. Modern resolution: query completed indexing jobs for newest valid generation
        if getattr(self, "_indexing_job_repo", None) is not None and hasattr(
            self._indexing_job_repo, "list_by_repository"
        ):
            jobs = self._indexing_job_repo.list_by_repository(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
            )
            completed_jobs = [
                j
                for j in jobs
                if getattr(j, "status", None) in ("ready", "completed")
                and getattr(j, "job_id", None)
            ]
            completed_jobs.sort(
                key=lambda j: (
                    getattr(j, "completed_at", None)
                    or getattr(j, "updated_at", None)
                    or datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )
            for candidate in completed_jobs:
                cand_chunks = self._code_chunk_repo.list_by_repository(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    generation_id=candidate.job_id,
                    limit=1,
                )
                if cand_chunks:
                    if hasattr(self._repository_repo, "update_active_generation"):
                        now_utc = datetime.now(UTC)
                        updated = self._repository_repo.update_active_generation(
                            owner_session_id=owner_session_id,
                            repository_id=repository_id,
                            active_generation_id=candidate.job_id,
                            updated_at=now_utc,
                        )
                        if updated is None:
                            raise RetrievalError(
                                "Failed to persist resolved active generation safely."
                            )
                    return candidate.job_id, True

        return None, False

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
        self, results: list[RetrievalResult], query_text: str = ""
    ) -> GroundedEvidenceResult:
        is_startup = self._is_startup_question(query_text) if query_text else False
        is_auth = self._is_auth_question(query_text) if query_text else False

        def compute_rank_score(r: RetrievalResult) -> float:
            base_score = float(r.score)
            path_lower = (r.chunk.relative_path or "").lower()
            sym_lower = (r.chunk.symbol_name or "").lower()

            if is_startup:
                if any(
                    k in path_lower or k in sym_lower
                    for k in (
                        "main",
                        "app",
                        "server",
                        "index",
                        "bootstrap",
                        "create_app",
                        "start",
                        "entry",
                        "listen",
                        "wsgi",
                    )
                ):
                    return base_score + 0.30
                return base_score - 0.10

            if is_auth:
                auth_keywords = (
                    "auth",
                    "login",
                    "token",
                    "session",
                    "jwt",
                    "password",
                    "security",
                    "signin",
                    "signout",
                    "authenticate",
                    "bearer",
                    "credential",
                    "permission",
                    "guard",
                    "oauth",
                )
                chunk_text = (
                    f"{path_lower} {sym_lower} "
                    f"{' '.join(r.chunk.search_terms or ())} "
                    f"{r.chunk.content or ''}"
                ).lower()
                if any(k in chunk_text for k in auth_keywords):
                    return base_score + 0.30
                return base_score - 0.30

            return base_score

        sorted_results = sorted(
            results,
            key=lambda r: (
                -compute_rank_score(r),
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

    def _derive_fallback_search_phrases(self, query_text: str) -> tuple[str, ...]:
        phrases: list[str] = []
        seen: set[str] = set()

        def add_phrase(p: str) -> None:
            p_clean = p.strip()
            if p_clean and len(p_clean) <= 40 and p_clean.lower() not in seen:
                seen.add(p_clean.lower())
                phrases.append(p_clean)

        from sourcetrace.storage.mongo_repositories import _ENGLISH_STOP_WORDS, tokenize_identifier

        raw_tokens = tokenize_identifier(query_text)
        query_token_set = set(t.lower() for t in raw_tokens)

        # 1. Deterministic Whole-Token Intent Mapping Rules
        # Entrypoint / App Startup
        if query_token_set.intersection(
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
            for term in (
                "main",
                "app",
                "server",
                "index",
                "bootstrap",
                "create_app",
                "start",
                "entry",
                "listen",
                "wsgi",
            ):
                add_phrase(term)

        # Authentication / Login
        if query_token_set.intersection(
            {
                "login",
                "auth",
                "authentication",
                "session",
                "jwt",
                "token",
                "password",
                "user",
                "signin",
                "security",
                "authenticate",
                "bearer",
            }
        ):
            for term in (
                "auth",
                "login",
                "session",
                "jwt",
                "token",
                "user",
                "authenticate",
                "middleware",
                "security",
                "passport",
                "bearer",
            ):
                add_phrase(term)

        # API Routes / Endpoints
        if query_token_set.intersection(
            {
                "route",
                "routes",
                "api",
                "endpoint",
                "endpoints",
                "controller",
                "handler",
                "handlers",
                "router",
            }
        ):
            for term in ("route", "router", "api", "endpoint", "controller", "handler"):
                add_phrase(term)

        # Configuration / Settings
        if query_token_set.intersection(
            {
                "config",
                "configuration",
                "settings",
                "setting",
                "env",
                "environment",
                "options",
                "option",
            }
        ):
            for term in ("config", "settings", "env", "configuration"):
                add_phrase(term)

        # Database / Storage / Connection
        if query_token_set.intersection(
            {
                "database",
                "db",
                "mongo",
                "mongodb",
                "storage",
                "repository",
                "connection",
                "connect",
                "sql",
                "orm",
                "client",
            }
        ):
            for term in ("database", "db", "mongo", "storage", "repository", "connection"):
                add_phrase(term)

        # 2. Extract Non-stopword Keywords from Query
        tokens = [t for t in raw_tokens if t.lower() not in _ENGLISH_STOP_WORDS]
        for t in tokens:
            add_phrase(t)

        if len(tokens) >= 2:
            add_phrase(" ".join(tokens[:3]))

        return tuple(phrases[:12])

    def _execute_query_planning_fallback(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int,
        generation_id: str | None = None,
    ) -> list[Any]:
        fallback_phrases = self._derive_fallback_search_phrases(query_text)
        if not fallback_phrases or not hasattr(self._code_chunk_repo, "search_lexical"):
            return []

        results: list[Any] = []
        for phrase in fallback_phrases:
            res = self._code_chunk_repo.search_lexical(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                query_text=phrase,
                limit=limit,
                generation_id=generation_id,
            )
            if res:
                results.extend(res)

        return results

    def _combine_search_results(
        self,
        direct_results: Sequence[Any],
        fallback_results: Sequence[Any],
        limit: int,
    ) -> list[Any]:
        combined_by_id: dict[str, Any] = {}

        for item in direct_results:
            chunk = getattr(item, "chunk", None)
            cid = getattr(chunk, "chunk_id", None) if chunk else None
            if cid:
                combined_by_id[cid] = item

        for item in fallback_results:
            chunk = getattr(item, "chunk", None)
            cid = getattr(chunk, "chunk_id", None) if chunk else None
            if cid:
                if cid not in combined_by_id:
                    combined_by_id[cid] = item
                else:
                    existing = combined_by_id[cid]
                    ex_score = float(getattr(existing, "score", 0.0))
                    fb_score = float(getattr(item, "score", 0.0))
                    if fb_score > ex_score:
                        combined_by_id[cid] = item

        combined = list(combined_by_id.values())
        combined.sort(
            key=lambda r: (
                -float(getattr(r, "score", 0.0)),
                getattr(getattr(r, "chunk", None), "relative_path", ""),
                getattr(getattr(r, "chunk", None), "start_line", 0),
                getattr(getattr(r, "chunk", None), "chunk_id", ""),
            )
        )
        return combined[:limit]

    def _is_orientation_question(self, query_text: str) -> bool:
        from sourcetrace.generation.prompts import _is_orientation_prompt_question

        return _is_orientation_prompt_question(query_text)

    def _is_startup_question(self, query_text: str) -> bool:
        from sourcetrace.storage.mongo_repositories import tokenize_identifier

        raw_tokens = tokenize_identifier(query_text)
        tokens = set(t.lower() for t in raw_tokens)

        if (
            "start" in tokens
            or "started" in tokens
            or "starting" in tokens
            or "boot" in tokens
            or "bootstrap" in tokens
            or "launch" in tokens
            or "entry" in tokens
            or "entrypoint" in tokens
            or "run" in tokens
            or "running" in tokens
        ) and (
            "where" in tokens
            or "how" in tokens
            or "application" in tokens
            or "app" in tokens
            or "server" in tokens
            or "main" in tokens
            or "code" in tokens
            or "project" in tokens
        ):
            return True
        return False

    def _is_auth_question(self, query_text: str) -> bool:
        from sourcetrace.storage.mongo_repositories import tokenize_identifier

        raw_tokens = tokenize_identifier(query_text)
        tokens = set(t.lower() for t in raw_tokens)

        if tokens.intersection(
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
            return True
        return False

    def _retrieve_orientation_evidence(
        self,
        owner_session_id: str,
        repository_id: str,
        limit: int,
        generation_id: str | None = None,
    ) -> list[Any]:
        if not hasattr(self._code_chunk_repo, "search_lexical"):
            return []

        orientation_terms = (
            "readme",
            "docs",
            "documentation",
            "overview",
            "architecture",
            "package",
            "config",
            "settings",
            "environment",
            "main",
            "app",
            "server",
            "index",
            "bootstrap",
            "create_app",
            "route",
            "router",
            "api",
            "controller",
            "service",
        )

        raw_candidates: list[Any] = []
        for term in orientation_terms:
            res = self._code_chunk_repo.search_lexical(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                query_text=term,
                limit=limit * 2,
                generation_id=generation_id,
            )
            if res:
                raw_candidates.extend(res)

        if not raw_candidates:
            return []

        unique_results: dict[str, Any] = {}
        for item in raw_candidates:
            chunk = getattr(item, "chunk", None)
            if chunk is None:
                continue
            chunk_id = chunk.chunk_id
            path_lower = (chunk.relative_path or "").lower()
            sym_lower = (chunk.symbol_name or "").lower()
            filename = path_lower.split("/")[-1]

            score = float(getattr(item, "score", 0.5))

            if any(k in path_lower for k in ("readme", "docs/", "documentation", "architecture")):
                score = max(score, 1.0)
            elif filename in (
                "package.json",
                "pyproject.toml",
                "cargo.toml",
                "go.mod",
                "build.gradle",
                "pom.xml",
                "requirements.txt",
                "composer.json",
                "setup.py",
                "setup.cfg",
            ):
                score = max(score, 0.95)
            elif (
                filename in (
                    "server.js",
                    "server.ts",
                    "server.py",
                    "app.js",
                    "app.ts",
                    "app.py",
                    "app.jsx",
                    "app.tsx",
                    "main.py",
                    "main.js",
                    "main.ts",
                    "main.go",
                    "index.js",
                    "index.ts",
                    "index.py",
                    "index.html",
                    "bootstrap.py",
                    "bootstrap.ts",
                    "wsgi.py",
                    "asgi.py",
                )
                or (
                    any(
                        filename.endswith(ext)
                        for ext in (".js", ".ts", ".jsx", ".tsx", ".py", ".go")
                    )
                    and any(
                        stem in filename for stem in ("server", "app", "main", "index", "bootstrap")
                    )
                )
                or sym_lower in ("main", "create_app", "bootstrap", "listen", "start_server")
            ):
                score = max(score, 0.90)
            elif any(
                k in path_lower or k in sym_lower
                for k in ("config", "settings", "env", "configuration")
            ):
                score = max(score, 0.85)
            elif (
                filename
                in (
                    "routes.py",
                    "routes.js",
                    "routes.ts",
                    "router.py",
                    "router.js",
                    "router.ts",
                    "api.py",
                    "api.js",
                    "api.ts",
                    "urls.py",
                )
                or filename.startswith(("route", "router", "controller", "service"))
            ):
                score = max(score, 0.80)
            else:
                score = 0.30

            new_item = RetrievalResult(chunk=chunk, score=score)
            if chunk_id not in unique_results or score > unique_results[chunk_id].score:
                unique_results[chunk_id] = new_item

        all_candidates = list(unique_results.values())
        strong_candidates = [c for c in all_candidates if c.score >= 0.75]
        if not strong_candidates:
            return []

        sorted_list = sorted(
            strong_candidates,
            key=lambda r: (
                -r.score,
                r.chunk.relative_path,
                r.chunk.start_line,
                r.chunk.chunk_id,
            ),
        )
        return sorted_list[:limit]
