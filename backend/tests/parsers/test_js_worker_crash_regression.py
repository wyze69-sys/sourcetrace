"""Regression test for the tree-sitter 0.26.0 native access-violation crash (TRACE-000).

Under tree-sitter 0.26.0 the isolated js_parser_worker subprocess died with
exit code 3221225477 (0xC0000005) on this fixture, and parsing degraded to a
single "<module>" fallback chunk. The fixture must always produce its real
symbol chunks; a fallback here means the native crash regressed.
"""

from pathlib import Path

from sourcetrace.parsers.javascript_ast import parse_javascript_source

_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "parser_fixtures"
    / "js_worker_av_regression.js"
)


def test_av_regression_fixture_yields_all_real_symbols() -> None:
    source = _FIXTURE.read_text(encoding="utf-8")

    chunks = parse_javascript_source(
        source=source,
        relative_path="src/utils/js_worker_av_regression.js",
        repository_id="repo_trace000",
        owner_session_id="sess_trace000",
    )

    names = {c.symbol_name for c in chunks}
    assert "<module>" not in names, "worker crashed and fell back to a module chunk"
    assert names == {f"calculateXpBreakdown{i}" for i in range(8)}
    assert len(chunks) == 8
    assert all(c.symbol_type == "function" for c in chunks)
    assert all(c.language == "javascript" for c in chunks)
