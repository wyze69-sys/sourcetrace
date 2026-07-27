"""Static Change Impact Preview engine (IMPACT-001).

Deterministically computes, for a resolved target symbol, the bounded set of
upstream dependents (chunks whose stored references, import bindings, or HTTP
endpoint calls resolve to the target — code that may break when the target
changes) and downstream dependencies (chunks the target transitively relies
on). Classifies affected HTTP endpoints, React components/hooks, and test
files, and derives transparent risk factors from those counts alone.

Zero LLM/provider calls: the only collaborator is the code chunk repository.
Reference and endpoint resolution reuse the exact shared functions of the
flow tracer (`retrieval/trace.py`), so trace and impact never disagree about
what an identifier resolves to. Determinism rules match the tracer: storage
return order is never trusted, every candidate set is re-sorted with a total
order ending in chunk_id, caps are server-side constants, and identical
inputs yield identical results.

One deliberate difference from the tracer: when a reference resolves
ambiguously, the impact graph keeps an edge to every alternative at "low"
confidence (the tracer follows only the chosen one). Upstream impact must not
silently drop a potential dependent just because resolution was ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass

from sourcetrace.models.domain import CodeChunk, RepositoryRecord
from sourcetrace.retrieval.diff import DiffFile, parse_unified_diff
from sourcetrace.retrieval.trace import (
    EVIDENCE_PARSER_VERSIONS,
    MAX_ENTRY_CANDIDATES,
    FlowIndexes,
    build_flow_indexes,
    chunk_sort_key,
    resolve_endpoint_call,
    resolve_reference,
)
from sourcetrace.storage.repositories import CodeChunkRepository


def _build_repo_stale_detail(repository: RepositoryRecord) -> str:
    details: list[str] = []
    if repository.indexed_commit_sha:
        sha_str = (
            repository.indexed_commit_sha[:7]
            if len(repository.indexed_commit_sha) >= 7
            else repository.indexed_commit_sha
        )
        details.append(f"indexed commit {sha_str}")
    if repository.last_indexed_at:
        details.append(f"last indexed {repository.last_indexed_at.isoformat()}")

    meta_info = f" ({'; '.join(details)})" if details else ""
    return f"Repository index is out of date{meta_info}; refresh repository to update."


def _should_report_stale_index_gap(
    all_chunks: list[CodeChunk],
    repository: RepositoryRecord | None,
) -> bool:
    if repository is not None and repository.parser_versions:
        if repository.flow_evidence_complete:
            return False
        return any(c.parser_version not in EVIDENCE_PARSER_VERSIONS for c in all_chunks)
    return any(c.parser_version not in EVIDENCE_PARSER_VERSIONS for c in all_chunks)


MAX_IMPACT_DEPTH: int = 6
MAX_IMPACT_NODES_PER_DIRECTION: int = 50

_COMPONENT_SYMBOL_TYPES: frozenset[str] = frozenset({"react_component", "hook"})
_TEST_DIR_SEGMENTS: frozenset[str] = frozenset({"test", "tests", "__tests__", "spec", "specs"})

_CONFIDENCE_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}
_RANK_CONFIDENCE: dict[int, str] = {v: k for k, v in _CONFIDENCE_RANK.items()}
_SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True, slots=True)
class ImpactTarget:
    query: str
    resolved_node_id: str | None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImpactItem:
    """One impacted chunk plus the evidence citation that connects it.

    The cited evidence lines always live inside ``evidence_node_id``: for an
    upstream dependent that is the item itself (the line where it references
    toward the target); for a downstream dependency it is ``via_node_id``
    (the line where the nearer chunk references this one).
    """

    node_id: str
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    distance: int
    confidence: str  # weakest edge confidence along the discovery path
    edge_kind: str  # "call" | "http"
    via_node_id: str
    evidence_node_id: str
    evidence_label: str
    evidence_line_start: int
    evidence_line_end: int


@dataclass(frozen=True, slots=True)
class AffectedEndpoint:
    http_method: str
    normalized_path: str
    node_id: str


@dataclass(frozen=True, slots=True)
class RiskFactor:
    kind: str
    severity: str  # "low" | "medium" | "high"
    detail: str


@dataclass(frozen=True, slots=True)
class ImpactGap:
    kind: str
    detail: str
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeImpactResult:
    target: ImpactTarget
    upstream: tuple[ImpactItem, ...] = ()
    downstream: tuple[ImpactItem, ...] = ()
    affected_endpoints: tuple[AffectedEndpoint, ...] = ()
    affected_components: tuple[str, ...] = ()
    affected_tests: tuple[str, ...] = ()
    risk_level: str = "unknown"  # "low" | "medium" | "high" | "unknown"
    risk_factors: tuple[RiskFactor, ...] = ()
    gaps: tuple[ImpactGap, ...] = ()


@dataclass(frozen=True, slots=True)
class DiffTarget:
    """An indexed chunk whose old-file lines the diff touches."""

    node_id: str
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    changed_lines: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DiffImpactResult:
    targets: tuple[DiffTarget, ...] = ()
    upstream: tuple[ImpactItem, ...] = ()
    downstream: tuple[ImpactItem, ...] = ()
    affected_endpoints: tuple[AffectedEndpoint, ...] = ()
    affected_components: tuple[str, ...] = ()
    affected_tests: tuple[str, ...] = ()
    risk_level: str = "unknown"  # "low" | "medium" | "high" | "unknown"
    risk_factors: tuple[RiskFactor, ...] = ()
    gaps: tuple[ImpactGap, ...] = ()


@dataclass(frozen=True, slots=True)
class _Edge:
    source_id: str
    target_id: str
    kind: str  # "call" | "http"
    confidence: str
    label: str
    line_start: int
    line_end: int


def _is_test_path(relative_path: str) -> bool:
    """Deterministic path heuristic for test files (Python and JS/TS layouts)."""
    posix = relative_path.replace("\\", "/").casefold()
    segments = posix.split("/")
    if any(segment in _TEST_DIR_SEGMENTS for segment in segments[:-1]):
        return True
    name = segments[-1]
    stem = name.rsplit(".", 1)[0]
    return (
        name.startswith("test_") or stem.endswith("_test") or ".test." in name or ".spec." in name
    )


def _sorted_gaps(gaps: list[ImpactGap]) -> tuple[ImpactGap, ...]:
    return tuple(sorted(gaps, key=lambda g: (g.kind, g.node_id or "", g.detail)))


class ChangeImpactService:
    """Deterministic, bounded, storage-backed static change impact previewer."""

    def __init__(self, code_chunk_repo: CodeChunkRepository) -> None:
        self._chunk_repo = code_chunk_repo

    def impact(
        self,
        owner_session_id: str,
        repository_id: str,
        symbol_query: str,
        max_depth: int | None = None,
        generation_id: str | None = None,
        repository: RepositoryRecord | None = None,
    ) -> ChangeImpactResult:
        depth_cap = MAX_IMPACT_DEPTH
        if max_depth is not None:
            depth_cap = max(1, min(max_depth, MAX_IMPACT_DEPTH))

        all_chunks = sorted(
            self._chunk_repo.list_by_repository(
                owner_session_id, repository_id, generation_id=generation_id
            ),
            key=chunk_sort_key,
        )

        gaps: list[ImpactGap] = []
        if repository is not None and repository.is_stale is True:
            gaps.append(
                ImpactGap(
                    kind="repo_stale",
                    detail=_build_repo_stale_detail(repository),
                )
            )

        if _should_report_stale_index_gap(all_chunks, repository):
            stale_count = sum(
                1 for c in all_chunks if c.parser_version not in EVIDENCE_PARSER_VERSIONS
            )
            gaps.append(
                ImpactGap(
                    kind="stale_index",
                    detail=(
                        f"{stale_count} chunk(s) were indexed before flow-evidence "
                        "extraction existed; refresh the repository to include them."
                    ),
                )
            )

        target = self._resolve_target(
            owner_session_id, repository_id, symbol_query, generation_id=generation_id
        )
        by_id = {c.chunk_id: c for c in all_chunks}
        target_chunk = (
            by_id.get(target.resolved_node_id) if target.resolved_node_id is not None else None
        )
        if target_chunk is None:
            gaps.append(
                ImpactGap(
                    kind="entry_unresolved",
                    detail=f"No indexed symbol matched query {symbol_query!r}.",
                )
            )
            return ChangeImpactResult(
                target=ImpactTarget(target.query, None, target.candidates),
                risk_level="unknown",
                gaps=_sorted_gaps(gaps),
            )

        indexes = build_flow_indexes(all_chunks)
        edges, unresolved_names, unmatched_calls = self._build_edges(all_chunks, indexes)

        downstream_adjacency: dict[str, list[_Edge]] = {}
        upstream_adjacency: dict[str, list[_Edge]] = {}
        for edge in edges:
            downstream_adjacency.setdefault(edge.source_id, []).append(edge)
            upstream_adjacency.setdefault(edge.target_id, []).append(edge)

        upstream = self._bfs(
            seeds=frozenset({target_chunk.chunk_id}),
            adjacency=upstream_adjacency,
            direction="upstream",
            depth_cap=depth_cap,
            by_id=by_id,
            gaps=gaps,
        )
        downstream = self._bfs(
            seeds=frozenset({target_chunk.chunk_id}),
            adjacency=downstream_adjacency,
            direction="downstream",
            depth_cap=depth_cap,
            by_id=by_id,
            gaps=gaps,
        )

        self._append_evidence_gaps(unresolved_names, unmatched_calls, gaps)

        involved_ids = (
            {target_chunk.chunk_id}
            | {item.node_id for item in upstream}
            | {item.node_id for item in downstream}
        )
        self._append_extraction_gaps(involved_ids, by_id, gaps)

        affected_endpoints = self._affected_endpoints([target_chunk], upstream, by_id)
        affected_components = tuple(
            item.node_id for item in upstream if item.symbol_type in _COMPONENT_SYMBOL_TYPES
        )
        affected_tests = tuple(
            item.node_id for item in upstream if _is_test_path(item.relative_path)
        )

        truncated = any(g.kind in ("depth_truncated", "nodes_truncated") for g in gaps)
        risk_level, risk_factors = self._assess_risk(
            upstream, affected_endpoints, affected_tests, truncated
        )

        return ChangeImpactResult(
            target=target,
            upstream=upstream,
            downstream=downstream,
            affected_endpoints=affected_endpoints,
            affected_components=affected_components,
            affected_tests=affected_tests,
            risk_level=risk_level,
            risk_factors=risk_factors,
            gaps=_sorted_gaps(gaps),
        )

    def preview_diff(
        self,
        owner_session_id: str,
        repository_id: str,
        diff_text: str,
        max_depth: int | None = None,
        generation_id: str | None = None,
        repository: RepositoryRecord | None = None,
    ) -> DiffImpactResult:
        """Produce a deterministic static impact preview for a pasted unified diff."""
        depth_cap = MAX_IMPACT_DEPTH
        if max_depth is not None:
            depth_cap = max(1, min(max_depth, MAX_IMPACT_DEPTH))

        diff_files = parse_unified_diff(diff_text)

        all_chunks = sorted(
            self._chunk_repo.list_by_repository(
                owner_session_id, repository_id, generation_id=generation_id
            ),
            key=chunk_sort_key,
        )
        by_id = {c.chunk_id: c for c in all_chunks}
        gaps: list[ImpactGap] = []

        if repository is not None and repository.is_stale is True:
            gaps.append(
                ImpactGap(
                    kind="repo_stale",
                    detail=_build_repo_stale_detail(repository),
                )
            )

        if _should_report_stale_index_gap(all_chunks, repository):
            stale_count = sum(
                1 for c in all_chunks if c.parser_version not in EVIDENCE_PARSER_VERSIONS
            )
            gaps.append(
                ImpactGap(
                    kind="stale_index",
                    detail=(
                        f"{stale_count} chunk(s) were indexed before flow-evidence "
                        "extraction existed; refresh the repository to include them."
                    ),
                )
            )

        chunks_by_path: dict[str, list[CodeChunk]] = {}
        for chunk in all_chunks:
            chunks_by_path.setdefault(chunk.relative_path, []).append(chunk)

        target_hits: dict[str, set[int]] = {}
        for diff_file in diff_files:
            resolved_path = self._resolve_diff_path(diff_file, chunks_by_path, gaps)
            if resolved_path is None:
                continue
            file_chunks = chunks_by_path[resolved_path]

            covered: set[int] = set()
            for chunk in file_chunks:
                hit_lines = {
                    line
                    for line in diff_file.changed_old_lines
                    if chunk.start_line <= line <= chunk.end_line
                }
                if hit_lines:
                    covered.update(hit_lines)
                    target_hits.setdefault(chunk.chunk_id, set()).update(hit_lines)

            uncovered = sorted(diff_file.changed_old_lines - covered)
            if uncovered:
                shown = ", ".join(str(line) for line in uncovered[:5])
                suffix = "" if len(uncovered) <= 5 else ", ..."
                gaps.append(
                    ImpactGap(
                        kind="diff_lines_uncovered",
                        detail=(
                            f"{len(uncovered)} changed line(s) in "
                            f"{resolved_path!r} fall outside every indexed symbol "
                            f"chunk (lines {shown}{suffix}); module-level code is "
                            "not traced."
                        ),
                    )
                )

            stale_at = self._first_stale_line(diff_file, file_chunks)
            if stale_at is not None:
                gaps.append(
                    ImpactGap(
                        kind="diff_stale",
                        detail=(
                            f"The diff's unchanged/deleted text does not match "
                            f"indexed content at {resolved_path}:{stale_at}; the "
                            "diff base and the indexed revision differ, so this "
                            "preview may be inaccurate. Refresh the repository "
                            "or rebase the diff."
                        ),
                    )
                )

        targets = tuple(
            DiffTarget(
                node_id=chunk_id,
                relative_path=by_id[chunk_id].relative_path,
                symbol_name=by_id[chunk_id].symbol_name,
                symbol_type=by_id[chunk_id].symbol_type,
                start_line=by_id[chunk_id].start_line,
                end_line=by_id[chunk_id].end_line,
                changed_lines=tuple(sorted(target_hits[chunk_id])),
            )
            for chunk_id in sorted(target_hits, key=lambda i: chunk_sort_key(by_id[i]))
        )
        if not targets:
            return DiffImpactResult(risk_level="unknown", gaps=_sorted_gaps(gaps))

        indexes = build_flow_indexes(all_chunks)
        edges, unresolved_names, unmatched_calls = self._build_edges(all_chunks, indexes)
        downstream_adjacency: dict[str, list[_Edge]] = {}
        upstream_adjacency: dict[str, list[_Edge]] = {}
        for edge in edges:
            downstream_adjacency.setdefault(edge.source_id, []).append(edge)
            upstream_adjacency.setdefault(edge.target_id, []).append(edge)

        seeds = frozenset(target.node_id for target in targets)
        upstream = self._bfs(
            seeds=seeds,
            adjacency=upstream_adjacency,
            direction="upstream",
            depth_cap=depth_cap,
            by_id=by_id,
            gaps=gaps,
        )
        downstream = self._bfs(
            seeds=seeds,
            adjacency=downstream_adjacency,
            direction="downstream",
            depth_cap=depth_cap,
            by_id=by_id,
            gaps=gaps,
        )

        self._append_evidence_gaps(unresolved_names, unmatched_calls, gaps)
        involved_ids = (
            set(seeds) | {item.node_id for item in upstream} | {item.node_id for item in downstream}
        )
        self._append_extraction_gaps(involved_ids, by_id, gaps)

        target_chunks = [by_id[target.node_id] for target in targets]
        affected_endpoints = self._affected_endpoints(target_chunks, upstream, by_id)
        affected_components = tuple(
            item.node_id for item in upstream if item.symbol_type in _COMPONENT_SYMBOL_TYPES
        )
        affected_tests = tuple(
            item.node_id for item in upstream if _is_test_path(item.relative_path)
        )

        truncated = any(g.kind in ("depth_truncated", "nodes_truncated") for g in gaps)
        risk_level, risk_factors = self._assess_risk(
            upstream, affected_endpoints, affected_tests, truncated
        )

        return DiffImpactResult(
            targets=targets,
            upstream=upstream,
            downstream=downstream,
            affected_endpoints=affected_endpoints,
            affected_components=affected_components,
            affected_tests=affected_tests,
            risk_level=risk_level,
            risk_factors=risk_factors,
            gaps=_sorted_gaps(gaps),
        )

    # ------------------------------------------------------------------
    # Diff-to-index mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_diff_path(
        diff_file: DiffFile,
        chunks_by_path: dict[str, list[CodeChunk]],
        gaps: list[ImpactGap],
    ) -> str | None:
        """Match one diff file to a unique indexed path, or report why not."""
        if diff_file.old_path is None:
            gaps.append(
                ImpactGap(
                    kind="diff_file_unmatched",
                    detail=(
                        f"{diff_file.display_path!r} is added by this diff and "
                        "has no indexed baseline; its future dependents cannot "
                        "be analyzed statically."
                    ),
                )
            )
            return None

        path = diff_file.old_path
        if path in chunks_by_path:
            return path

        candidates = sorted(
            indexed
            for indexed in chunks_by_path
            if indexed.endswith("/" + path) or path.endswith("/" + indexed)
        )
        if len(candidates) == 1:
            return candidates[0]

        if not candidates:
            detail = (
                f"{path!r} does not match any indexed file path; it may be "
                "excluded from indexing or the diff may target another "
                "repository."
            )
        else:
            shown = ", ".join(candidates[:3])
            detail = (
                f"{path!r} ambiguously matches {len(candidates)} indexed "
                f"paths ({shown}); no impact was computed for it."
            )
        gaps.append(ImpactGap(kind="diff_file_unmatched", detail=detail))
        return None

    @staticmethod
    def _first_stale_line(diff_file: DiffFile, file_chunks: list[CodeChunk]) -> int | None:
        """Return the first old line whose diff text disagrees with the index.

        Compares the diff's context/deleted line samples against the stored
        chunk content at the same old-file line (trailing whitespace
        ignored). One mismatch is enough to flag the file as stale.
        """
        for line_no, text in diff_file.old_line_samples:
            for chunk in file_chunks:
                if chunk.start_line <= line_no <= chunk.end_line:
                    content_lines = chunk.content.splitlines()
                    index = line_no - chunk.start_line
                    if index < len(content_lines) and (
                        content_lines[index].rstrip() != text.rstrip()
                    ):
                        return line_no
                    break
        return None

    # ------------------------------------------------------------------
    # Target resolution (same contract as the tracer's entry resolution)
    # ------------------------------------------------------------------

    def _resolve_target(
        self,
        owner_session_id: str,
        repository_id: str,
        symbol_query: str,
        generation_id: str | None = None,
    ) -> ImpactTarget:
        results = self._chunk_repo.search_lexical(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            query_text=symbol_query,
            limit=MAX_ENTRY_CANDIDATES,
            generation_id=generation_id,
        )
        ordered = sorted(results, key=lambda r: (-r.score,) + chunk_sort_key(r.chunk))
        candidates = tuple(r.chunk.chunk_id for r in ordered)
        resolved = candidates[0] if candidates else None
        return ImpactTarget(symbol_query, resolved, candidates)

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_edges(
        chunks: list[CodeChunk], indexes: FlowIndexes
    ) -> tuple[list[_Edge], list[str], list[str]]:
        """Resolve every stored evidence item into deterministic graph edges.

        Evidence volume per chunk is already bounded at extraction time (the
        parser caps it and sets extraction_truncated), so the full graph is
        built without additional per-node caps; traversal bounds apply later.
        """
        edges: list[_Edge] = []
        unresolved_names: list[str] = []
        unmatched_calls: list[str] = []

        for chunk in chunks:
            for reference in chunk.references:
                resolution = resolve_reference(chunk, reference, indexes)
                if resolution.target is None:
                    if resolution.internal_unresolved:
                        unresolved_names.append(reference.local_name)
                    continue
                edges.append(
                    _Edge(
                        source_id=chunk.chunk_id,
                        target_id=resolution.target.chunk_id,
                        kind="call",
                        confidence=resolution.confidence,
                        label=reference.local_name,
                        line_start=reference.line_start,
                        line_end=reference.line_end,
                    )
                )
                for alternative in resolution.alternatives:
                    edges.append(
                        _Edge(
                            source_id=chunk.chunk_id,
                            target_id=alternative.chunk_id,
                            kind="call",
                            confidence="low",
                            label=reference.local_name,
                            line_start=reference.line_start,
                            line_end=reference.line_end,
                        )
                    )

            for endpoint in chunk.endpoints:
                if endpoint.kind != "calls":
                    continue
                resolution = resolve_endpoint_call(chunk, endpoint, indexes)
                label = f"{endpoint.http_method} {endpoint.path_literal}"
                if resolution.target is None:
                    unmatched_calls.append(f"{endpoint.http_method} {endpoint.normalized_path}")
                    continue
                edges.append(
                    _Edge(
                        source_id=chunk.chunk_id,
                        target_id=resolution.target.chunk_id,
                        kind="http",
                        confidence=resolution.confidence,
                        label=label,
                        line_start=endpoint.line_start,
                        line_end=endpoint.line_end,
                    )
                )
                for alternative in resolution.alternatives:
                    edges.append(
                        _Edge(
                            source_id=chunk.chunk_id,
                            target_id=alternative.chunk_id,
                            kind="http",
                            confidence="low",
                            label=label,
                            line_start=endpoint.line_start,
                            line_end=endpoint.line_end,
                        )
                    )

        return edges, unresolved_names, unmatched_calls

    # ------------------------------------------------------------------
    # Bounded traversal
    # ------------------------------------------------------------------

    @staticmethod
    def _bfs(
        seeds: frozenset[str],
        adjacency: dict[str, list[_Edge]],
        direction: str,
        depth_cap: int,
        by_id: dict[str, CodeChunk],
        gaps: list[ImpactGap],
    ) -> tuple[ImpactItem, ...]:
        """Level-by-level traversal with deterministic discovery.

        Seeds (one for a symbol preview, all matched targets for a diff
        preview) are excluded from the result set. A node's confidence is the
        weakest edge confidence along its discovery path; when several
        frontier edges reach the same new node, the highest combined
        confidence wins and earlier edges (fixed build order) break ties.
        Frontier nodes are expanded in chunk sort order.
        """

        def neighbor_of(edge: _Edge) -> str:
            return edge.source_id if direction == "upstream" else edge.target_id

        visited: dict[str, tuple[int, int, _Edge]] = {}
        seen: set[str] = set(seeds)
        frontier: dict[str, int] = {seed: _CONFIDENCE_RANK["high"] for seed in seeds}
        depth = 0
        nodes_truncated = False

        while frontier and depth < depth_cap and not nodes_truncated:
            depth += 1
            discoveries: dict[str, tuple[int, _Edge]] = {}
            for node_id in sorted(frontier, key=lambda i: chunk_sort_key(by_id[i])):
                base_rank = frontier[node_id]
                for edge in adjacency.get(node_id, ()):
                    neighbor = neighbor_of(edge)
                    if neighbor in seen:
                        continue
                    combined = min(base_rank, _CONFIDENCE_RANK[edge.confidence])
                    previous = discoveries.get(neighbor)
                    if previous is None or combined > previous[0]:
                        discoveries[neighbor] = (combined, edge)

            admitted: dict[str, int] = {}
            for neighbor in sorted(discoveries, key=lambda i: chunk_sort_key(by_id[i])):
                if len(visited) >= MAX_IMPACT_NODES_PER_DIRECTION:
                    gaps.append(
                        ImpactGap(
                            kind="nodes_truncated",
                            detail=(
                                f"{direction} impact stopped at the "
                                f"{MAX_IMPACT_NODES_PER_DIRECTION}-node limit; "
                                "the true impact set is larger."
                            ),
                        )
                    )
                    nodes_truncated = True
                    break
                rank, edge = discoveries[neighbor]
                visited[neighbor] = (depth, rank, edge)
                seen.add(neighbor)
                admitted[neighbor] = rank
            frontier = admitted

        if frontier and depth >= depth_cap and not nodes_truncated:
            has_unexpanded = any(
                neighbor_of(edge) not in seen
                for node_id in frontier
                for edge in adjacency.get(node_id, ())
            )
            if has_unexpanded:
                gaps.append(
                    ImpactGap(
                        kind="depth_truncated",
                        detail=(
                            f"{direction} traversal stopped at depth {depth_cap}; "
                            "more distant impact was not expanded."
                        ),
                    )
                )

        items: list[ImpactItem] = []
        for node_id, (distance, rank, edge) in visited.items():
            chunk = by_id[node_id]
            via_node_id = edge.target_id if direction == "upstream" else edge.source_id
            items.append(
                ImpactItem(
                    node_id=node_id,
                    relative_path=chunk.relative_path,
                    symbol_name=chunk.symbol_name,
                    symbol_type=chunk.symbol_type,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    distance=distance,
                    confidence=_RANK_CONFIDENCE[rank],
                    edge_kind=edge.kind,
                    via_node_id=via_node_id,
                    evidence_node_id=edge.source_id,
                    evidence_label=edge.label,
                    evidence_line_start=edge.line_start,
                    evidence_line_end=edge.line_end,
                )
            )
        items.sort(
            key=lambda item: (
                item.distance,
                item.relative_path,
                item.start_line,
                item.symbol_name,
                item.node_id,
            )
        )
        return tuple(items)

    # ------------------------------------------------------------------
    # Classification and risk
    # ------------------------------------------------------------------

    @staticmethod
    def _append_evidence_gaps(
        unresolved_names: list[str],
        unmatched_calls: list[str],
        gaps: list[ImpactGap],
    ) -> None:
        if unresolved_names:
            shown = ", ".join(sorted(set(unresolved_names))[:5])
            suffix = "" if len(set(unresolved_names)) <= 5 else ", ..."
            gaps.append(
                ImpactGap(
                    kind="unresolved_references",
                    detail=(
                        f"{len(unresolved_names)} reference(s) to repo-internal "
                        f"modules did not resolve to indexed symbols ({shown}{suffix}); "
                        "upstream impact may be incomplete."
                    ),
                )
            )
        if unmatched_calls:
            shown = ", ".join(sorted(set(unmatched_calls))[:5])
            suffix = "" if len(set(unmatched_calls)) <= 5 else ", ..."
            gaps.append(
                ImpactGap(
                    kind="endpoint_unmatched",
                    detail=(
                        f"{len(unmatched_calls)} HTTP call(s) had no indexed "
                        f"declaring handler ({shown}{suffix}); cross-service "
                        "impact may be incomplete."
                    ),
                )
            )

    @staticmethod
    def _append_extraction_gaps(
        involved_ids: set[str],
        by_id: dict[str, CodeChunk],
        gaps: list[ImpactGap],
    ) -> None:
        for node_id in sorted(involved_ids, key=lambda i: chunk_sort_key(by_id[i])):
            if by_id[node_id].extraction_truncated:
                gaps.append(
                    ImpactGap(
                        kind="extraction_truncated",
                        detail=(
                            "Evidence extraction was truncated for this chunk at "
                            "index time; its impact links may be incomplete."
                        ),
                        node_id=node_id,
                    )
                )

    @staticmethod
    def _affected_endpoints(
        target_chunks: list[CodeChunk],
        upstream: tuple[ImpactItem, ...],
        by_id: dict[str, CodeChunk],
    ) -> tuple[AffectedEndpoint, ...]:
        affected: set[AffectedEndpoint] = set()
        involved = list(target_chunks) + [by_id[item.node_id] for item in upstream]
        for chunk in involved:
            for endpoint in chunk.endpoints:
                if endpoint.kind == "declares":
                    affected.add(
                        AffectedEndpoint(
                            http_method=endpoint.http_method,
                            normalized_path=endpoint.normalized_path,
                            node_id=chunk.chunk_id,
                        )
                    )
        return tuple(
            sorted(
                affected,
                key=lambda e: (e.normalized_path, e.http_method, e.node_id),
            )
        )

    @staticmethod
    def _assess_risk(
        upstream: tuple[ImpactItem, ...],
        affected_endpoints: tuple[AffectedEndpoint, ...],
        affected_tests: tuple[str, ...],
        truncated: bool,
    ) -> tuple[str, tuple[RiskFactor, ...]]:
        """Derive risk factors from observable counts only — no heuristic scores."""
        factors: list[RiskFactor] = []

        dependent_count = len(upstream)
        if dependent_count >= 10:
            factors.append(
                RiskFactor(
                    kind="dependent_fanout",
                    severity="high",
                    detail=f"{dependent_count} upstream dependent(s) reference this symbol.",
                )
            )
        elif dependent_count >= 3:
            factors.append(
                RiskFactor(
                    kind="dependent_fanout",
                    severity="medium",
                    detail=f"{dependent_count} upstream dependent(s) reference this symbol.",
                )
            )

        if affected_endpoints:
            factors.append(
                RiskFactor(
                    kind="endpoint_exposure",
                    severity="high" if len(affected_endpoints) >= 3 else "medium",
                    detail=(
                        f"{len(affected_endpoints)} HTTP endpoint(s) transitively "
                        "depend on this symbol."
                    ),
                )
            )

        if not affected_tests:
            factors.append(
                RiskFactor(
                    kind="no_test_coverage",
                    severity="medium",
                    detail="No indexed test file references this symbol.",
                )
            )

        low_confidence_count = sum(1 for item in upstream if item.confidence == "low")
        if low_confidence_count:
            factors.append(
                RiskFactor(
                    kind="ambiguous_resolution",
                    severity="low",
                    detail=(
                        f"{low_confidence_count} upstream dependent(s) resolved "
                        "with low confidence."
                    ),
                )
            )

        if truncated:
            factors.append(
                RiskFactor(
                    kind="impact_truncated",
                    severity="medium",
                    detail=("Traversal limits were reached; the true impact may be larger."),
                )
            )

        factors.sort(key=lambda f: (-_SEVERITY_RANK[f.severity], f.kind))
        risk_level = (
            max((f.severity for f in factors), key=lambda s: _SEVERITY_RANK[s])
            if factors
            else "low"
        )
        return risk_level, tuple(factors)
