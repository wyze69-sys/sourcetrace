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
from sourcetrace.models.domain import (
    EndpointEvidence,
    ImportEvidence,
    ParsedCodeChunk,
    ReferenceEvidence,
)
from sourcetrace.parsers.javascript_ast import (
    _make_module_chunk,
    parse_javascript_source,
)

PYTHON_AST_PARSER_VERSION: str = "python-ast-v2"

# Per-category cap on flow evidence items stored per chunk.
FLOW_EVIDENCE_MAX_ITEMS: int = 100

_HTTP_ENDPOINT_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options"}
)

_SYMBOL_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

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
    node: ast.AST


@dataclass(frozen=True, slots=True)
class _FlowEvidence:
    references: tuple[ReferenceEvidence, ...]
    imports: tuple[ImportEvidence, ...]
    endpoints: tuple[EndpointEvidence, ...]
    truncated: bool


def _node_lines(node: ast.AST) -> tuple[int, int]:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", None) or start
    return start, max(start, end)


def _normalize_endpoint_path(path_literal: str) -> str:
    """Reduce a path literal to a comparable form: host stripped, params as {}."""
    path = path_literal
    if path.startswith(("http://", "https://")):
        after_scheme = path.split("://", 1)[1]
        slash = after_scheme.find("/")
        path = after_scheme[slash:] if slash >= 0 else "/"
    path = path.split("?", 1)[0]
    segments = []
    for seg in path.split("/"):
        is_param = (
            (seg.startswith("{") and seg.endswith("}"))
            or (seg.startswith("<") and seg.endswith(">"))
            or seg.startswith(":")
        )
        segments.append("{}" if is_param and len(seg) > 1 else seg)
    return "/".join(segments)


def _dotted_call_name(func: ast.expr) -> tuple[str, str] | None:
    """Return (local_name, kind) for a call target, or None when underivable."""
    if isinstance(func, ast.Name):
        return func.id, "call"
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        current = func.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts)), "attribute_call"
        # Base is not a plain name (call result, subscript, ...): keep the
        # final attribute as evidence rather than guessing the chain.
        return func.attr, "attribute_call"
    return None


def _iter_scope(root: ast.AST):
    """Yield nodes in a symbol's direct scope without entering nested symbol defs.

    Nested function/class definitions produce their own chunks, so their bodies
    (and decorators) are owned by those chunks, not the enclosing one.
    """
    pending = list(ast.iter_child_nodes(root))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, _SYMBOL_NODE_TYPES):
            pending.extend(ast.iter_child_nodes(node))


def _import_evidence(node: ast.Import | ast.ImportFrom) -> list[ImportEvidence]:
    line_start, line_end = _node_lines(node)
    out: list[ImportEvidence] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".")[0]
            out.append(
                ImportEvidence(local_name, alias.name, alias.name, line_start, line_end)
            )
    else:
        source_module = "." * (node.level or 0) + (node.module or "")
        for alias in node.names:
            local_name = alias.asname or alias.name
            out.append(
                ImportEvidence(local_name, source_module, alias.name, line_start, line_end)
            )
    return out


def _decorator_endpoints(
    node: ast.AST,
) -> tuple[list[EndpointEvidence], set[int]]:
    """Extract declares-endpoints from decorators; also return consumed Call ids."""
    out: list[EndpointEvidence] = []
    consumed: set[int] = set()
    for dec in getattr(node, "decorator_list", []):
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            continue
        if not dec.args:
            continue
        first_arg = dec.args[0]
        if not (isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)):
            continue
        method_attr = dec.func.attr.casefold()
        path = first_arg.value
        line_start, line_end = _node_lines(dec)
        if method_attr in _HTTP_ENDPOINT_METHODS:
            consumed.add(id(dec))
            out.append(
                EndpointEvidence(
                    "declares",
                    method_attr.upper(),
                    path,
                    _normalize_endpoint_path(path),
                    line_start,
                    line_end,
                )
            )
        elif method_attr == "route":
            consumed.add(id(dec))
            methods = ["GET"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    literal_methods = [
                        elt.value
                        for elt in kw.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                    if literal_methods:
                        methods = literal_methods
            for method in methods:
                out.append(
                    EndpointEvidence(
                        "declares",
                        str(method).upper(),
                        path,
                        _normalize_endpoint_path(path),
                        line_start,
                        line_end,
                    )
                )
    return out, consumed


def _finalize_evidence(items: list, identity_key) -> tuple[tuple, bool]:
    """Dedupe by identity key (keeping earliest lines), sort, and cap."""
    best: dict = {}
    for item in items:
        key = identity_key(item)
        existing = best.get(key)
        if existing is None or (item.line_start, item.line_end) < (
            existing.line_start,
            existing.line_end,
        ):
            best[key] = item
    ordered = sorted(
        best.values(),
        key=lambda i: (i.line_start, i.line_end) + identity_key(i),
    )
    return tuple(ordered[:FLOW_EVIDENCE_MAX_ITEMS]), len(ordered) > FLOW_EVIDENCE_MAX_ITEMS


def _extract_flow_evidence(
    node: ast.AST,
    module_imports: list[ImportEvidence],
) -> _FlowEvidence:
    """Deterministically extract references, imports, and endpoints for one symbol."""
    references: list[ReferenceEvidence] = []
    imports: list[ImportEvidence] = list(module_imports)
    endpoints, consumed_decorators = _decorator_endpoints(node)

    for scoped in _iter_scope(node):
        if isinstance(scoped, (ast.Import, ast.ImportFrom)):
            imports.extend(_import_evidence(scoped))
        elif isinstance(scoped, ast.Call):
            named = _dotted_call_name(scoped.func)
            if named is not None:
                line_start, line_end = _node_lines(scoped)
                references.append(
                    ReferenceEvidence(named[0], named[1], line_start, line_end)
                )
            if (
                id(scoped) not in consumed_decorators
                and isinstance(scoped.func, ast.Attribute)
                and scoped.func.attr.casefold() in _HTTP_ENDPOINT_METHODS
                and scoped.args
                and isinstance(scoped.args[0], ast.Constant)
                and isinstance(scoped.args[0].value, str)
                and scoped.args[0].value.startswith(("/", "http://", "https://"))
            ):
                path = scoped.args[0].value
                line_start, line_end = _node_lines(scoped)
                endpoints.append(
                    EndpointEvidence(
                        "calls",
                        scoped.func.attr.upper(),
                        path,
                        _normalize_endpoint_path(path),
                        line_start,
                        line_end,
                    )
                )

    final_refs, refs_truncated = _finalize_evidence(
        references, lambda r: (r.local_name, r.kind)
    )
    final_imports, imports_truncated = _finalize_evidence(
        imports, lambda i: (i.local_name, i.source_module, i.imported_name)
    )
    final_endpoints, endpoints_truncated = _finalize_evidence(
        endpoints, lambda e: (e.kind, e.http_method, e.path_literal)
    )
    return _FlowEvidence(
        references=final_refs,
        imports=final_imports,
        endpoints=final_endpoints,
        truncated=refs_truncated or imports_truncated or endpoints_truncated,
    )


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
            self.symbols.append(
                _RawSymbol(qual_name, sym_type, start_line, end_line, node)
            )

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

    module_imports: list[ImportEvidence] = []
    for top_node in tree.body:
        if isinstance(top_node, (ast.Import, ast.ImportFrom)):
            module_imports.extend(_import_evidence(top_node))

    for sym in raw_symbols:
        # Extract content from source lines (0-indexed slice)
        content = "".join(source_lines[sym.start_line - 1 : sym.end_line])
        content_hash = _compute_content_hash(content)
        chunk_id = _compute_chunk_id(
            repository_id, relative_path, sym.symbol_type, sym.qualified_name,
            sym.start_line, sym.end_line, content_hash, PYTHON_AST_PARSER_VERSION
        )
        evidence = _extract_flow_evidence(sym.node, module_imports)
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
                references=evidence.references,
                imports=evidence.imports,
                endpoints=evidence.endpoints,
                extraction_truncated=evidence.truncated,
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
