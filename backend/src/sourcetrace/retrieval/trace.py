"""Static Feature Flow Trace engine (TRACE-003).

Deterministically walks stored flow evidence (references, imports, endpoints
extracted at index time) from a resolved entry symbol, producing a bounded
node/edge graph with evidence line citations, confidence levels, and explicit
gaps. Makes zero LLM/provider calls: the only collaborator is the code chunk
repository.

Determinism rules: storage return order is never trusted — every candidate
set is re-sorted in memory with a total order ending in chunk_id; caps are
server-side constants; identical inputs yield identical results.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from sourcetrace.models.domain import CodeChunk, EndpointEvidence, ReferenceEvidence
from sourcetrace.storage.mongo_repositories import tokenize_identifier
from sourcetrace.storage.repositories import CodeChunkRepository

# Parser versions whose chunks carry flow evidence. python-ast-v2 chunks are
# still valid evidence — v3 only adds router-prefix folding into
# normalized_path, so v2 indexes lose some HTTP-edge matches but never lie.
EVIDENCE_PARSER_VERSIONS: frozenset[str] = frozenset(
    {"python-ast-v2", "python-ast-v3", "js-ts-treesitter-v2"}
)

MAX_TRACE_DEPTH: int = 8
MAX_TRACE_NODES: int = 50
MAX_EDGES_PER_NODE: int = 5
MAX_EVIDENCE_EXAMINED_PER_NODE: int = 10
MAX_ENTRY_CANDIDATES: int = 5
SNIPPET_MAX_CHARS: int = 400


@dataclass(frozen=True, slots=True)
class TraceNode:
    node_id: str
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    snippet: str


@dataclass(frozen=True, slots=True)
class TraceEdge:
    from_node_id: str
    to_node_id: str
    kind: str  # "call" | "import" | "http"
    confidence: str  # "high" | "medium" | "low"
    evidence_label: str
    evidence_line_start: int
    evidence_line_end: int
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TraceGap:
    kind: str
    detail: str
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class TraceEntry:
    query: str
    resolved_node_id: str | None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowTraceResult:
    entry: TraceEntry
    nodes: tuple[TraceNode, ...] = ()
    edges: tuple[TraceEdge, ...] = ()
    steps: tuple[str, ...] = ()
    gaps: tuple[TraceGap, ...] = ()


def _norm_name(name: str) -> str:
    return " ".join(tokenize_identifier(name))


def _last_segment(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def chunk_sort_key(chunk: CodeChunk) -> tuple:
    """Total deterministic order over chunks, ending in the unique chunk_id."""
    return (
        chunk.relative_path,
        chunk.start_line,
        chunk.symbol_name,
        chunk.chunk_id,
    )


def _strip_extension(path: str) -> str:
    slash = path.rfind("/")
    dot = path.rfind(".")
    if dot > slash:
        return path[:dot]
    return path


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _module_matches_path(source_module: str, importer_path: str, target_path: str) -> bool:
    """Best-effort deterministic check that an import module maps to a repo path."""
    importer_dir = posixpath.dirname(_posix(importer_path))
    target = _strip_extension(_posix(target_path))

    if source_module.startswith(("./", "../")):
        resolved = posixpath.normpath(posixpath.join(importer_dir, source_module))
        return target == resolved or target == posixpath.join(resolved, "index")

    if source_module.startswith("."):
        dots = len(source_module) - len(source_module.lstrip("."))
        remainder = source_module[dots:]
        base = importer_dir
        for _ in range(dots - 1):
            base = posixpath.dirname(base)
        candidate = (
            posixpath.normpath(posixpath.join(base, remainder.replace(".", "/")))
            if remainder
            else posixpath.normpath(base or ".")
        )
        return target == candidate or target == posixpath.join(candidate, "__init__")

    module_path = source_module.replace(".", "/")
    return (
        target == module_path
        or target.endswith("/" + module_path)
        or target == module_path + "/__init__"
        or target.endswith("/" + module_path + "/__init__")
    )


def _is_relative_module(source_module: str) -> bool:
    return source_module.startswith(".")


# ---------------------------------------------------------------------------
# Shared deterministic resolution layer (used by trace and impact services)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlowIndexes:
    """Deterministic in-memory lookup structures over a repository's chunks."""

    by_symbol_norm: dict[str, list[CodeChunk]]
    by_last_segment_norm: dict[str, list[CodeChunk]]
    declares_by_endpoint: dict[tuple[str, str], list[CodeChunk]]


def build_flow_indexes(chunks: list[CodeChunk]) -> FlowIndexes:
    """Index chunks by normalized symbol name and declared endpoints."""
    by_symbol_norm: dict[str, list[CodeChunk]] = {}
    by_last_segment_norm: dict[str, list[CodeChunk]] = {}
    declares_by_endpoint: dict[tuple[str, str], list[CodeChunk]] = {}
    for chunk in chunks:
        full_norm = _norm_name(chunk.symbol_name)
        if full_norm:
            by_symbol_norm.setdefault(full_norm, []).append(chunk)
        last_norm = _norm_name(_last_segment(chunk.symbol_name))
        if last_norm:
            by_last_segment_norm.setdefault(last_norm, []).append(chunk)
        for endpoint in chunk.endpoints:
            if endpoint.kind == "declares":
                key = (endpoint.http_method, endpoint.normalized_path)
                declares_by_endpoint.setdefault(key, []).append(chunk)
    return FlowIndexes(by_symbol_norm, by_last_segment_norm, declares_by_endpoint)


@dataclass(frozen=True, slots=True)
class ReferenceResolution:
    """Outcome of resolving one reference: a target with confidence, or a miss."""

    target: CodeChunk | None
    confidence: str
    alternatives: tuple[CodeChunk, ...]
    internal_unresolved: bool


def resolve_reference(
    source: CodeChunk,
    reference: ReferenceEvidence,
    indexes: FlowIndexes,
) -> ReferenceResolution:
    """Resolve an identifier reference to an indexed chunk, deterministically."""
    name = reference.local_name
    base = name.split(".", 1)[0]

    candidates: list[CodeChunk] = []
    seen_ids: set[str] = set()
    full_norm = _norm_name(name)
    last_norm = _norm_name(_last_segment(name))
    for key, mapping in (
        (full_norm, indexes.by_symbol_norm),
        (last_norm, indexes.by_last_segment_norm),
    ):
        if not key:
            continue
        for candidate in mapping.get(key, ()):
            if candidate.chunk_id not in seen_ids:
                seen_ids.add(candidate.chunk_id)
                candidates.append(candidate)
    candidates.sort(key=chunk_sort_key)

    binding = next(
        (imp for imp in source.imports if imp.local_name in (name, base)), None
    )

    if binding is not None:
        path_matched = [
            c
            for c in candidates
            if _module_matches_path(
                binding.source_module, source.relative_path, c.relative_path
            )
        ]
        if len(path_matched) == 1:
            return ReferenceResolution(path_matched[0], "high", (), False)
        if len(path_matched) > 1:
            return ReferenceResolution(
                path_matched[0], "low", tuple(path_matched[1:]), False
            )
        if not candidates:
            # Bound but unresolvable: internal modules are a real gap,
            # external packages are expected to be absent from the index.
            return ReferenceResolution(
                None, "", (), _is_relative_module(binding.source_module)
            )

    if not candidates:
        # No declaration and no import binding: builtin, stdlib, or a local
        # variable method — outside the indexed flow graph, silently skipped.
        return ReferenceResolution(None, "", (), False)

    if len(candidates) == 1:
        return ReferenceResolution(candidates[0], "medium", (), False)

    same_file = [c for c in candidates if c.relative_path == source.relative_path]
    pool = same_file if same_file else candidates
    chosen = pool[0]
    alternatives = tuple(c for c in candidates if c.chunk_id != chosen.chunk_id)
    return ReferenceResolution(chosen, "low", alternatives, False)


@dataclass(frozen=True, slots=True)
class EndpointResolution:
    """Outcome of matching an HTTP call to a declaring handler (None = unmatched)."""

    target: CodeChunk | None
    confidence: str
    alternatives: tuple[CodeChunk, ...]


def resolve_endpoint_call(
    source: CodeChunk,
    endpoint: EndpointEvidence,
    indexes: FlowIndexes,
) -> EndpointResolution:
    """Match an HTTP call literal against indexed declaring handlers."""
    key = (endpoint.http_method, endpoint.normalized_path)
    declaring = sorted(indexes.declares_by_endpoint.get(key, ()), key=chunk_sort_key)
    declaring = [c for c in declaring if c.chunk_id != source.chunk_id]
    if not declaring:
        return EndpointResolution(None, "", ())
    confidence = "high" if len(declaring) == 1 else "low"
    return EndpointResolution(declaring[0], confidence, tuple(declaring[1:]))


@dataclass
class _TraceState:
    nodes: dict[str, TraceNode] = field(default_factory=dict)
    edges: list[TraceEdge] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    gaps: list[TraceGap] = field(default_factory=list)
    nodes_truncated_reported: bool = False


class FlowTraceService:
    """Deterministic, bounded, storage-backed static flow tracer."""

    def __init__(self, code_chunk_repo: CodeChunkRepository) -> None:
        self._chunk_repo = code_chunk_repo

    def trace(
        self,
        owner_session_id: str,
        repository_id: str,
        entry_query: str,
        max_depth: int | None = None,
    ) -> FlowTraceResult:
        depth_cap = MAX_TRACE_DEPTH
        if max_depth is not None:
            depth_cap = max(1, min(max_depth, MAX_TRACE_DEPTH))

        all_chunks = sorted(
            self._chunk_repo.list_by_repository(owner_session_id, repository_id),
            key=chunk_sort_key,
        )

        state = _TraceState()

        stale_count = sum(
            1 for c in all_chunks if c.parser_version not in EVIDENCE_PARSER_VERSIONS
        )
        if stale_count:
            state.gaps.append(
                TraceGap(
                    kind="stale_index",
                    detail=(
                        f"{stale_count} chunk(s) were indexed before flow-evidence "
                        "extraction existed; refresh the repository to trace through them."
                    ),
                )
            )

        entry = self._resolve_entry(owner_session_id, repository_id, entry_query)
        if entry.resolved_node_id is None:
            state.gaps.append(
                TraceGap(
                    kind="entry_unresolved",
                    detail=f"No indexed symbol matched entry query {entry_query!r}.",
                )
            )
            return FlowTraceResult(entry=entry, gaps=self._sorted_gaps(state))

        by_id = {c.chunk_id: c for c in all_chunks}
        entry_chunk = by_id.get(entry.resolved_node_id)
        if entry_chunk is None:
            # Lexical search returned a chunk the repository listing does not
            # contain; report honestly rather than fabricating a trace root.
            state.gaps.append(
                TraceGap(
                    kind="entry_unresolved",
                    detail="Entry symbol could not be loaded from the repository index.",
                )
            )
            return FlowTraceResult(
                entry=TraceEntry(entry.query, None, entry.candidates),
                gaps=self._sorted_gaps(state),
            )

        indexes = build_flow_indexes(all_chunks)

        self._visit(
            chunk=entry_chunk,
            depth=0,
            depth_cap=depth_cap,
            path_ids=(),
            state=state,
            indexes=indexes,
        )

        return FlowTraceResult(
            entry=entry,
            nodes=tuple(state.nodes[node_id] for node_id in state.steps),
            edges=tuple(state.edges),
            steps=tuple(state.steps),
            gaps=self._sorted_gaps(state),
        )

    # ------------------------------------------------------------------
    # Entry resolution
    # ------------------------------------------------------------------

    def _resolve_entry(
        self, owner_session_id: str, repository_id: str, entry_query: str
    ) -> TraceEntry:
        results = self._chunk_repo.search_lexical(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            query_text=entry_query,
            limit=MAX_ENTRY_CANDIDATES,
        )
        ordered = sorted(
            results, key=lambda r: (-r.score,) + chunk_sort_key(r.chunk)
        )
        candidates = tuple(r.chunk.chunk_id for r in ordered)
        resolved = candidates[0] if candidates else None
        return TraceEntry(entry_query, resolved, candidates)

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def _ensure_node(self, chunk: CodeChunk, state: _TraceState) -> bool:
        """Register a node; return False when the node budget is exhausted."""
        if chunk.chunk_id in state.nodes:
            return True
        if len(state.nodes) >= MAX_TRACE_NODES:
            if not state.nodes_truncated_reported:
                state.gaps.append(
                    TraceGap(
                        kind="nodes_truncated",
                        detail=f"Trace stopped at the {MAX_TRACE_NODES}-node limit.",
                    )
                )
                state.nodes_truncated_reported = True
            return False
        snippet = chunk.content[:SNIPPET_MAX_CHARS]
        state.nodes[chunk.chunk_id] = TraceNode(
            node_id=chunk.chunk_id,
            relative_path=chunk.relative_path,
            symbol_name=chunk.symbol_name,
            symbol_type=chunk.symbol_type,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            snippet=snippet,
        )
        state.steps.append(chunk.chunk_id)
        return True

    def _visit(
        self,
        chunk: CodeChunk,
        depth: int,
        depth_cap: int,
        path_ids: tuple[str, ...],
        state: _TraceState,
        indexes: FlowIndexes,
    ) -> None:
        if not self._ensure_node(chunk, state):
            return
        if chunk.chunk_id in path_ids:
            return

        outgoing_evidence = list(chunk.references) + [
            e for e in chunk.endpoints if e.kind == "calls"
        ]
        if not outgoing_evidence:
            return

        if depth >= depth_cap:
            state.gaps.append(
                TraceGap(
                    kind="depth_truncated",
                    detail=f"Depth limit {depth_cap} reached before expanding this node.",
                    node_id=chunk.chunk_id,
                )
            )
            return

        edges_created = 0
        examined = 0
        unresolved_names: list[str] = []
        next_path = path_ids + (chunk.chunk_id,)

        for evidence in outgoing_evidence:
            if edges_created >= MAX_EDGES_PER_NODE or examined >= MAX_EVIDENCE_EXAMINED_PER_NODE:
                remaining = len(outgoing_evidence) - examined
                if remaining > 0:
                    state.gaps.append(
                        TraceGap(
                            kind="fanout_truncated",
                            detail=(
                                f"{remaining} further evidence item(s) not expanded "
                                "(per-node limit reached)."
                            ),
                            node_id=chunk.chunk_id,
                        )
                    )
                break
            examined += 1

            if isinstance(evidence, ReferenceEvidence):
                edge, target = self._resolve_reference(chunk, evidence, indexes)
                if edge is None:
                    if target == "internal_unresolved":
                        unresolved_names.append(evidence.local_name)
                    continue
            else:
                edge, target = self._resolve_endpoint_call(
                    chunk, evidence, state, indexes
                )
                if edge is None:
                    continue

            if not self._ensure_node(target, state):
                continue
            state.edges.append(edge)
            edges_created += 1

            if target.chunk_id in next_path:
                state.gaps.append(
                    TraceGap(
                        kind="cycle_detected",
                        detail=(
                            f"Edge to {target.symbol_name!r} closes a cycle; "
                            "traversal stopped there."
                        ),
                        node_id=target.chunk_id,
                    )
                )
                continue

            self._visit(
                target,
                depth + 1,
                depth_cap,
                next_path,
                state,
                indexes,
            )

        if unresolved_names:
            shown = ", ".join(unresolved_names[:5])
            suffix = "" if len(unresolved_names) <= 5 else ", ..."
            state.gaps.append(
                TraceGap(
                    kind="unresolved_references",
                    detail=(
                        f"{len(unresolved_names)} reference(s) to repo-internal "
                        f"modules did not resolve: {shown}{suffix}"
                    ),
                    node_id=chunk.chunk_id,
                )
            )

    # ------------------------------------------------------------------
    # Edge resolution
    # ------------------------------------------------------------------

    def _resolve_reference(
        self,
        source: CodeChunk,
        reference: ReferenceEvidence,
        indexes: FlowIndexes,
    ) -> tuple[TraceEdge | None, CodeChunk | str | None]:
        resolution = resolve_reference(source, reference, indexes)
        if resolution.target is None:
            if resolution.internal_unresolved:
                return None, "internal_unresolved"
            return None, None
        return (
            self._edge(
                source,
                resolution.target,
                "call",
                resolution.confidence,
                reference,
                tuple(a.chunk_id for a in resolution.alternatives),
            ),
            resolution.target,
        )

    def _resolve_endpoint_call(
        self,
        source: CodeChunk,
        endpoint: EndpointEvidence,
        state: _TraceState,
        indexes: FlowIndexes,
    ) -> tuple[TraceEdge | None, CodeChunk | None]:
        resolution = resolve_endpoint_call(source, endpoint, indexes)
        if resolution.target is None:
            state.gaps.append(
                TraceGap(
                    kind="endpoint_unmatched",
                    detail=(
                        f"No indexed handler declares {endpoint.http_method} "
                        f"{endpoint.normalized_path!r}."
                    ),
                    node_id=source.chunk_id,
                )
            )
            return None, None

        edge = TraceEdge(
            from_node_id=source.chunk_id,
            to_node_id=resolution.target.chunk_id,
            kind="http",
            confidence=resolution.confidence,
            evidence_label=f"{endpoint.http_method} {endpoint.path_literal}",
            evidence_line_start=endpoint.line_start,
            evidence_line_end=endpoint.line_end,
            alternatives=tuple(c.chunk_id for c in resolution.alternatives),
        )
        return edge, resolution.target

    @staticmethod
    def _edge(
        source: CodeChunk,
        target: CodeChunk,
        kind: str,
        confidence: str,
        reference: ReferenceEvidence,
        alternatives: tuple[str, ...],
    ) -> TraceEdge:
        return TraceEdge(
            from_node_id=source.chunk_id,
            to_node_id=target.chunk_id,
            kind=kind,
            confidence=confidence,
            evidence_label=reference.local_name,
            evidence_line_start=reference.line_start,
            evidence_line_end=reference.line_end,
            alternatives=alternatives,
        )

    @staticmethod
    def _sorted_gaps(state: _TraceState) -> tuple[TraceGap, ...]:
        return tuple(
            sorted(state.gaps, key=lambda g: (g.kind, g.node_id or "", g.detail))
        )
