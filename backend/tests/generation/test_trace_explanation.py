"""Offline unit tests for TraceExplanationService (TRACE-005).

The explanation may only narrate the deterministic static trace, citing
valid [S#] markers; every invalid outcome must discard the explanation.
"""

from __future__ import annotations

from sourcetrace.generation.client import GenerationMessage
from sourcetrace.generation.trace_explanation import (
    TraceExplanationService,
    build_trace_explanation_prompt,
)
from sourcetrace.retrieval.trace import (
    FlowTraceResult,
    TraceEdge,
    TraceEntry,
    TraceNode,
)


def _node(node_id: str, symbol: str, path: str = "src/app.py") -> TraceNode:
    return TraceNode(
        node_id=node_id,
        relative_path=path,
        symbol_name=symbol,
        symbol_type="function",
        start_line=1,
        end_line=10,
        snippet=f"def {symbol}(): ...",
    )


def _result(step_count: int = 2) -> FlowTraceResult:
    nodes = tuple(_node(f"c_{i}", f"sym_{i}") for i in range(step_count))
    edges = tuple(
        TraceEdge(
            from_node_id=f"c_{i}",
            to_node_id=f"c_{i + 1}",
            kind="call",
            confidence="medium",
            evidence_label=f"sym_{i + 1}",
            evidence_line_start=5,
            evidence_line_end=5,
        )
        for i in range(step_count - 1)
    )
    return FlowTraceResult(
        entry=TraceEntry("sym_0", "c_0", ("c_0",)),
        nodes=nodes,
        edges=edges,
        steps=tuple(f"c_{i}" for i in range(step_count)),
    )


class _FakeProvider:
    def __init__(self, output: str | Exception) -> None:
        self._output = output
        self.calls: list[tuple[GenerationMessage, ...]] = []

    def generate(self, messages) -> str:
        self.calls.append(tuple(messages))
        if isinstance(self._output, Exception):
            raise self._output
        return self._output


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_prompt_contains_only_step_markers_and_isolation_rules() -> None:
    messages = build_trace_explanation_prompt(_result(2))

    assert messages[0].role == "system"
    assert "UNTRUSTED DATA" in messages[0].content
    user = messages[1].content
    assert "[S1]" in user and "[S2]" in user
    assert "[S3]" not in user
    assert "sym_0" in user and "sym_1" in user
    assert "reached from: (trace entry point)" in user
    assert "via call evidence 'sym_1'" in user


def test_prompt_is_bounded() -> None:
    messages = build_trace_explanation_prompt(_result(2), max_prompt_chars=200)
    assert len(messages[1].content) <= 200


# ---------------------------------------------------------------------------
# Marker validation
# ---------------------------------------------------------------------------


def test_valid_explanation_returns_text_and_sorted_unique_cited_steps() -> None:
    provider = _FakeProvider("Flow starts at [S1], which calls [S2]. See [S1] again.")
    service = TraceExplanationService(provider)

    explained = service.explain(_result(2))

    assert explained is not None
    text, cited = explained
    assert text.startswith("Flow starts")
    assert cited == (1, 2)
    assert len(provider.calls) == 1


def test_out_of_range_marker_discards_explanation() -> None:
    service = TraceExplanationService(_FakeProvider("Step [S1] then invented [S99]."))
    assert service.explain(_result(2)) is None


def test_zero_marker_explanation_is_discarded() -> None:
    service = TraceExplanationService(_FakeProvider("A plausible story that cites nothing at all."))
    assert service.explain(_result(2)) is None


def test_marker_below_one_is_discarded() -> None:
    service = TraceExplanationService(_FakeProvider("Starts at [S0] then [S1]."))
    assert service.explain(_result(2)) is None


def test_empty_or_non_string_output_is_discarded() -> None:
    assert TraceExplanationService(_FakeProvider("   ")).explain(_result(2)) is None
    assert TraceExplanationService(_FakeProvider("")).explain(_result(2)) is None


def test_provider_exception_is_contained() -> None:
    service = TraceExplanationService(_FakeProvider(RuntimeError("provider down")))
    assert service.explain(_result(2)) is None


def test_empty_trace_is_never_sent_to_the_provider() -> None:
    provider = _FakeProvider("[S1]")
    service = TraceExplanationService(provider)
    empty = FlowTraceResult(entry=TraceEntry("x", None, ()))

    assert service.explain(empty) is None
    assert provider.calls == []


def test_long_output_is_truncated_but_markers_still_validated() -> None:
    long_text = "[S1] " + ("x" * 10000)
    service = TraceExplanationService(_FakeProvider(long_text))

    explained = service.explain(_result(2))

    assert explained is not None
    assert len(explained[0]) <= 4000
    assert explained[1] == (1,)
