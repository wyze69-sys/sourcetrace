"""Intent-specific retrieval orchestration over the existing chunk index.

This module does not own a vector store or a second index.  It composes the
existing semantic/lexical retrieval service and parser-backed flow tracer,
then normalizes their results into one bounded evidence bundle.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import replace
from typing import Any

from sourcetrace.generation.planning import (
    MAX_DIRECT_RESULTS_PER_QUERY,
    MAX_EXPANDED_CHUNKS,
    MAX_GRAPH_HOPS,
    MAX_GRAPH_SEEDS,
    EvidenceBundle,
    EvidencePlan,
    IntentName,
    RetrievalMethod,
    SourceCategory,
    classify_source_category,
)
from sourcetrace.models.domain import CodeChunk, GroundedEvidenceResult, RetrievedEvidence
from sourcetrace.retrieval.impact import ChangeImpactService
from sourcetrace.retrieval.trace import FlowTraceService
from sourcetrace.storage.repositories import (
    CodeChunkRepository,
    RepositoryRepository,
)


class PlannedRetrievalService:
    """Execute an evidence plan with bounded direct and relationship retrieval."""

    def __init__(
        self,
        retrieval_service: Any,
        *,
        code_chunk_repo: CodeChunkRepository | None = None,
        repository_repo: RepositoryRepository | None = None,
        trace_service: FlowTraceService | None = None,
        impact_service: ChangeImpactService | None = None,
    ) -> None:
        self._retrieval_service = retrieval_service
        self._code_chunk_repo = code_chunk_repo
        self._repository_repo = repository_repo
        self._trace_service = trace_service or (
            FlowTraceService(code_chunk_repo) if code_chunk_repo is not None else None
        )
        self._impact_service = impact_service or (
            ChangeImpactService(code_chunk_repo) if code_chunk_repo is not None else None
        )

    def retrieve(
        self,
        owner_session_id: str,
        repository_id: str,
        plan: EvidencePlan,
    ) -> EvidenceBundle:
        """Execute a plan; a failed optional branch never widens scope or fabricates evidence."""
        direct_by_key: dict[str, RetrievedEvidence] = {}
        reindex_required = False
        exact_requested = bool(
            plan.query_variants and self._looks_exact(plan.query_variants[0], plan)
        )
        max_direct = min(MAX_DIRECT_RESULTS_PER_QUERY, plan.max_evidence_items)

        for index, query_variant in enumerate(plan.query_variants):
            if not query_variant.strip():
                continue
            method = self._method_for_variant(plan, index, exact_requested)
            try:
                result = self._retrieve_direct(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    query=query_variant,
                    limit=max_direct,
                    method=method,
                    plan=plan,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                # A semantic provider or one lexical phrase can fail without
                # invalidating evidence already collected from other branches.
                continue

            if not isinstance(result, GroundedEvidenceResult):
                continue
            if result.reindex_required:
                # Re-index state is determined by the canonical retrieval
                # service and is handled by GroundedAnswerService.  Do not
                # manufacture evidence from a failed branch.
                reindex_required = True
                continue
            for item in result.items:
                if not isinstance(item, RetrievedEvidence):
                    continue
                decorated = self._decorate_direct(item, method, query_variant, plan)
                key = self._evidence_key(decorated)
                previous = direct_by_key.get(key)
                if previous is None or decorated.score > previous.score:
                    direct_by_key[key] = decorated

        selected = list(direct_by_key.values())
        expanded, hop_count = self._expand_relationships(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            plan=plan,
            seeds=selected,
        )
        for item in expanded:
            key = self._evidence_key(item)
            previous = direct_by_key.get(key)
            if previous is None or item.score > previous.score or item.relationship:
                direct_by_key[key] = item

        ranked = sorted(
            direct_by_key.values(),
            key=lambda item: (
                -float(item.score),
                item.hop,
                item.citation.relative_path,
                item.citation.start_line,
                item.chunk_id,
            ),
        )[: plan.max_evidence_items]
        deduplication_keys = tuple(self._evidence_key(item) for item in ranked)
        methods = tuple(
            dict.fromkeys(
                RetrievalMethod(item.retrieval_method)
                for item in ranked
                if item.retrieval_method in {method.value for method in RetrievalMethod}
            )
        )
        categories = tuple(
            dict.fromkeys(
                SourceCategory(item.source_category)
                for item in ranked
                if item.source_category in {category.value for category in SourceCategory}
            )
        )
        return EvidenceBundle(
            items=tuple(ranked),
            retrieval_methods=methods,
            deduplication_keys=deduplication_keys,
            expansion_hops=min(
                MAX_GRAPH_HOPS, max(hop_count, max((i.hop for i in ranked), default=0))
            ),
            source_categories=categories,
            plan=plan,
            reindex_required=reindex_required and not ranked,
        )

    def _retrieve_direct(
        self,
        *,
        owner_session_id: str,
        repository_id: str,
        query: str,
        limit: int,
        method: RetrievalMethod,
        plan: EvidencePlan,
    ) -> GroundedEvidenceResult:
        if method == RetrievalMethod.METADATA:
            if self._code_chunk_repo is not None and self._repository_repo is not None:
                return self._retrieve_metadata(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    query=query,
                    limit=limit,
                    plan=plan,
                )
            # The production dependency graph supplies both repositories.  A
            # small compatibility fallback keeps lightweight service doubles
            # on the canonical owner-scoped retrieval path.
            method = RetrievalMethod.LEXICAL
        if method == RetrievalMethod.LEXICAL and hasattr(
            self._retrieval_service, "retrieve_lexical"
        ):
            return self._retrieval_service.retrieve_lexical(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                query=query,
                limit=limit,
            )
        return self._retrieval_service.retrieve(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            query=query,
            limit=limit,
        )

    def _retrieve_metadata(
        self,
        *,
        owner_session_id: str,
        repository_id: str,
        query: str,
        limit: int,
        plan: EvidencePlan,
    ) -> GroundedEvidenceResult:
        """Select parser/index metadata without creating a second index."""
        if self._code_chunk_repo is None or self._repository_repo is None:
            return GroundedEvidenceResult()

        try:
            repository = self._repository_repo.get_by_id(owner_session_id, repository_id)
            if repository is None or repository.status != "ready":
                return GroundedEvidenceResult()
            generation_id = getattr(repository, "active_generation_id", None)
            try:
                chunks = self._code_chunk_repo.list_by_repository(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    generation_id=generation_id,
                    limit=plan.max_expanded_chunks * MAX_DIRECT_RESULTS_PER_QUERY,
                )
            except TypeError:
                chunks = self._code_chunk_repo.list_by_repository(
                    owner_session_id,
                    repository_id,
                    generation_id=generation_id,
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return GroundedEvidenceResult()

        typed_chunks = [chunk for chunk in chunks if isinstance(chunk, CodeChunk)]
        if not typed_chunks:
            return GroundedEvidenceResult()

        query_terms = set(re.findall(r"[a-z0-9][a-z0-9_./-]*", query.casefold()))
        candidates: list[RetrievedEvidence] = []
        for chunk in typed_chunks:
            category = classify_source_category(chunk.relative_path, chunk.symbol_name)
            path_text = chunk.relative_path.casefold()
            symbol_text = chunk.symbol_name.casefold()
            filename = path_text.rsplit("/", 1)[-1]
            query_hits = sum(
                1
                for term in query_terms
                if term in path_text or term in symbol_text or term in filename
            )
            category_fit = category in plan.source_categories
            if not category_fit and query_hits == 0:
                continue

            score = 0.30
            if category_fit:
                score += 0.55
            score += min(0.30, query_hits * 0.10)
            if filename.startswith(("readme", "package", "pyproject", "requirements")):
                score += 0.10
            candidates.append(
                RetrievedEvidence(
                    chunk_id=chunk.chunk_id,
                    score=score,
                    citation=self._citation(chunk),
                    snippet=self._snippet(chunk),
                    retrieval_method=RetrievalMethod.METADATA.value,
                    hop=0,
                    source_category=category.value,
                    query_variant=query[:240],
                )
            )

        candidates.sort(
            key=lambda item: (
                -item.score,
                item.citation.relative_path,
                item.citation.start_line,
                item.chunk_id,
            )
        )
        selected = tuple(candidates[:limit])
        return GroundedEvidenceResult(items=selected, total_retrieved=len(selected))

    @staticmethod
    def _looks_exact(query: str, plan: EvidencePlan) -> bool:
        return bool(
            "/" in query
            or "\\" in query
            or any(symbol in query for symbol in ("_", "::", "."))
            or plan.intent
            in (IntentName.SYMBOL_OR_FILE_EXPLANATION, IntentName.ENTRYPOINT_AND_STARTUP)
        )

    @staticmethod
    def _method_for_variant(
        plan: EvidencePlan,
        index: int,
        exact_requested: bool,
    ) -> RetrievalMethod:
        methods = plan.retrieval_methods
        if not methods:
            return RetrievalMethod.SEMANTIC
        if exact_requested and index == 0 and RetrievalMethod.LEXICAL in methods:
            return RetrievalMethod.LEXICAL
        # Preserve the intent plan's method order for the first variants. In
        # particular, testing plans intentionally use lexical manifest/runner
        # queries before semantic fallback; otherwise build config files can
        # crowd out package.json/pyproject.toml evidence.
        if index < len(methods):
            return methods[index]
        if RetrievalMethod.LEXICAL in methods and index == len(plan.query_variants) - 1:
            return RetrievalMethod.LEXICAL
        return RetrievalMethod.SEMANTIC if RetrievalMethod.SEMANTIC in methods else methods[0]

    @staticmethod
    def _decorate_direct(
        item: RetrievedEvidence,
        method: RetrievalMethod,
        query_variant: str,
        plan: EvidencePlan,
    ) -> RetrievedEvidence:
        category = classify_source_category(item.citation.relative_path, item.citation.symbol_name)
        query_lower = query_variant.casefold()
        path_lower = item.citation.relative_path.casefold()
        symbol_lower = item.citation.symbol_name.casefold()
        exact_bonus = 0.45 if path_lower in query_lower or symbol_lower in query_lower else 0.0
        category_bonus = 0.15 if category in plan.source_categories else 0.0
        method_bonus = {
            RetrievalMethod.LEXICAL: 0.18,
            RetrievalMethod.METADATA: 0.12,
            RetrievalMethod.SEMANTIC: 0.05,
            RetrievalMethod.GRAPH: 0.0,
        }[method]
        return replace(
            item,
            score=float(item.score) + exact_bonus + category_bonus + method_bonus,
            retrieval_method=method.value,
            hop=0,
            source_category=category.value,
            query_variant=query_variant[:240],
        )

    @staticmethod
    def _evidence_key(item: RetrievedEvidence) -> str:
        return (
            f"{item.chunk_id}|{item.citation.relative_path}|"
            f"{item.citation.start_line}:{item.citation.end_line}|{item.citation.symbol_name}"
        )

    def _expand_relationships(
        self,
        *,
        owner_session_id: str,
        repository_id: str,
        plan: EvidencePlan,
        seeds: list[RetrievedEvidence],
    ) -> tuple[list[RetrievedEvidence], int]:
        if (
            self._trace_service is None
            or self._code_chunk_repo is None
            or self._repository_repo is None
            or plan.requested_hop_depth <= 0
            or RetrievalMethod.GRAPH not in plan.retrieval_methods
        ):
            return [], 0

        try:
            repository = self._repository_repo.get_by_id(owner_session_id, repository_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            return [], 0
        if repository is None or repository.status != "ready":
            return [], 0

        generation_id = getattr(repository, "active_generation_id", None)
        chunks = self._list_chunks(owner_session_id, repository_id, generation_id)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        expanded: list[RetrievedEvidence] = []
        paths_seen: set[str] = set()
        max_depth = min(MAX_GRAPH_HOPS, plan.requested_hop_depth)
        max_expanded_chunks = min(MAX_EXPANDED_CHUNKS, plan.max_expanded_chunks)

        if plan.intent == IntentName.IMPACT_AND_CHANGE and self._impact_service is not None:
            return self._expand_impact_relationships(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                repository=repository,
                generation_id=generation_id,
                plan=plan,
                seeds=seeds,
                by_id=by_id,
                max_depth=max_depth,
                max_expanded_chunks=max_expanded_chunks,
            )

        for seed in sorted(seeds, key=lambda item: (-item.score, item.chunk_id))[:MAX_GRAPH_SEEDS]:
            if len(expanded) >= max_expanded_chunks:
                break
            try:
                trace = self._trace_service.trace(
                    owner_session_id=owner_session_id,
                    repository_id=repository_id,
                    entry_query=seed.citation.symbol_name or seed.citation.relative_path,
                    max_depth=max_depth,
                    generation_id=generation_id,
                    repository=repository,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                continue

            distances, relationships = self._trace_distances(trace)
            for node_id, distance in distances.items():
                if len(expanded) >= max_expanded_chunks:
                    break
                if distance <= 0 or distance > max_depth:
                    continue
                chunk = by_id.get(node_id)
                if chunk is None:
                    continue
                if (
                    chunk.relative_path not in paths_seen
                    and len(paths_seen) >= plan.max_expanded_files
                ):
                    break
                paths_seen.add(chunk.relative_path)
                category = classify_source_category(chunk.relative_path, chunk.symbol_name)
                evidence_category = SourceCategory.RELATIONSHIPS
                relation = relationships.get(node_id)
                expanded.append(
                    RetrievedEvidence(
                        chunk_id=chunk.chunk_id,
                        score=max(0.05, 0.68 - (distance * 0.12))
                        + (0.10 if category in plan.source_categories else 0.0),
                        citation=self._citation(chunk),
                        snippet=self._snippet(chunk),
                        retrieval_method=RetrievalMethod.GRAPH.value,
                        hop=distance,
                        source_category=evidence_category.value,
                        relationship=relation,
                        query_variant=seed.query_variant,
                    )
                )

        return expanded, max((item.hop for item in expanded), default=0)

    def _expand_impact_relationships(
        self,
        *,
        owner_session_id: str,
        repository_id: str,
        repository: Any,
        generation_id: str | None,
        plan: EvidencePlan,
        seeds: list[RetrievedEvidence],
        by_id: dict[str, CodeChunk],
        max_depth: int,
        max_expanded_chunks: int,
    ) -> tuple[list[RetrievedEvidence], int]:
        """Use the existing bidirectional impact graph for change questions."""
        expanded: list[RetrievedEvidence] = []
        paths_seen: set[str] = set()
        for seed in sorted(seeds, key=lambda item: (-item.score, item.chunk_id))[:MAX_GRAPH_SEEDS]:
            if len(expanded) >= max_expanded_chunks:
                break
            query = seed.citation.symbol_name or seed.citation.relative_path
            try:
                impact = self._impact_service.impact(
                    owner_session_id,
                    repository_id,
                    symbol_query=query,
                    max_depth=max_depth,
                    generation_id=generation_id,
                    repository=repository,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                continue

            for direction, items in (
                ("upstream", getattr(impact, "upstream", ())),
                ("downstream", getattr(impact, "downstream", ())),
            ):
                for impact_item in items:
                    if len(expanded) >= max_expanded_chunks:
                        break
                    distance = int(getattr(impact_item, "distance", 0))
                    node_id = getattr(impact_item, "node_id", None)
                    chunk = by_id.get(node_id)
                    if chunk is None or distance <= 0 or distance > max_depth:
                        continue
                    if (
                        chunk.relative_path not in paths_seen
                        and len(paths_seen) >= plan.max_expanded_files
                    ):
                        break
                    paths_seen.add(chunk.relative_path)
                    category = classify_source_category(chunk.relative_path, chunk.symbol_name)
                    evidence_category = SourceCategory.RELATIONSHIPS
                    edge_kind = str(getattr(impact_item, "edge_kind", "relationship"))
                    evidence_label = str(getattr(impact_item, "evidence_label", ""))
                    relation = f"{direction} {edge_kind}"
                    if evidence_label:
                        relation += f" via {evidence_label}"
                    expanded.append(
                        RetrievedEvidence(
                            chunk_id=chunk.chunk_id,
                            score=max(0.05, 0.70 - (distance * 0.12))
                            + (0.10 if category in plan.source_categories else 0.0),
                            citation=self._citation(chunk),
                            snippet=self._snippet(chunk),
                            retrieval_method=RetrievalMethod.GRAPH.value,
                            hop=distance,
                            source_category=evidence_category.value,
                            relationship=relation,
                            query_variant=seed.query_variant,
                        )
                    )

        return expanded, max((item.hop for item in expanded), default=0)

    def _list_chunks(
        self,
        owner_session_id: str,
        repository_id: str,
        generation_id: str | None,
    ) -> list[CodeChunk]:
        try:
            raw = self._code_chunk_repo.list_by_repository(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                generation_id=generation_id,
            )
        except TypeError:
            raw = self._code_chunk_repo.list_by_repository(owner_session_id, repository_id)
        return [chunk for chunk in raw if isinstance(chunk, CodeChunk)]

    @staticmethod
    def _trace_distances(trace: Any) -> tuple[dict[str, int], dict[str, str]]:
        entry_id = getattr(getattr(trace, "entry", None), "resolved_node_id", None)
        if not entry_id:
            return {}, {}
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for edge in getattr(trace, "edges", ()):
            source = getattr(edge, "from_node_id", None)
            target = getattr(edge, "to_node_id", None)
            if not source or not target:
                continue
            label = str(getattr(edge, "kind", "relationship"))
            evidence_label = getattr(edge, "evidence_label", "")
            if evidence_label:
                label = f"{label} via {evidence_label}"
            adjacency.setdefault(source, []).append((target, label))

        distances = {entry_id: 0}
        relationships: dict[str, str] = {}
        queue: deque[str] = deque([entry_id])
        while queue:
            current = queue.popleft()
            for target, label in adjacency.get(current, ()):
                if target in distances:
                    continue
                distances[target] = distances[current] + 1
                relationships[target] = label
                queue.append(target)
        return distances, relationships

    @staticmethod
    def _citation(chunk: CodeChunk):
        from sourcetrace.models.domain import CitationRecord

        return CitationRecord(
            relative_path=chunk.relative_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            symbol_name=chunk.symbol_name,
            symbol_type=chunk.symbol_type,
        )

    @staticmethod
    def _snippet(chunk: CodeChunk):
        from sourcetrace.models.domain import EvidenceSnippetRecord

        return EvidenceSnippetRecord(
            snippet=chunk.content[:2_000],
            relative_path=chunk.relative_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            symbol_name=chunk.symbol_name,
            symbol_type=chunk.symbol_type,
        )
