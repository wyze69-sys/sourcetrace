"""Offline unit tests for FlowTraceService (TRACE-003).

The fake chunk repository implements only list_by_repository and
search_lexical — any other collaborator access fails loudly, which also
proves the static tracer makes zero LLM/provider calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sourcetrace.models.domain import (
    CodeChunk,
    EndpointEvidence,
    ImportEvidence,
    ReferenceEvidence,
    RetrievalResult,
)
from sourcetrace.retrieval.trace import (
    MAX_EDGES_PER_NODE,
    MAX_TRACE_DEPTH,
    FlowTraceService,
)

_OWNER = "owner_trace"
_REPO = "repo_trace"
_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _chunk(
    chunk_id: str,
    relative_path: str,
    symbol_name: str,
    *,
    symbol_type: str = "function",
    start_line: int = 1,
    end_line: int = 10,
    references: tuple[ReferenceEvidence, ...] = (),
    imports: tuple[ImportEvidence, ...] = (),
    endpoints: tuple[EndpointEvidence, ...] = (),
    parser_version: str = "python-ast-v2",
) -> CodeChunk:
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id=_REPO,
        owner_session_id=_OWNER,
        relative_path=relative_path,
        language="python",
        symbol_name=symbol_name,
        symbol_type=symbol_type,
        start_line=start_line,
        end_line=end_line,
        content=f"def {symbol_name}(): ...",
        content_hash=f"hash_{chunk_id}",
        parser_version=parser_version,
        created_at=_NOW,
        references=references,
        imports=imports,
        endpoints=endpoints,
    )


def _ref(name: str, line: int = 5, kind: str = "call") -> ReferenceEvidence:
    return ReferenceEvidence(local_name=name, kind=kind, line_start=line, line_end=line)


class _FakeChunkRepo:
    """Implements exactly the two methods the tracer is allowed to use."""

    def __init__(self, chunks: list[CodeChunk]) -> None:
        self._chunks = chunks

    def list_by_repository(
        self, owner_session_id: str, repository_id: str, generation_id: str | None = None
    ) -> list[CodeChunk]:
        assert owner_session_id == _OWNER and repository_id == _REPO
        return list(self._chunks)

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]:
        matches = [c for c in self._chunks if query_text.casefold() in c.symbol_name.casefold()]
        return [RetrievalResult(chunk=c, score=1.0) for c in matches[:limit]]


def _trace(chunks: list[CodeChunk], entry: str, max_depth: int | None = None):
    service = FlowTraceService(_FakeChunkRepo(chunks))
    return service.trace(_OWNER, _REPO, entry, max_depth=max_depth)


def _gap_kinds(result) -> set[str]:
    return {g.kind for g in result.gaps}


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------


def test_same_file_unique_call_yields_medium_edge_with_citation() -> None:
    chunks = [
        _chunk("c_main", "src/app.py", "main", references=(_ref("helper", line=6),)),
        _chunk("c_helper", "src/app.py", "helper", start_line=20, end_line=25),
    ]
    result = _trace(chunks, "main")

    assert result.entry.resolved_node_id == "c_main"
    assert result.steps == ("c_main", "c_helper")
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert (edge.from_node_id, edge.to_node_id) == ("c_main", "c_helper")
    assert edge.kind == "call"
    assert edge.confidence == "medium"
    assert (edge.evidence_label, edge.evidence_line_start) == ("helper", 6)


def test_import_resolved_cross_file_call_is_high_confidence() -> None:
    imports = (ImportEvidence("get_stats", "services.stats", "get_stats", 1, 1),)
    chunks = [
        _chunk(
            "c_api",
            "src/api.py",
            "api_handler",
            references=(_ref("get_stats"),),
            imports=imports,
        ),
        _chunk("c_stats", "src/services/stats.py", "get_stats"),
        _chunk("c_decoy", "src/other/decoy.py", "get_stats"),
    ]
    result = _trace(chunks, "api_handler")

    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.to_node_id == "c_stats"
    assert edge.confidence == "high"
    assert edge.alternatives == ()


def test_http_edge_links_full_stack_flow() -> None:
    chunks = [
        _chunk(
            "c_component",
            "client/src/Dashboard.jsx",
            "Dashboard",
            symbol_type="react_component",
            references=(_ref("fetchStats", line=4),),
        ),
        _chunk(
            "c_client",
            "client/src/api.js",
            "fetchStats",
            endpoints=(EndpointEvidence("calls", "GET", "/api/v1/stats", "/api/v1/stats", 9, 9),),
            parser_version="js-ts-treesitter-v2",
        ),
        _chunk(
            "c_handler",
            "backend/routes/stats.py",
            "read_stats",
            references=(_ref("load_stats", line=15),),
            endpoints=(
                EndpointEvidence("declares", "GET", "/api/v1/stats", "/api/v1/stats", 12, 12),
            ),
        ),
        _chunk("c_service", "backend/services/stats.py", "load_stats"),
    ]
    result = _trace(chunks, "Dashboard")

    assert result.steps == ("c_component", "c_client", "c_handler", "c_service")
    kinds = [e.kind for e in result.edges]
    assert kinds == ["call", "http", "call"]
    http_edge = result.edges[1]
    assert http_edge.confidence == "high"
    assert http_edge.evidence_label == "GET /api/v1/stats"
    assert (http_edge.evidence_line_start, http_edge.evidence_line_end) == (9, 9)


def test_ambiguous_reference_low_confidence_with_alternatives() -> None:
    chunks = [
        _chunk("c_entry", "src/entry.py", "entry", references=(_ref("save"),)),
        _chunk("c_save_a", "src/a_store.py", "save"),
        _chunk("c_save_b", "src/b_store.py", "save"),
    ]
    result = _trace(chunks, "entry")

    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.confidence == "low"
    # Deterministic pick: lowest sort key (a_store before b_store).
    assert edge.to_node_id == "c_save_a"
    assert edge.alternatives == ("c_save_b",)


def test_external_bare_import_reference_is_silently_skipped() -> None:
    chunks = [
        _chunk(
            "c_pusher",
            "src/push.py",
            "pusher",
            references=(_ref("requests.post", kind="attribute_call"),),
            imports=(ImportEvidence("requests", "requests", "requests", 1, 1),),
        ),
    ]
    result = _trace(chunks, "pusher")

    assert result.edges == ()
    assert "unresolved_references" not in _gap_kinds(result)


def test_unresolved_relative_import_reference_reports_gap() -> None:
    chunks = [
        _chunk(
            "c_lazy",
            "src/lazy.py",
            "lazy",
            references=(_ref("missing_helper"),),
            imports=(ImportEvidence("missing_helper", ".gone", "missing_helper", 2, 2),),
        ),
    ]
    result = _trace(chunks, "lazy")

    assert result.edges == ()
    gaps = [g for g in result.gaps if g.kind == "unresolved_references"]
    assert len(gaps) == 1
    assert "missing_helper" in gaps[0].detail
    assert gaps[0].node_id == "c_lazy"


def test_endpoint_unmatched_reports_gap() -> None:
    chunks = [
        _chunk(
            "c_client",
            "client/src/api.js",
            "pushLog",
            endpoints=(EndpointEvidence("calls", "POST", "/api/v1/logs", "/api/v1/logs", 3, 3),),
            parser_version="js-ts-treesitter-v2",
        ),
    ]
    result = _trace(chunks, "pushLog")

    gaps = [g for g in result.gaps if g.kind == "endpoint_unmatched"]
    assert len(gaps) == 1
    assert "/api/v1/logs" in gaps[0].detail


# ---------------------------------------------------------------------------
# Entry behavior
# ---------------------------------------------------------------------------


def test_no_entry_match_returns_entry_unresolved_gap() -> None:
    chunks = [_chunk("c_only", "src/app.py", "main")]
    result = _trace(chunks, "nonexistent_symbol")

    assert result.entry.resolved_node_id is None
    assert result.nodes == ()
    assert result.edges == ()
    assert _gap_kinds(result) == {"entry_unresolved"}


def test_entry_candidates_are_listed() -> None:
    chunks = [
        _chunk("c_a", "src/a.py", "handler_a"),
        _chunk("c_b", "src/b.py", "handler_b"),
    ]
    result = _trace(chunks, "handler")

    assert result.entry.resolved_node_id == "c_a"
    assert set(result.entry.candidates) == {"c_a", "c_b"}


# ---------------------------------------------------------------------------
# Bounds, cycles, staleness
# ---------------------------------------------------------------------------


def test_client_max_depth_is_respected_and_reported() -> None:
    chunks = [
        _chunk("c_a", "src/a.py", "alpha", references=(_ref("beta"),)),
        _chunk("c_b", "src/b.py", "beta", references=(_ref("gamma"),)),
        _chunk("c_g", "src/g.py", "gamma"),
    ]
    result = _trace(chunks, "alpha", max_depth=1)

    assert result.steps == ("c_a", "c_b")
    gaps = [g for g in result.gaps if g.kind == "depth_truncated"]
    assert len(gaps) == 1 and gaps[0].node_id == "c_b"


def test_client_max_depth_is_clamped_to_server_cap() -> None:
    count = MAX_TRACE_DEPTH + 4
    chunks = []
    for i in range(count):
        refs = (_ref(f"chain{i + 1}x"),) if i + 1 < count else ()
        chunks.append(_chunk(f"c_{i}", f"src/m{i}.py", f"chain{i}x", references=refs))
    result = _trace(chunks, "chain0x", max_depth=99)

    # Entry at depth 0 plus MAX_TRACE_DEPTH expansions.
    assert len(result.steps) == MAX_TRACE_DEPTH + 1
    assert "depth_truncated" in _gap_kinds(result)


def test_cycle_terminates_with_gap() -> None:
    chunks = [
        _chunk("c_a", "src/a.py", "alpha", references=(_ref("beta"),)),
        _chunk("c_b", "src/b.py", "beta", references=(_ref("alpha"),)),
    ]
    result = _trace(chunks, "alpha")

    assert result.steps == ("c_a", "c_b")
    assert len(result.edges) == 2
    assert "cycle_detected" in _gap_kinds(result)


def test_fanout_is_capped_with_gap() -> None:
    fanout = MAX_EDGES_PER_NODE + 3
    refs = tuple(_ref(f"target_{i}", line=i + 2) for i in range(fanout))
    chunks = [_chunk("c_hub", "src/hub.py", "hub", references=refs)]
    chunks += [_chunk(f"c_t{i}", f"src/t{i}.py", f"target_{i}") for i in range(fanout)]
    result = _trace(chunks, "hub")

    hub_edges = [e for e in result.edges if e.from_node_id == "c_hub"]
    assert len(hub_edges) == MAX_EDGES_PER_NODE
    assert "fanout_truncated" in _gap_kinds(result)


def test_stale_parser_version_reports_stale_index_gap() -> None:
    chunks = [
        _chunk("c_new", "src/new.py", "fresh"),
        _chunk("c_old", "src/old.py", "legacy", parser_version="python-ast-v1"),
    ]
    result = _trace(chunks, "fresh")

    gaps = [g for g in result.gaps if g.kind == "stale_index"]
    assert len(gaps) == 1
    assert "refresh" in gaps[0].detail


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_storage_return_order_never_affects_result() -> None:
    chunks = [
        _chunk("c_main", "src/app.py", "main", references=(_ref("save"), _ref("load"))),
        _chunk("c_save_a", "src/a.py", "save"),
        _chunk("c_save_b", "src/b.py", "save"),
        _chunk("c_load", "src/c.py", "load"),
    ]
    forward = _trace(list(chunks), "main")
    backward = _trace(list(reversed(chunks)), "main")

    assert forward == backward
