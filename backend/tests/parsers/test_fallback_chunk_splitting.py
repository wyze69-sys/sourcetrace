"""Offline unit tests for fallback chunk splitting."""

from sourcetrace.parsers.javascript_ast import (
    MAX_FALLBACK_CHUNK_CHARS,
    _make_module_chunk,
)
from sourcetrace.parsers.python_ast import parse_python_source


def test_fallback_chunk_splitting_under_limit():
    source = "const x = 1;\nconsole.log(x);\n"
    chunks = _make_module_chunk(
        source=source,
        relative_path="client/src/small.js",
        language="javascript",
        repository_id="repo_test",
        owner_session_id="session_test",
    )

    assert len(chunks) == 1
    assert chunks[0].content == source
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2
    assert len(chunks[0].content) <= MAX_FALLBACK_CHUNK_CHARS


def test_fallback_chunk_splitting_oversized_172k_source():
    # Build synthetic 172K character source with 3000 lines
    line = "const foo = 'bar'; // This is a typical line in a fallback module\n"
    lines = [line] * 3000
    source = "".join(lines)
    assert len(source) > 170000

    chunks = _make_module_chunk(
        source=source,
        relative_path="client/src/large_fallback.js",
        language="javascript",
        repository_id="repo_test",
        owner_session_id="session_test",
    )

    # 1. Multiple bounded chunks produced
    assert len(chunks) > 1

    # 2. Maximum chunk size bounded
    for c in chunks:
        assert len(c.content) <= MAX_FALLBACK_CHUNK_CHARS
        assert c.relative_path == "client/src/large_fallback.js"
        assert c.repository_id == "repo_test"
        assert c.owner_session_id == "session_test"
        assert c.symbol_type == "module"
        assert c.symbol_name == "<module>"

    # 3. Complete source coverage (concatenation matches original source exactly)
    reconstructed = "".join([c.content for c in chunks])
    assert reconstructed == source

    # 4. Accurate line ranges and ordering
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 3000
    for i in range(len(chunks) - 1):
        assert chunks[i + 1].start_line == chunks[i].end_line + 1

    # 5. Deterministic chunk IDs
    chunk_ids = [c.chunk_id for c in chunks]
    assert len(chunk_ids) == len(set(chunk_ids))  # all unique


def test_fallback_chunk_splitting_single_huge_line():
    huge_line = "x" * 10000 + "\n"
    chunks = _make_module_chunk(
        source=huge_line,
        relative_path="client/src/huge_line.js",
        language="javascript",
        repository_id="repo_test",
        owner_session_id="session_test",
    )

    assert len(chunks) == 3
    for c in chunks:
        assert len(c.content) <= MAX_FALLBACK_CHUNK_CHARS

    reconstructed = "".join([c.content for c in chunks])
    assert reconstructed == huge_line


def test_python_fallback_chunk_splitting():
    source = "x = 1\n" * 2000
    chunks = parse_python_source(
        source=source,
        relative_path="backend/large_module.py",
        repository_id="repo_test",
        owner_session_id="session_test",
    )

    for c in chunks:
        assert len(c.content) <= MAX_FALLBACK_CHUNK_CHARS

    reconstructed = "".join([c.content for c in chunks])
    assert reconstructed == source
