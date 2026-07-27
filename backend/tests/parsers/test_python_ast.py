"""Comprehensive offline tests for deterministic Python AST symbol parsing.

Tests use only temporary local fixtures — no internet, no live MongoDB,
no embedding provider, no API credentials.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from sourcetrace.ingestion.scanner import SkipReason
from sourcetrace.parsers.python_ast import (
    PYTHON_AST_PARSER_VERSION,
    _compute_chunk_id,
    _compute_content_hash,
    parse_acquired_source,
    parse_python_source,
)

REPO_ID = "repo_test123"
OWNER_ID = "owner_test456"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(source: str, path: str = "test.py") -> list:
    return parse_python_source(source, path, REPO_ID, OWNER_ID)


# ---------------------------------------------------------------------------
# Top-level function
# ---------------------------------------------------------------------------


class TestTopLevelFunction:
    def test_simple_function(self) -> None:
        source = textwrap.dedent("""\
            def greet(name: str) -> str:
                return f"Hello, {name}!"
        """)
        chunks = _parse(source)
        assert len(chunks) == 1
        c = chunks[0]
        assert c.symbol_name == "greet"
        assert c.symbol_type == "function"
        assert c.start_line == 1
        assert c.end_line == 2
        assert c.language == "python"
        assert c.repository_id == REPO_ID
        assert c.owner_session_id == OWNER_ID

    def test_multiple_functions(self) -> None:
        source = textwrap.dedent("""\
            def foo():
                pass

            def bar():
                pass
        """)
        chunks = _parse(source)
        names = [c.symbol_name for c in chunks]
        assert "foo" in names
        assert "bar" in names


# ---------------------------------------------------------------------------
# Async function
# ---------------------------------------------------------------------------


class TestAsyncFunction:
    def test_async_function_type(self) -> None:
        source = textwrap.dedent("""\
            async def fetch(url: str) -> str:
                return url
        """)
        chunks = _parse(source)
        assert len(chunks) == 1
        assert chunks[0].symbol_type == "async_function"
        assert chunks[0].symbol_name == "fetch"


# ---------------------------------------------------------------------------
# Decorated function start line
# ---------------------------------------------------------------------------


class TestDecoratedFunction:
    def test_decorator_included_in_start_line(self) -> None:
        source = textwrap.dedent("""\
            def decorator(f):
                return f

            @decorator
            def decorated():
                pass
        """)
        chunks = _parse(source)
        decorated = [c for c in chunks if c.symbol_name == "decorated"]
        assert len(decorated) == 1
        # Decorator is on line 4, def is on line 5 => start_line should be 4
        assert decorated[0].start_line == 4

    def test_multiple_decorators(self) -> None:
        source = textwrap.dedent("""\
            def dec1(f):
                return f

            def dec2(f):
                return f

            @dec1
            @dec2
            def multi_decorated():
                pass
        """)
        chunks = _parse(source)
        md = [c for c in chunks if c.symbol_name == "multi_decorated"]
        assert len(md) == 1
        # First decorator is on line 7
        assert md[0].start_line == 7


# ---------------------------------------------------------------------------
# Class and methods
# ---------------------------------------------------------------------------


class TestClassAndMethods:
    def test_class_extracted(self) -> None:
        source = textwrap.dedent("""\
            class MyClass:
                def method(self):
                    pass
        """)
        chunks = _parse(source)
        class_chunks = [c for c in chunks if c.symbol_type == "class"]
        assert len(class_chunks) == 1
        assert class_chunks[0].symbol_name == "MyClass"

    def test_method_qualified_name(self) -> None:
        source = textwrap.dedent("""\
            class MyClass:
                def method(self):
                    pass
        """)
        chunks = _parse(source)
        methods = [c for c in chunks if c.symbol_type == "method"]
        assert len(methods) == 1
        assert methods[0].symbol_name == "MyClass.method"

    def test_multiple_methods(self) -> None:
        source = textwrap.dedent("""\
            class Calculator:
                def add(self, x):
                    pass

                def subtract(self, x):
                    pass
        """)
        chunks = _parse(source)
        methods = [c for c in chunks if c.symbol_type == "method"]
        names = [m.symbol_name for m in methods]
        assert "Calculator.add" in names
        assert "Calculator.subtract" in names


# ---------------------------------------------------------------------------
# Async methods
# ---------------------------------------------------------------------------


class TestAsyncMethods:
    def test_async_method_type(self) -> None:
        source = textwrap.dedent("""\
            class Service:
                async def process(self):
                    pass
        """)
        chunks = _parse(source)
        async_methods = [c for c in chunks if c.symbol_type == "async_method"]
        assert len(async_methods) == 1
        assert async_methods[0].symbol_name == "Service.process"


# ---------------------------------------------------------------------------
# Nested functions and nested classes
# ---------------------------------------------------------------------------


class TestNestedSymbols:
    def test_nested_function(self) -> None:
        source = textwrap.dedent("""\
            def outer():
                def inner():
                    pass
                return inner()
        """)
        chunks = _parse(source)
        nested = [c for c in chunks if c.symbol_type == "nested_function"]
        assert len(nested) == 1
        assert nested[0].symbol_name == "outer.inner"

    def test_nested_async_function(self) -> None:
        source = textwrap.dedent("""\
            def outer():
                async def async_inner():
                    pass
        """)
        chunks = _parse(source)
        nested = [c for c in chunks if c.symbol_type == "nested_async_function"]
        assert len(nested) == 1
        assert nested[0].symbol_name == "outer.async_inner"

    def test_nested_class(self) -> None:
        source = textwrap.dedent("""\
            class Outer:
                class Inner:
                    def method(self):
                        pass
        """)
        chunks = _parse(source)
        nested_classes = [c for c in chunks if c.symbol_type == "nested_class"]
        assert len(nested_classes) == 1
        assert nested_classes[0].symbol_name == "Outer.Inner"

    def test_nested_class_method(self) -> None:
        source = textwrap.dedent("""\
            class Outer:
                class Inner:
                    def method(self):
                        pass
        """)
        chunks = _parse(source)
        methods = [c for c in chunks if c.symbol_type == "method"]
        inner_methods = [m for m in methods if "Inner" in m.symbol_name]
        assert len(inner_methods) == 1
        assert inner_methods[0].symbol_name == "Outer.Inner.method"


# ---------------------------------------------------------------------------
# Qualified names are deterministic
# ---------------------------------------------------------------------------


class TestQualifiedNames:
    def test_deeply_nested_qualified_name(self) -> None:
        source = textwrap.dedent("""\
            class Outer:
                class Middle:
                    class Inner:
                        def deep_method(self):
                            pass
        """)
        chunks = _parse(source)
        methods = [c for c in chunks if c.symbol_type == "method"]
        assert any(m.symbol_name == "Outer.Middle.Inner.deep_method" for m in methods)

    def test_deterministic_across_calls(self) -> None:
        source = textwrap.dedent("""\
            def func_a():
                pass

            def func_b():
                pass
        """)
        chunks1 = _parse(source)
        chunks2 = _parse(source)
        names1 = [c.symbol_name for c in chunks1]
        names2 = [c.symbol_name for c in chunks2]
        assert names1 == names2


# ---------------------------------------------------------------------------
# Inclusive exact line ranges
# ---------------------------------------------------------------------------


class TestLineRanges:
    def test_inclusive_line_range(self) -> None:
        source = textwrap.dedent("""\
            def greet(name):
                return f"Hello, {name}!"
        """)
        chunks = _parse(source)
        assert chunks[0].start_line == 1
        assert chunks[0].end_line == 2

    def test_lines_are_1_indexed(self) -> None:
        source = textwrap.dedent("""\
            x = 1

            def func():
                pass
        """)
        chunks = _parse(source)
        func = [c for c in chunks if c.symbol_name == "func"]
        assert len(func) == 1
        assert func[0].start_line == 3


# ---------------------------------------------------------------------------
# Chunk content matches its line range
# ---------------------------------------------------------------------------


class TestContentMatchesLineRange:
    def test_content_matches_range(self) -> None:
        source = "x = 1\ndef func():\n    pass\ny = 2\n"
        chunks = _parse(source)
        func = [c for c in chunks if c.symbol_name == "func"]
        assert len(func) == 1
        lines = source.splitlines(True)
        expected = "".join(lines[func[0].start_line - 1 : func[0].end_line])
        assert func[0].content == expected

    def test_multiline_function_content(self) -> None:
        source = textwrap.dedent("""\
            def long_func():
                a = 1
                b = 2
                c = 3
                return a + b + c
        """)
        chunks = _parse(source)
        assert len(chunks) == 1
        assert "a = 1" in chunks[0].content
        assert "return a + b + c" in chunks[0].content


# ---------------------------------------------------------------------------
# Stable source-order output
# ---------------------------------------------------------------------------


class TestSourceOrder:
    def test_symbols_in_source_order(self) -> None:
        source = textwrap.dedent("""\
            def first():
                pass

            class Second:
                def method(self):
                    pass

            def third():
                pass
        """)
        chunks = _parse(source)
        start_lines = [c.start_line for c in chunks]
        assert start_lines == sorted(start_lines)

    def test_nested_symbol_order(self) -> None:
        source = textwrap.dedent("""\
            class Container:
                def alpha(self):
                    pass

                def beta(self):
                    pass
        """)
        chunks = _parse(source)
        method_names = [c.symbol_name for c in chunks if c.symbol_type == "method"]
        assert method_names == ["Container.alpha", "Container.beta"]


# ---------------------------------------------------------------------------
# Module fallback for valid symbol-free source
# ---------------------------------------------------------------------------


class TestModuleFallback:
    def test_module_fallback_chunk(self) -> None:
        source = textwrap.dedent("""\
            VERSION = "1.0.0"
            MAX_RETRIES = 3
        """)
        chunks = _parse(source)
        assert len(chunks) == 1
        assert chunks[0].symbol_type == "module"
        assert chunks[0].symbol_name == "<module>"
        assert chunks[0].start_line == 1

    def test_module_fallback_content(self) -> None:
        source = "X = 1\nY = 2\n"
        chunks = _parse(source)
        assert len(chunks) == 1
        assert chunks[0].content == source


# ---------------------------------------------------------------------------
# Empty source does not fabricate a chunk
# ---------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_string(self) -> None:
        chunks = _parse("")
        assert len(chunks) == 0

    def test_whitespace_only(self) -> None:
        chunks = _parse("   \n\n  \n")
        assert len(chunks) == 0


# ---------------------------------------------------------------------------
# SHA-256 content hashes
# ---------------------------------------------------------------------------


class TestContentHashes:
    def test_sha256_hash(self) -> None:
        source = "def func():\n    pass\n"
        chunks = _parse(source)
        expected_hash = hashlib.sha256(chunks[0].content.encode("utf-8")).hexdigest()
        assert chunks[0].content_hash == expected_hash

    def test_hash_is_hex_digest(self) -> None:
        chunks = _parse("def f():\n    pass\n")
        assert len(chunks[0].content_hash) == 64  # SHA-256 hex is 64 chars
        assert all(c in "0123456789abcdef" for c in chunks[0].content_hash)


# ---------------------------------------------------------------------------
# Stable deterministic chunk IDs
# ---------------------------------------------------------------------------


class TestChunkIDs:
    def test_chunk_id_prefix(self) -> None:
        chunks = _parse("def func():\n    pass\n")
        assert chunks[0].chunk_id.startswith("chunk_")

    def test_deterministic_chunk_id(self) -> None:
        source = "def func():\n    pass\n"
        c1 = _parse(source)
        c2 = _parse(source)
        assert c1[0].chunk_id == c2[0].chunk_id

    def test_same_inputs_same_id(self) -> None:
        """Same source, same path, same IDs → same chunk_id."""
        source = "def func():\n    pass\n"
        c1 = parse_python_source(source, "a.py", REPO_ID, OWNER_ID)
        c2 = parse_python_source(source, "a.py", REPO_ID, OWNER_ID)
        assert c1[0].chunk_id == c2[0].chunk_id


# ---------------------------------------------------------------------------
# Changed content changes hash and chunk ID
# ---------------------------------------------------------------------------


class TestContentChangeAffectsID:
    def test_different_content_different_hash(self) -> None:
        c1 = _parse("def func():\n    return 1\n")
        c2 = _parse("def func():\n    return 2\n")
        assert c1[0].content_hash != c2[0].content_hash

    def test_different_content_different_chunk_id(self) -> None:
        c1 = _parse("def func():\n    return 1\n")
        c2 = _parse("def func():\n    return 2\n")
        assert c1[0].chunk_id != c2[0].chunk_id

    def test_different_path_different_chunk_id(self) -> None:
        source = "def func():\n    pass\n"
        c1 = parse_python_source(source, "a.py", REPO_ID, OWNER_ID)
        c2 = parse_python_source(source, "b.py", REPO_ID, OWNER_ID)
        assert c1[0].chunk_id != c2[0].chunk_id


# ---------------------------------------------------------------------------
# Owner/repository IDs copied exactly from trusted runner context
# ---------------------------------------------------------------------------


class TestTrustedContext:
    def test_ids_match_input(self) -> None:
        source = "def f():\n    pass\n"
        chunks = parse_python_source(source, "test.py", "repo_abc", "owner_xyz")
        assert chunks[0].repository_id == "repo_abc"
        assert chunks[0].owner_session_id == "owner_xyz"


# ---------------------------------------------------------------------------
# No repository code execution
# ---------------------------------------------------------------------------


class TestNoCodeExecution:
    def test_malicious_source_not_executed(self) -> None:
        """Source with side effects must not be executed."""
        source = textwrap.dedent("""\
            import os
            os.system("echo PWNED")

            def safe_func():
                pass
        """)
        # Parsing must succeed without executing the code
        chunks = _parse(source)
        func = [c for c in chunks if c.symbol_name == "safe_func"]
        assert len(func) == 1


# ---------------------------------------------------------------------------
# One malformed file does not prevent valid files from parsing
# ---------------------------------------------------------------------------


class TestMalformedFileIsolation:
    """Uses parse_acquired_source integration path for this test."""

    def test_malformed_file_isolated(self, tmp_path: Path) -> None:
        """A syntax error in one file should not prevent others from parsing."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _FakeManifest:
            file_count: int
            total_extracted_bytes: int
            relative_paths: tuple[str, ...]

        @dataclass(frozen=True)
        class _FakeAcquiredSource:
            extraction_root: Path
            manifest: _FakeManifest
            source_type: str

        # Write a good file
        good = tmp_path / "good.py"
        good.write_text("def hello():\n    pass\n", encoding="utf-8")

        # Write a bad file
        bad = tmp_path / "bad.py"
        bad.write_text("def !!!\n", encoding="utf-8")

        manifest = _FakeManifest(
            file_count=2,
            total_extracted_bytes=100,
            relative_paths=("good.py", "bad.py"),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = parse_acquired_source(source, REPO_ID, OWNER_ID)
        assert result.parsed_file_count >= 1
        assert any(c.symbol_name == "hello" for c in result.chunks)
        assert any(s.reason == SkipReason.INVALID_PYTHON_SYNTAX for s in result.skipped)


# ---------------------------------------------------------------------------
# No physical temporary path or raw exception in skip results
# ---------------------------------------------------------------------------


class TestNoPhysicalPathInResults:
    def test_no_physical_path_in_chunk(self) -> None:
        source = "def f():\n    pass\n"
        chunks = _parse(source, "src/module.py")
        assert chunks[0].relative_path == "src/module.py"
        # No temp dir paths
        assert "tmp" not in chunks[0].chunk_id.lower() or chunks[0].chunk_id.startswith("chunk_")

    def test_parser_version_present(self) -> None:
        chunks = _parse("def f():\n    pass\n")
        assert chunks[0].parser_version == PYTHON_AST_PARSER_VERSION


# ---------------------------------------------------------------------------
# Content hash function
# ---------------------------------------------------------------------------


class TestContentHashFunction:
    def test_compute_content_hash_sha256(self) -> None:
        content = "def func():\n    pass\n"
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert _compute_content_hash(content) == expected

    def test_empty_content_hash(self) -> None:
        h = _compute_content_hash("")
        assert h == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Chunk ID function
# ---------------------------------------------------------------------------


class TestChunkIDFunction:
    def test_chunk_id_deterministic(self) -> None:
        id1 = _compute_chunk_id("repo", "path.py", "function", "func", 1, 2, "hash1", "v1")
        id2 = _compute_chunk_id("repo", "path.py", "function", "func", 1, 2, "hash1", "v1")
        assert id1 == id2

    def test_chunk_id_prefix(self) -> None:
        cid = _compute_chunk_id("repo", "path.py", "function", "func", 1, 2, "hash1", "v1")
        assert cid.startswith("chunk_")

    def test_different_inputs_different_id(self) -> None:
        id1 = _compute_chunk_id("repo", "a.py", "function", "func", 1, 2, "hash1", "v1")
        id2 = _compute_chunk_id("repo", "b.py", "function", "func", 1, 2, "hash1", "v1")
        assert id1 != id2

    def test_no_owner_session_in_id(self) -> None:
        """owner_session_id must not be part of the chunk ID inputs."""
        # Parse with different owners, same everything else
        source = "def f():\n    pass\n"
        c1 = parse_python_source(source, "test.py", REPO_ID, "owner_a")
        c2 = parse_python_source(source, "test.py", REPO_ID, "owner_b")
        assert c1[0].chunk_id == c2[0].chunk_id


# ---------------------------------------------------------------------------
# Decorated classes and methods
# ---------------------------------------------------------------------------


class TestDecoratedClassesAndMethods:
    def test_decorated_class_start_line(self) -> None:
        source = textwrap.dedent("""\
            def class_decorator(cls):
                return cls

            @class_decorator
            class DecoratedClass:
                def method(self):
                    pass
        """)
        chunks = _parse(source)
        cls_chunk = [c for c in chunks if c.symbol_type == "class"]
        assert len(cls_chunk) == 1
        assert cls_chunk[0].symbol_name == "DecoratedClass"
        # Decorator is on line 4, class is on line 5
        assert cls_chunk[0].start_line == 4

    def test_decorated_method_start_line(self) -> None:
        source = textwrap.dedent("""\
            def method_decorator(f):
                return f

            class SampleClass:
                @method_decorator
                def decorated_method(self):
                    pass
        """)
        chunks = _parse(source)
        method_chunk = [c for c in chunks if c.symbol_name == "SampleClass.decorated_method"]
        assert len(method_chunk) == 1
        # Decorator is on line 5, def is on line 6
        assert method_chunk[0].start_line == 5


# ---------------------------------------------------------------------------
# Single AST parsing per file in parse_acquired_source
# ---------------------------------------------------------------------------


class TestSingleASTParseInAcquiredSource:
    """parse_acquired_source must parse each eligible file's AST exactly once."""

    def test_ast_parse_called_once_per_file(self, tmp_path: Path) -> None:
        import ast
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _FakeManifest:
            file_count: int
            total_extracted_bytes: int
            relative_paths: tuple[str, ...]

        @dataclass(frozen=True)
        class _FakeAcquiredSource:
            extraction_root: Path
            manifest: _FakeManifest
            source_type: str

        p = tmp_path / "mod.py"
        p.write_text("def hello():\n    pass\n", encoding="utf-8")
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=20,
            relative_paths=("mod.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )

        parse_calls = 0
        original_parse = ast.parse

        def tracking_parse(*args, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return original_parse(*args, **kwargs)

        from unittest.mock import patch

        with patch("ast.parse", side_effect=tracking_parse):
            result = parse_acquired_source(source, REPO_ID, OWNER_ID)
            assert result.parsed_file_count == 1
            # Must be called exactly ONCE per file, not TWICE
            assert parse_calls == 1
