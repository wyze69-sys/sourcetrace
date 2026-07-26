"""Offline unit tests for ImpactExplanationService (IMPACT-004)."""

from __future__ import annotations

from sourcetrace.generation.impact_explanation import (
    MAX_EXPLANATION_CHARS,
    ImpactExplanationService,
    build_impact_explanation_prompt,
)
from sourcetrace.retrieval.impact import (
    ChangeImpactResult,
    DiffImpactResult,
    DiffTarget,
    ImpactItem,
    ImpactTarget,
)


def _item(
    node_id: str,
    symbol_name: str,
    *,
    distance: int = 1,
    confidence: str = "high",
) -> ImpactItem:
    return ImpactItem(
        node_id=node_id,
        relative_path=f"src/{symbol_name}.py",
        symbol_name=symbol_name,
        symbol_type="function",
        start_line=4,
        end_line=20,
        distance=distance,
        confidence=confidence,
        edge_kind="call",
        via_node_id="c_target",
        evidence_node_id=node_id,
        evidence_label="compute",
        evidence_line_start=9,
        evidence_line_end=9,
    )


def _symbol_result(upstream=(), downstream=()) -> ChangeImpactResult:
    return ChangeImpactResult(
        target=ImpactTarget("compute", "c_target", ("c_target",)),
        upstream=tuple(upstream),
        downstream=tuple(downstream),
        risk_level="medium",
    )


def _diff_result(targets=(), upstream=(), downstream=()) -> DiffImpactResult:
    return DiffImpactResult(
        targets=tuple(targets),
        upstream=tuple(upstream),
        downstream=tuple(downstream),
        risk_level="medium",
    )


def _diff_target(node_id: str, symbol: str) -> DiffTarget:
    return DiffTarget(
        node_id=node_id,
        relative_path=f"src/{symbol}.py",
        symbol_name=symbol,
        symbol_type="function",
        start_line=10,
        end_line=20,
        changed_lines=(13, 14),
    )


class _FakeProvider:
    def __init__(self, response) -> None:
        self._response = response
        self.received: list = []

    def generate(self, messages) -> str:
        self.received.append(messages)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def test_symbol_prompt_numbers_upstream_then_downstream() -> None:
    result = _symbol_result(
        upstream=[_item("c_up", "handler")], downstream=[_item("c_down", "store")]
    )
    system, user = build_impact_explanation_prompt(result)

    assert "UNTRUSTED DATA" in system.content
    assert "symbol query 'compute'" in user.content
    assert user.content.index("[S1]") < user.content.index("[S2]")
    s1_block = user.content.split("[S2]")[0]
    assert "handler" in s1_block and "upstream dependent" in s1_block
    assert "cited at line 9" in user.content
    assert "Risk level (computed statically): medium" in user.content


def test_diff_prompt_numbers_targets_before_impact_items() -> None:
    result = _diff_result(
        targets=[_diff_target("c_t", "compute")],
        upstream=[_item("c_up", "handler")],
    )
    _, user = build_impact_explanation_prompt(result)

    s1_block = user.content.split("[S2]")[0]
    assert "changed target" in s1_block
    assert "changed lines: 13, 14" in s1_block
    assert "handler" in user.content.split("[S2]")[1]


def test_prompt_is_deterministic() -> None:
    result = _symbol_result(upstream=[_item("c_up", "handler")])
    assert build_impact_explanation_prompt(result) == build_impact_explanation_prompt(
        result
    )


# ---------------------------------------------------------------------------
# Marker validation
# ---------------------------------------------------------------------------


def test_valid_markers_return_text_and_sorted_unique_steps() -> None:
    result = _symbol_result(
        upstream=[_item("c_up", "handler")], downstream=[_item("c_down", "store")]
    )
    service = ImpactExplanationService(
        _FakeProvider("Changing this breaks [S1]; it relies on [S2] and [S1].")
    )

    explained = service.explain(result)

    assert explained is not None
    text, cited = explained
    assert cited == (1, 2)
    assert text.startswith("Changing this breaks")


def test_out_of_range_marker_discards_explanation() -> None:
    result = _symbol_result(upstream=[_item("c_up", "handler")])
    service = ImpactExplanationService(_FakeProvider("Affects [S1] and [S9]."))
    assert service.explain(result) is None


def test_markerless_answer_is_discarded() -> None:
    result = _symbol_result(upstream=[_item("c_up", "handler")])
    service = ImpactExplanationService(_FakeProvider("This change is risky."))
    assert service.explain(result) is None


def test_empty_answer_and_provider_failure_are_discarded() -> None:
    result = _symbol_result(upstream=[_item("c_up", "handler")])
    assert ImpactExplanationService(_FakeProvider("   ")).explain(result) is None
    assert (
        ImpactExplanationService(_FakeProvider(RuntimeError("boom"))).explain(result)
        is None
    )


def test_zero_item_preview_never_calls_the_provider() -> None:
    provider = _FakeProvider("[S1]")
    service = ImpactExplanationService(provider)

    assert service.explain(_symbol_result()) is None
    assert service.explain(_diff_result()) is None
    assert provider.received == []


def test_diff_targets_alone_are_explainable() -> None:
    result = _diff_result(targets=[_diff_target("c_t", "compute")])
    service = ImpactExplanationService(_FakeProvider("Only [S1] changed."))

    explained = service.explain(result)

    assert explained is not None
    assert explained[1] == (1,)


def test_overlong_answer_is_truncated_before_validation() -> None:
    result = _symbol_result(upstream=[_item("c_up", "handler")])
    long_text = "[S1] " + "x" * (MAX_EXPLANATION_CHARS + 500)
    service = ImpactExplanationService(_FakeProvider(long_text))

    explained = service.explain(result)

    assert explained is not None
    assert len(explained[0]) == MAX_EXPLANATION_CHARS
