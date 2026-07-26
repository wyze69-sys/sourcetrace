"""Offline unit tests for ChangeImpactService (IMPACT-001).

The fake chunk repository implements only list_by_repository and
search_lexical — any other collaborator access fails loudly, which also
proves the static impact previewer makes zero LLM/provider calls.
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
from sourcetrace.retrieval.impact import (
    MAX_IMPACT_DEPTH,
    MAX_IMPACT_NODES_PER_DIRECTION,
    ChangeImpactService,
)

_OWNER = "owner_impact"
_REPO = "repo_impact"
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
    extraction_truncated: bool = False,
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
        extraction_truncated=extraction_truncated,
    )


def _ref(name: str, line: int = 5, kind: str = "call") -> ReferenceEvidence:
    return ReferenceEvidence(local_name=name, kind=kind, line_start=line, line_end=line)


class _FakeChunkRepo:
    """Implements exactly the two methods the impact service is allowed to use."""

    def __init__(self, chunks: list[CodeChunk]) -> None:
        self._chunks = chunks

    def list_by_repository(self, owner_session_id: str, repository_id: str) -> list[CodeChunk]:
        assert owner_session_id == _OWNER and repository_id == _REPO
        return list(self._chunks)

    def search_lexical(
        self, owner_session_id: str, repository_id: str, query_text: str, limit: int = 5
    ) -> list[RetrievalResult]:
        # Match symbol names or paths — the real implementation indexes both
        # via search_terms.
        needle = query_text.casefold()
        matches = [
            c
            for c in self._chunks
            if needle in c.symbol_name.casefold()
            or needle in c.relative_path.casefold()
        ]
        return [RetrievalResult(chunk=c, score=1.0) for c in matches[:limit]]


def _preview(chunks: list[CodeChunk], symbol: str, max_depth: int | None = None):
    service = ChangeImpactService(_FakeChunkRepo(chunks))
    return service.preview(_OWNER, _REPO, symbol, max_depth=max_depth)


def _gap_kinds(result) -> set[str]:
    return {g.kind for g in result.gaps}


def _factor_kinds(result) -> set[str]:
    return {f.kind for f in result.risk_factors}


# ---------------------------------------------------------------------------
# Upstream / downstream discovery
# ---------------------------------------------------------------------------


def test_direct_upstream_dependent_found_with_citation() -> None:
    chunks = [
        _chunk("c_caller", "src/api.py", "handler", references=(_ref("compute", line=7),)),
        _chunk("c_target", "src/calc.py", "compute"),
    ]
    result = _preview(chunks, "compute")

    assert result.target.resolved_node_id == "c_target"
    assert [item.node_id for item in result.upstream] == ["c_caller"]
    item = result.upstream[0]
    assert item.distance == 1
    assert item.confidence == "medium"
    assert item.edge_kind == "call"
    # Citation lives inside the dependent itself: the line where it calls.
    assert item.evidence_node_id == "c_caller"
    assert item.via_node_id == "c_target"
    assert (item.evidence_label, item.evidence_line_start) == ("compute", 7)
    assert result.downstream == ()


def test_downstream_dependency_found_with_citation_in_via_node() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute", references=(_ref("load", line=3),)),
        _chunk("c_dep", "src/store.py", "load"),
    ]
    result = _preview(chunks, "compute")

    assert [item.node_id for item in result.downstream] == ["c_dep"]
    item = result.downstream[0]
    assert item.distance == 1
    # Citation lives in the nearer chunk (the target references the dependency).
    assert item.evidence_node_id == "c_target"
    assert item.via_node_id == "c_target"
    assert item.evidence_line_start == 3
    assert result.upstream == ()


def test_transitive_upstream_confidence_is_weakest_edge_on_path() -> None:
    imports = (ImportEvidence("compute", "calc", "compute", 1, 1),)
    chunks = [
        # far -> mid is ambiguous (two "process" chunks) => low edge;
        # mid -> target is import-bound unique => high edge.
        _chunk("c_far", "src/far.py", "entry", references=(_ref("process"),)),
        _chunk("c_mid", "src/a_mid.py", "process", references=(_ref("compute"),), imports=imports),
        _chunk("c_mid2", "src/b_mid.py", "process"),
        _chunk("c_target", "src/calc.py", "compute"),
    ]
    result = _preview(chunks, "compute")

    by_node = {item.node_id: item for item in result.upstream}
    assert by_node["c_mid"].confidence == "high"
    assert by_node["c_mid"].distance == 1
    assert by_node["c_far"].confidence == "low"
    assert by_node["c_far"].distance == 2


def test_ambiguous_alternative_still_counts_as_upstream_dependent() -> None:
    # "save" is ambiguous between a_store and b_store. Resolution picks
    # a_store deterministically, so b_store is only an *alternative* — but the
    # impact preview of b_store must still report the caller as a potential
    # (low-confidence) dependent instead of silently dropping it.
    chunks = [
        _chunk("c_caller", "src/entry.py", "entry", references=(_ref("save", line=6),)),
        _chunk("c_save_a", "src/a_store.py", "save"),
        _chunk("c_save_b", "src/b_store.py", "save"),
    ]

    result_chosen = _preview(chunks, "a_store")
    assert result_chosen.target.resolved_node_id == "c_save_a"
    assert [item.node_id for item in result_chosen.upstream] == ["c_caller"]
    assert result_chosen.upstream[0].confidence == "low"

    result_alternative = _preview(chunks, "b_store")
    assert result_alternative.target.resolved_node_id == "c_save_b"
    assert [item.node_id for item in result_alternative.upstream] == ["c_caller"]
    item = result_alternative.upstream[0]
    assert item.confidence == "low"
    assert (item.evidence_label, item.evidence_line_start) == ("save", 6)


def test_http_edges_connect_frontend_to_backend_upstream() -> None:
    chunks = [
        _chunk(
            "c_component",
            "client/src/Dashboard.jsx",
            "Dashboard",
            symbol_type="react_component",
            references=(_ref("fetchStats", line=4),),
            parser_version="js-ts-treesitter-v2",
        ),
        _chunk(
            "c_client",
            "client/src/api.js",
            "fetchStats",
            endpoints=(
                EndpointEvidence("calls", "GET", "/api/v1/stats", "/api/v1/stats", 9, 9),
            ),
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
        _chunk("c_target", "backend/services/stats.py", "load_stats"),
    ]
    result = _preview(chunks, "load_stats")

    upstream_ids = [item.node_id for item in result.upstream]
    assert set(upstream_ids) == {"c_handler", "c_client", "c_component"}
    by_node = {item.node_id: item for item in result.upstream}
    assert by_node["c_handler"].distance == 1
    assert by_node["c_client"].distance == 2
    assert by_node["c_client"].edge_kind == "http"
    assert by_node["c_client"].evidence_label == "GET /api/v1/stats"
    assert by_node["c_component"].distance == 3

    # Classification: the declaring handler's endpoint and the component.
    assert [(e.http_method, e.normalized_path, e.node_id) for e in result.affected_endpoints] == [
        ("GET", "/api/v1/stats", "c_handler")
    ]
    assert result.affected_components == ("c_component",)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_affected_tests_detected_by_path_heuristic() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute"),
        _chunk("c_test", "tests/test_calc.py", "test_compute", references=(_ref("compute"),)),
        _chunk("c_spec", "client/src/calc.spec.ts", "calcSpec", references=(_ref("compute"),),
               parser_version="js-ts-treesitter-v2"),
        _chunk("c_plain", "src/app.py", "run", references=(_ref("compute"),)),
    ]
    result = _preview(chunks, "compute")

    assert set(result.affected_tests) == {"c_test", "c_spec"}
    assert "no_test_coverage" not in _factor_kinds(result)


def test_target_declaring_endpoint_is_reported_affected() -> None:
    chunks = [
        _chunk(
            "c_target",
            "backend/routes/stats.py",
            "read_stats",
            endpoints=(
                EndpointEvidence("declares", "GET", "/api/v1/stats", "/api/v1/stats", 2, 2),
            ),
        ),
    ]
    result = _preview(chunks, "read_stats")

    assert [(e.http_method, e.normalized_path) for e in result.affected_endpoints] == [
        ("GET", "/api/v1/stats")
    ]
    assert "endpoint_exposure" in _factor_kinds(result)


# ---------------------------------------------------------------------------
# Risk factors
# ---------------------------------------------------------------------------


def test_no_dependents_and_no_tests_yields_medium_risk_no_test_coverage() -> None:
    chunks = [_chunk("c_target", "src/calc.py", "compute")]
    result = _preview(chunks, "compute")

    assert result.risk_level == "medium"
    assert _factor_kinds(result) == {"no_test_coverage"}


def test_dependent_fanout_factor_scales_with_count() -> None:
    dependents = [
        _chunk(f"c_dep{i}", f"src/dep{i}.py", f"caller{i}", references=(_ref("compute"),))
        for i in range(10)
    ]
    chunks = [_chunk("c_target", "src/calc.py", "compute"), *dependents]
    result = _preview(chunks, "compute")

    fanout = [f for f in result.risk_factors if f.kind == "dependent_fanout"]
    assert len(fanout) == 1
    assert fanout[0].severity == "high"
    assert "10" in fanout[0].detail
    assert result.risk_level == "high"


def test_risk_factors_sorted_most_severe_first_and_level_is_max() -> None:
    dependents = [
        _chunk(f"c_dep{i}", f"src/dep{i}.py", f"caller{i}", references=(_ref("compute"),))
        for i in range(3)
    ]
    chunks = [_chunk("c_target", "src/calc.py", "compute"), *dependents]
    result = _preview(chunks, "compute")

    severities = [f.severity for f in result.risk_factors]
    ranks = [{"low": 1, "medium": 2, "high": 3}[s] for s in severities]
    assert ranks == sorted(ranks, reverse=True)
    assert result.risk_level == "medium"
    assert {"dependent_fanout", "no_test_coverage"} <= _factor_kinds(result)


def test_ambiguous_resolution_factor_present_for_low_confidence_dependents() -> None:
    chunks = [
        _chunk("c_caller", "src/entry.py", "entry", references=(_ref("save"),)),
        _chunk("c_save_a", "src/a_store.py", "save"),
        _chunk("c_save_b", "src/b_store.py", "save"),
    ]
    result = _preview(chunks, "save")

    assert "ambiguous_resolution" in _factor_kinds(result)


# ---------------------------------------------------------------------------
# Bounds and gaps
# ---------------------------------------------------------------------------


def test_max_depth_clamped_and_depth_truncation_reported() -> None:
    chunks = [
        _chunk("c_far", "src/far.py", "far_entry", references=(_ref("mid_step"),)),
        _chunk("c_mid", "src/mid.py", "mid_step", references=(_ref("compute"),)),
        _chunk("c_target", "src/calc.py", "compute"),
    ]
    result = _preview(chunks, "compute", max_depth=1)

    assert [item.node_id for item in result.upstream] == ["c_mid"]
    gaps = [g for g in result.gaps if g.kind == "depth_truncated"]
    assert len(gaps) == 1
    assert "upstream" in gaps[0].detail
    assert "impact_truncated" in _factor_kinds(result)


def test_requested_depth_above_server_cap_is_clamped() -> None:
    count = MAX_IMPACT_DEPTH + 3
    chunks = [_chunk("c_target", "src/m0.py", "level0x")]
    for i in range(1, count + 1):
        chunks.append(
            _chunk(f"c_{i}", f"src/m{i}.py", f"level{i}x", references=(_ref(f"level{i - 1}x"),))
        )
    result = _preview(chunks, "level0x", max_depth=99)

    assert max(item.distance for item in result.upstream) == MAX_IMPACT_DEPTH
    assert "depth_truncated" in _gap_kinds(result)


def test_node_cap_truncates_upstream_with_gap() -> None:
    extra = MAX_IMPACT_NODES_PER_DIRECTION + 5
    dependents = [
        _chunk(f"c_dep{i:03d}", f"src/dep{i:03d}.py", f"caller{i}x", references=(_ref("compute"),))
        for i in range(extra)
    ]
    chunks = [_chunk("c_target", "src/calc.py", "compute"), *dependents]
    result = _preview(chunks, "compute")

    assert len(result.upstream) == MAX_IMPACT_NODES_PER_DIRECTION
    assert "nodes_truncated" in _gap_kinds(result)
    assert "impact_truncated" in _factor_kinds(result)


def test_unresolved_target_reports_gap_and_unknown_risk() -> None:
    chunks = [_chunk("c_only", "src/app.py", "main")]
    result = _preview(chunks, "nonexistent_symbol")

    assert result.target.resolved_node_id is None
    assert result.upstream == () and result.downstream == ()
    assert result.risk_level == "unknown"
    assert result.risk_factors == ()
    assert _gap_kinds(result) == {"entry_unresolved"}


def test_stale_parser_version_reports_stale_index_gap() -> None:
    chunks = [
        _chunk("c_new", "src/new.py", "fresh"),
        _chunk("c_old", "src/old.py", "legacy", parser_version="python-ast-v1"),
    ]
    result = _preview(chunks, "fresh")

    gaps = [g for g in result.gaps if g.kind == "stale_index"]
    assert len(gaps) == 1
    assert "refresh" in gaps[0].detail


def test_unresolved_internal_reference_reports_aggregate_gap() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute"),
        _chunk(
            "c_lazy",
            "src/lazy.py",
            "lazy",
            references=(_ref("compute"), _ref("missing_helper")),
            imports=(ImportEvidence("missing_helper", ".gone", "missing_helper", 2, 2),),
        ),
    ]
    result = _preview(chunks, "compute")

    gaps = [g for g in result.gaps if g.kind == "unresolved_references"]
    assert len(gaps) == 1
    assert "missing_helper" in gaps[0].detail


def test_unmatched_endpoint_call_reports_aggregate_gap() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute"),
        _chunk(
            "c_client",
            "client/src/api.js",
            "pushLog",
            endpoints=(
                EndpointEvidence("calls", "POST", "/api/v1/logs", "/api/v1/logs", 3, 3),
            ),
            parser_version="js-ts-treesitter-v2",
        ),
    ]
    result = _preview(chunks, "compute")

    gaps = [g for g in result.gaps if g.kind == "endpoint_unmatched"]
    assert len(gaps) == 1
    assert "/api/v1/logs" in gaps[0].detail


def test_extraction_truncated_involved_chunk_reports_gap() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute"),
        _chunk(
            "c_caller",
            "src/api.py",
            "handler",
            references=(_ref("compute"),),
            extraction_truncated=True,
        ),
        # Truncated but uninvolved: must NOT produce a gap.
        _chunk("c_faraway", "src/other.py", "unrelated", extraction_truncated=True),
    ]
    result = _preview(chunks, "compute")

    gaps = [g for g in result.gaps if g.kind == "extraction_truncated"]
    assert [g.node_id for g in gaps] == ["c_caller"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_storage_return_order_never_affects_result() -> None:
    chunks = [
        _chunk("c_caller", "src/api.py", "handler", references=(_ref("compute"), _ref("save"))),
        _chunk("c_target", "src/calc.py", "compute", references=(_ref("load"),)),
        _chunk("c_save_a", "src/a.py", "save"),
        _chunk("c_save_b", "src/b.py", "save"),
        _chunk("c_load", "src/c.py", "load"),
        _chunk("c_test", "tests/test_calc.py", "test_compute", references=(_ref("compute"),)),
    ]
    forward = _preview(list(chunks), "compute")
    backward = _preview(list(reversed(chunks)), "compute")

    assert forward == backward
