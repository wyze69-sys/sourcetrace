"""
SourceTrace Python AST symbol parser.

This module processes Python source code extracted by the scanner, extracting functions, 
classes, and methods into deterministic chunks. It uses the builtin `ast` module and
avoids unsafe execution methods.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.scanner import (
    SkippedFile,
    SkipReason,
    scan_code_sources,
)
from sourcetrace.models.domain import ParsedCodeChunk
from sourcetrace.parsers.javascript_ast import (
    _make_module_chunk,
    parse_javascript_source,
)

PYTHON_AST_PARSER_VERSION: str = "python-ast-v1"

VALID_SYMBOL_TYPES: frozenset[str] = frozenset({
    "function",
    "async_function", 
    "class",
    "method",
    "async_method",
    "nested_function",
    "nested_async_function",
    "nested_class",
    "module",
})

@dataclass(frozen=True, slots=True)
class ParseResult:
    chunks: tuple[ParsedCodeChunk, ...]
    parsed_file_count: int
    skipped: tuple[SkippedFile, ...]

@dataclass(frozen=True, slots=True)
class _RawSymbol:
    qualified_name: str
    symbol_type: str
    start_line: int
    end_line: int


def _compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compute_chunk_id(
    repository_id: str,
    relative_path: str,
    symbol_type: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
    content_hash: str,
    parser_version: str,
) -> str:
    canonical_string = (
        f"{repository_id}|{relative_path}|{symbol_type}|{qualified_name}|"
        f"{start_line}|{end_line}|{content_hash}|{parser_version}"
    )
    digest = hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()
    return f"chunk_{digest}"


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, source_lines: list[str]) -> None:
        self.source_lines = source_lines
        self.symbols: list[_RawSymbol] = []
        self._context_stack: list[tuple[str, str]] = []  # (name, type)

    def _get_start_line(self, node: ast.AST) -> int:
        start_line = getattr(node, "lineno", 1)
        if hasattr(node, "decorator_list") and node.decorator_list:  # type: ignore
            dec_lines = [getattr(dec, "lineno", start_line) for dec in node.decorator_list]  # type: ignore
            start_line = min([start_line] + dec_lines)
        return start_line

    def _get_end_line(self, node: ast.AST) -> int:
        end_lineno = getattr(node, "end_lineno", None)
        if end_lineno is None:
            end_lineno = getattr(node, "lineno", 1)
        return end_lineno

    def _add_symbol(self, node: ast.AST, base_type: str, name: str) -> None:
        start_line = self._get_start_line(node)
        end_line = self._get_end_line(node)

        # Determine qualified name and specific type based on context
        if not self._context_stack:
            qual_name = name
            sym_type = base_type
        else:
            qual_name = ".".join([ctx[0] for ctx in self._context_stack] + [name])
            parent_type = self._context_stack[-1][1]
            if base_type == "class":
                sym_type = "nested_class"
            elif parent_type in ("class", "nested_class"):
                # Direct child of any class is a method
                sym_type = "method" if base_type == "function" else "async_method"
            else:
                # Child of a function/method is a nested function
                sym_type = (
                    "nested_function" if base_type == "function"
                    else "nested_async_function"
                )

        if 1 <= start_line <= end_line <= len(self.source_lines):
            self.symbols.append(_RawSymbol(qual_name, sym_type, start_line, end_line))

        self._context_stack.append((name, sym_type))
        self.generic_visit(node)
        self._context_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_symbol(node, "function", node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add_symbol(node, "async_function", node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_symbol(node, "class", node.name)


def _extract_symbols(tree: ast.Module, source_lines: list[str]) -> list[_RawSymbol]:
    visitor = _SymbolVisitor(source_lines)
    for node in tree.body:
        visitor.visit(node)
    
    # Sort by (start_line, end_line)
    return sorted(visitor.symbols, key=lambda s: (s.start_line, s.end_line))


def parse_python_source(
    source: str,
    relative_path: str,
    repository_id: str,
    owner_session_id: str,
    tree: ast.AST | None = None,
) -> list[ParsedCodeChunk]:
    if not source or not source.strip():
        return []

    if tree is None:
        tree = ast.parse(source, filename=relative_path)

    if not isinstance(tree, ast.Module):
        return []

    source_lines = source.splitlines(True)
    source_lines_stripped = source.splitlines()

    raw_symbols = _extract_symbols(tree, source_lines_stripped)
    
    chunks: list[ParsedCodeChunk] = []
    
    if not raw_symbols:
        return _make_module_chunk(
            source=source,
            relative_path=relative_path,
            language="python",
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            parser_version=PYTHON_AST_PARSER_VERSION,
        )

    for sym in raw_symbols:
        # Extract content from source lines (0-indexed slice)
        content = "".join(source_lines[sym.start_line - 1 : sym.end_line])
        content_hash = _compute_content_hash(content)
        chunk_id = _compute_chunk_id(
            repository_id, relative_path, sym.symbol_type, sym.qualified_name,
            sym.start_line, sym.end_line, content_hash, PYTHON_AST_PARSER_VERSION
        )
        chunks.append(
            ParsedCodeChunk(
                chunk_id=chunk_id,
                repository_id=repository_id,
                owner_session_id=owner_session_id,
                relative_path=relative_path,
                language="python",
                symbol_name=sym.qualified_name,
                symbol_type=sym.symbol_type,
                start_line=sym.start_line,
                end_line=sym.end_line,
                content=content,
                content_hash=content_hash,
                parser_version=PYTHON_AST_PARSER_VERSION,
            )
        )
        
    return chunks


def parse_acquired_source(
    acquired_source: AcquiredSource,
    repository_id: str,
    owner_session_id: str,
) -> ParseResult:
    scan_result = scan_code_sources(acquired_source)
    
    all_chunks: list[ParsedCodeChunk] = []
    all_skipped: list[SkippedFile] = list(scan_result.skipped)
    parsed_count = 0

    for eligible in scan_result.eligible_files:
        ext = Path(eligible.relative_path).suffix.casefold()
        if ext == ".py":
            try:
                tree = ast.parse(eligible.source, filename=eligible.relative_path)
            except SyntaxError:
                all_skipped.append(
                    SkippedFile(
                        relative_path=eligible.relative_path,
                        reason=SkipReason.INVALID_PYTHON_SYNTAX,
                    )
                )
                continue

            chunks = parse_python_source(
                source=eligible.source,
                relative_path=eligible.relative_path,
                repository_id=repository_id,
                owner_session_id=owner_session_id,
                tree=tree,
            )
            all_chunks.extend(chunks)
            parsed_count += 1
        elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            chunks = parse_javascript_source(
                source=eligible.source,
                relative_path=eligible.relative_path,
                repository_id=repository_id,
                owner_session_id=owner_session_id,
            )
            all_chunks.extend(chunks)
            parsed_count += 1

    return ParseResult(
        chunks=tuple(all_chunks),
        parsed_file_count=parsed_count,
        skipped=tuple(all_skipped),
    )
