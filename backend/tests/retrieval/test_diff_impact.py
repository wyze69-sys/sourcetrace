"""Offline unit tests for unified-diff parsing and diff impact previews
(IMPACT-003).

Same collaborator discipline as the symbol-impact tests: the fake chunk
repository implements only what the service is allowed to call, proving the
diff preview stays zero-token.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sourcetrace.models.domain import CodeChunk, ReferenceEvidence
from sourcetrace.retrieval.diff import DiffParseError, parse_unified_diff
from sourcetrace.retrieval.impact import ChangeImpactService

_OWNER = "owner_diff"
_REPO = "repo_diff"
_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _chunk(
    chunk_id: str,
    relative_path: str,
    symbol_name: str,
    *,
    start_line: int = 1,
    end_line: int = 10,
    content: str | None = None,
    references: tuple[ReferenceEvidence, ...] = (),
) -> CodeChunk:
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id=_REPO,
        owner_session_id=_OWNER,
        relative_path=relative_path,
        language="python",
        symbol_name=symbol_name,
        symbol_type="function",
        start_line=start_line,
        end_line=end_line,
        content=content if content is not None else f"def {symbol_name}(): ...",
        content_hash=f"hash_{chunk_id}",
        parser_version="python-ast-v2",
        created_at=_NOW,
        references=references,
    )


def _ref(name: str, line: int = 5) -> ReferenceEvidence:
    return ReferenceEvidence(local_name=name, kind="call", line_start=line, line_end=line)


class _FakeChunkRepo:
    """Only list_by_repository: the diff preview needs no lexical search."""

    def __init__(self, chunks: list[CodeChunk]) -> None:
        self._chunks = chunks

    def list_by_repository(
        self, owner_session_id: str, repository_id: str, generation_id: str | None = None
    ) -> list[CodeChunk]:
        assert owner_session_id == _OWNER and repository_id == _REPO
        return list(self._chunks)


def _preview(chunks: list[CodeChunk], diff_text: str, max_depth: int | None = None):
    service = ChangeImpactService(_FakeChunkRepo(chunks))
    return service.preview_diff(_OWNER, _REPO, diff_text, max_depth=max_depth)


def _gap_kinds(result) -> set[str]:
    return {g.kind for g in result.gaps}


# ---------------------------------------------------------------------------
# Unified diff parsing
# ---------------------------------------------------------------------------


def test_parse_modification_yields_old_coordinates_and_samples() -> None:
    diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -12,3 +12,4 @@\n"
        " context line\n"
        "-old body line\n"
        "+new body line\n"
        "+added line\n"
        " trailing context\n"
    )
    files = parse_unified_diff(diff)

    assert len(files) == 1
    f = files[0]
    assert f.old_path == "src/calc.py"
    assert f.new_path == "src/calc.py"
    # Deleted line 13 exactly; additions anchor to the old line they displace.
    assert 13 in f.changed_old_lines
    assert (12, "context line") in f.old_line_samples
    assert (13, "old body line") in f.old_line_samples


def test_parse_appends_at_eof_anchor_to_last_old_line() -> None:
    diff = (
        "--- a/src/tail.py\n"
        "+++ b/src/tail.py\n"
        "@@ -8,2 +8,4 @@\n"
        " def existing():\n"
        "     return 1\n"
        "+\n"
        "+NEW_CONSTANT = 2\n"
    )
    files = parse_unified_diff(diff)

    # old range is 8..9; appended lines must anchor inside it, not beyond EOF.
    assert files[0].changed_old_lines == frozenset({9})


def test_parse_new_and_deleted_files() -> None:
    diff = (
        "--- /dev/null\n"
        "+++ b/src/brand_new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def created():\n"
        "+    return 1\n"
        "--- a/src/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-def removed():\n"
        "-    return 2\n"
    )
    files = parse_unified_diff(diff)

    assert files[0].old_path is None
    assert files[0].new_path == "src/brand_new.py"
    assert files[1].old_path == "src/gone.py"
    assert files[1].new_path is None
    assert files[1].changed_old_lines == frozenset({1, 2})


def test_parse_strips_prefixes_timestamps_and_backslash_markers() -> None:
    diff = (
        "--- a/pkg/mod.py\t2026-07-26 10:00:00\n"
        "+++ b/pkg/mod.py\t2026-07-26 10:05:00\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    files = parse_unified_diff(diff)

    assert files[0].old_path == "pkg/mod.py"
    assert files[0].changed_old_lines == frozenset({1})


def test_parse_rejects_non_diff_input() -> None:
    with pytest.raises(DiffParseError):
        parse_unified_diff("just some pasted prose, definitely not a diff")
    with pytest.raises(DiffParseError):
        parse_unified_diff("--- a/x.py\n+++ b/x.py\nno hunks here\n")


# ---------------------------------------------------------------------------
# Diff impact preview
# ---------------------------------------------------------------------------

_SIMPLE_DIFF = (
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -12,3 +12,3 @@\n"
    " def compute():\n"
    "-    return legacy()\n"
    "+    return modern()\n"
)


def test_diff_touching_a_chunk_seeds_impact_from_it() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute", start_line=10, end_line=20),
        _chunk("c_caller", "src/api.py", "handler", references=(_ref("compute"),)),
    ]
    result = _preview(chunks, _SIMPLE_DIFF)

    # Line 13 is deleted; the replacement '+' run anchors to the old line it
    # displaces (14), so both are reported as touched.
    assert [(t.node_id, t.changed_lines) for t in result.targets] == [("c_target", (13, 14))]
    assert [item.node_id for item in result.upstream] == ["c_caller"]
    assert result.upstream[0].distance == 1
    assert result.risk_level in ("low", "medium", "high")


def test_multi_file_diff_aggregates_targets_and_deduplicates_upstream() -> None:
    diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -12,1 +12,1 @@\n"
        "-x\n"
        "+y\n"
        "--- a/src/store.py\n"
        "+++ b/src/store.py\n"
        "@@ -3,1 +3,1 @@\n"
        "-a\n"
        "+b\n"
    )
    chunks = [
        _chunk("c_calc", "src/calc.py", "compute", start_line=10, end_line=20),
        _chunk("c_store", "src/store.py", "save", start_line=1, end_line=8),
        # One caller references BOTH changed symbols: it must appear once.
        _chunk(
            "c_caller",
            "src/api.py",
            "handler",
            references=(_ref("compute", line=4), _ref("save", line=5)),
        ),
    ]
    result = _preview(chunks, diff)

    assert [t.node_id for t in result.targets] == ["c_calc", "c_store"]
    assert [item.node_id for item in result.upstream] == ["c_caller"]
    # Targets are seeds: they never appear in their own impact lists.
    assert not {"c_calc", "c_store"} & {i.node_id for i in result.upstream}
    assert not {"c_calc", "c_store"} & {i.node_id for i in result.downstream}


def test_diff_between_targets_does_not_report_them_as_impact() -> None:
    # compute (changed) calls save (also changed): the edge between two
    # seeds must not resurface either as upstream/downstream of the other.
    diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -12,1 +12,1 @@\n"
        "-x\n"
        "+y\n"
        "--- a/src/store.py\n"
        "+++ b/src/store.py\n"
        "@@ -3,1 +3,1 @@\n"
        "-a\n"
        "+b\n"
    )
    chunks = [
        _chunk(
            "c_calc",
            "src/calc.py",
            "compute",
            start_line=10,
            end_line=20,
            references=(_ref("save", line=15),),
        ),
        _chunk("c_store", "src/store.py", "save", start_line=1, end_line=8),
    ]
    result = _preview(chunks, diff)

    assert [t.node_id for t in result.targets] == ["c_calc", "c_store"]
    assert result.upstream == ()
    assert result.downstream == ()


def test_unknown_diff_path_reports_diff_file_unmatched() -> None:
    chunks = [_chunk("c_only", "src/app.py", "main")]
    diff = "--- a/src/other_project.py\n+++ b/src/other_project.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    result = _preview(chunks, diff)

    assert result.targets == ()
    assert result.risk_level == "unknown"
    gaps = [g for g in result.gaps if g.kind == "diff_file_unmatched"]
    assert len(gaps) == 1
    assert "src/other_project.py" in gaps[0].detail


def test_new_file_in_diff_reports_unmatched_baseline_gap() -> None:
    chunks = [_chunk("c_only", "src/app.py", "main")]
    diff = "--- /dev/null\n+++ b/src/brand_new.py\n@@ -0,0 +1,1 @@\n+def created(): ...\n"
    result = _preview(chunks, diff)

    gaps = [g for g in result.gaps if g.kind == "diff_file_unmatched"]
    assert len(gaps) == 1
    assert "added by this diff" in gaps[0].detail


def test_changed_lines_outside_chunks_report_diff_lines_uncovered() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute", start_line=10, end_line=20),
    ]
    diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -12,1 +12,1 @@\n"
        "-inside\n"
        "+inside2\n"
        "@@ -30,1 +30,1 @@\n"
        "-module level constant\n"
        "+changed constant\n"
    )
    result = _preview(chunks, diff)

    assert [t.node_id for t in result.targets] == ["c_target"]
    gaps = [g for g in result.gaps if g.kind == "diff_lines_uncovered"]
    assert len(gaps) == 1
    assert "30" in gaps[0].detail


def test_mismatched_baseline_text_reports_diff_stale() -> None:
    chunks = [
        _chunk(
            "c_target",
            "src/calc.py",
            "compute",
            start_line=1,
            end_line=3,
            content="def compute():\n    value = 1\n    return value\n",
        ),
    ]
    diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def compute():\n"
        "-    value = 99\n"
        "+    value = 100\n"
        "     return value\n"
    )
    result = _preview(chunks, diff)

    # Line 2 in the index is "    value = 1", not "    value = 99".
    gaps = [g for g in result.gaps if g.kind == "diff_stale"]
    assert len(gaps) == 1
    assert "src/calc.py:2" in gaps[0].detail
    # Targets are still computed; the gap informs rather than aborts.
    assert [t.node_id for t in result.targets] == ["c_target"]


def test_matching_baseline_text_reports_no_diff_stale() -> None:
    chunks = [
        _chunk(
            "c_target",
            "src/calc.py",
            "compute",
            start_line=1,
            end_line=3,
            content="def compute():\n    value = 1\n    return value\n",
        ),
    ]
    diff = (
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def compute():\n"
        "-    value = 1\n"
        "+    value = 2\n"
        "     return value\n"
    )
    result = _preview(chunks, diff)

    assert "diff_stale" not in _gap_kinds(result)


def test_diff_path_with_extra_root_matches_by_unique_suffix() -> None:
    chunks = [_chunk("c_target", "src/calc.py", "compute", start_line=1, end_line=10)]
    diff = "--- a/myproject/src/calc.py\n+++ b/myproject/src/calc.py\n@@ -2,1 +2,1 @@\n-x\n+y\n"
    result = _preview(chunks, diff)

    assert [t.node_id for t in result.targets] == ["c_target"]
    assert "diff_file_unmatched" not in _gap_kinds(result)


def test_ambiguous_diff_path_is_reported_not_guessed() -> None:
    chunks = [
        _chunk("c_a", "app_a/utils/calc.py", "compute_a", start_line=1, end_line=10),
        _chunk("c_b", "app_b/utils/calc.py", "compute_b", start_line=1, end_line=10),
    ]
    diff = "--- a/utils/calc.py\n+++ b/utils/calc.py\n@@ -2,1 +2,1 @@\n-x\n+y\n"
    result = _preview(chunks, diff)

    assert result.targets == ()
    gaps = [g for g in result.gaps if g.kind == "diff_file_unmatched"]
    assert len(gaps) == 1
    assert "ambiguous" in gaps[0].detail


def test_invalid_diff_raises_parse_error() -> None:
    with pytest.raises(DiffParseError):
        _preview([_chunk("c", "src/app.py", "main")], "not a diff at all")


def test_storage_return_order_never_affects_result() -> None:
    chunks = [
        _chunk("c_target", "src/calc.py", "compute", start_line=10, end_line=20),
        _chunk("c_caller", "src/api.py", "handler", references=(_ref("compute"),)),
        _chunk("c_test", "tests/test_calc.py", "test_compute", references=(_ref("compute"),)),
    ]
    forward = _preview(list(chunks), _SIMPLE_DIFF)
    backward = _preview(list(reversed(chunks)), _SIMPLE_DIFF)

    assert forward == backward
