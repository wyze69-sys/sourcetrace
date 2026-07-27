"""SourceTrace JavaScript/TypeScript AST symbol parser.

This module processes JavaScript and TypeScript source files (.js, .jsx, .ts, .tsx, .mjs, .cjs)
extracted by the scanner into deterministic chunks using static tree-sitter AST analysis.

SAFETY: Native tree-sitter parser crashes (access violations / segfaults on Windows)
cannot be caught by Python try/except. To protect the indexing process, a subprocess-based
safe wrapper is used for all JS/TS parsing. The subprocess runs parse_javascript_source
inside an isolated child process; if the native parser crashes, only the child dies and
the main process falls back safely to a module-level chunk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tree_sitter
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts

from sourcetrace.models.domain import (
    EndpointEvidence,
    ImportEvidence,
    ParsedCodeChunk,
    ReferenceEvidence,
)
from sourcetrace.parsers.flow_evidence import (
    HTTP_ENDPOINT_METHODS,
    finalize_evidence,
    normalize_endpoint_path,
)

logger = logging.getLogger(__name__)

JS_TS_PARSER_VERSION: str = "js-ts-treesitter-v3"

# Path to the worker script used for subprocess-based safe parsing
_WORKER_SCRIPT: Path = Path(__file__).resolve().parent / "js_parser_worker.py"

_FUNCTION_VALUED_NODE_TYPES: frozenset[str] = frozenset(
    {"arrow_function", "function_expression", "function"}
)


@dataclass(frozen=True, slots=True)
class _RawSymbol:
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    content: str
    node: tree_sitter.Node | None = None
    skip_methods: bool = False


def _compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compute_chunk_id(
    repository_id: str,
    relative_path: str,
    symbol_type: str,
    symbol_name: str,
    start_line: int,
    end_line: int,
    content_hash: str,
    parser_version: str,
) -> str:
    canonical_string = (
        f"{repository_id}|{relative_path}|{symbol_type}|{symbol_name}|"
        f"{start_line}|{end_line}|{content_hash}|{parser_version}"
    )
    digest = hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()
    return f"chunk_{digest}"


_JS_LANGUAGE: tree_sitter.Language | None = None
_JSX_LANGUAGE: tree_sitter.Language | None = None
_TS_LANGUAGE: tree_sitter.Language | None = None
_TSX_LANGUAGE: tree_sitter.Language | None = None


def _get_js_language() -> tree_sitter.Language:
    global _JS_LANGUAGE
    if _JS_LANGUAGE is None:
        _JS_LANGUAGE = tree_sitter.Language(tsjs.language())
    return _JS_LANGUAGE


def _get_jsx_language() -> tree_sitter.Language:
    global _JSX_LANGUAGE
    if _JSX_LANGUAGE is None:
        _JSX_LANGUAGE = tree_sitter.Language(tsjs.language())
    return _JSX_LANGUAGE


def _get_ts_language() -> tree_sitter.Language:
    global _TS_LANGUAGE
    if _TS_LANGUAGE is None:
        _TS_LANGUAGE = tree_sitter.Language(tsts.language_typescript())
    return _TS_LANGUAGE


def _get_tsx_language() -> tree_sitter.Language:
    global _TSX_LANGUAGE
    if _TSX_LANGUAGE is None:
        _TSX_LANGUAGE = tree_sitter.Language(tsts.language_tsx())
    return _TSX_LANGUAGE


def _get_language_info(relative_path: str) -> tuple[str, tree_sitter.Language]:
    ext = Path(relative_path).suffix.casefold()
    if ext == ".tsx":
        return "tsx", _get_tsx_language()
    elif ext == ".ts":
        return "typescript", _get_ts_language()
    elif ext == ".jsx":
        return "jsx", _get_jsx_language()
    else:
        return "javascript", _get_js_language()


def _calc_line_bounds(node: tree_sitter.Node) -> tuple[int, int]:
    start_line = node.start_point.row + 1
    if node.end_point.column == 0 and node.end_point.row > node.start_point.row:
        end_line = node.end_point.row
    else:
        end_line = node.end_point.row + 1
    return start_line, max(start_line, end_line)


def _get_node_text(node: tree_sitter.Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _find_child_text(
    node: tree_sitter.Node,
    types: set[str],
    source_bytes: bytes,
) -> str | None:
    for c in node.children:
        if c.type in types:
            return _get_node_text(c, source_bytes)
    return None


def _is_hook_name(name: str) -> bool:
    return name.startswith("use") and len(name) > 3 and name[3].isupper()


def _is_react_component_name(name: str) -> bool:
    return len(name) > 0 and name[0].isupper()


def _is_react_context(language: str, source_text: str) -> bool:
    if language in ("jsx", "tsx"):
        return True
    return "react" in source_text.casefold()


def _string_literal_value(node: tree_sitter.Node | None, source_bytes: bytes) -> str | None:
    """Return the unquoted value of a plain string literal, else None (dynamic)."""
    if node is None or node.type != "string":
        return None
    text = _get_node_text(node, source_bytes)
    if len(text) >= 2 and text[0] in "'\"" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _js_callable_name(node: tree_sitter.Node, source_bytes: bytes) -> tuple[str, str] | None:
    """Return (local_name, kind) for a call/new target, or None when underivable."""
    if node.type == "identifier":
        return _get_node_text(node, source_bytes), "call"
    if node.type == "member_expression":
        parts: list[str] = []
        current: tree_sitter.Node | None = node
        while current is not None and current.type == "member_expression":
            prop = current.child_by_field_name("property")
            if prop is None or prop.type not in (
                "property_identifier",
                "private_property_identifier",
            ):
                return None
            parts.append(_get_node_text(prop, source_bytes))
            current = current.child_by_field_name("object")
        if current is not None and current.type in ("identifier", "this"):
            parts.append(_get_node_text(current, source_bytes))
            return ".".join(reversed(parts)), "attribute_call"
        # Base is not a plain name (call result, subscript, ...): keep the
        # final property as evidence rather than guessing the chain.
        return parts[0], "attribute_call"
    return None


def _call_arguments(call_node: tree_sitter.Node) -> list[tree_sitter.Node]:
    args_node = call_node.child_by_field_name("arguments")
    if args_node is None:
        return []
    return [c for c in args_node.children if c.type not in ("(", ")", ",", "comment")]


def _object_string_prop(obj_node: tree_sitter.Node, key: str, source_bytes: bytes) -> str | None:
    for child in obj_node.children:
        if child.type != "pair":
            continue
        key_node = child.child_by_field_name("key")
        value_node = child.child_by_field_name("value")
        if key_node is None or value_node is None:
            continue
        key_text = _get_node_text(key_node, source_bytes).strip("'\"")
        if key_text == key:
            return _string_literal_value(value_node, source_bytes)
    return None


@dataclass(frozen=True, slots=True)
class _ExpressContext:
    """Same-file Express knowledge: server/router names and literal mounts.

    server_objects: identifiers assigned from express() / express.Router() /
    Router(), plus every identifier mounted via use() — registrations on them
    are endpoint declarations even when the handler is a named reference.
    mount_prefixes: router name -> literal path prefix from
    `X.use('/prefix', router)`. Conflicting or computed mounts are never
    guessed: a router mounted twice with different literal prefixes loses its
    prefix entirely (declares keep their unprefixed normalized path).
    """

    server_objects: frozenset[str]
    mount_prefixes: dict[str, str]


_EMPTY_EXPRESS_CONTEXT = _ExpressContext(frozenset(), {})

# Exact factory-call texts that create Express apps/routers; deliberately not
# suffix-matched so e.g. React Router's createBrowserRouter never qualifies.
_EXPRESS_FACTORY_CALLS = frozenset({"express", "Router", "express.Router"})


def _identifier_text(node: tree_sitter.Node | None, source_bytes: bytes) -> str | None:
    if node is not None and node.type == "identifier":
        return _get_node_text(node, source_bytes)
    return None


def collect_express_context(root: tree_sitter.Node, source_bytes: bytes) -> _ExpressContext:
    """One deterministic pass over the whole file for Express server facts."""
    server_objects: set[str] = set()
    mounts: dict[str, str | None] = {}  # None = conflicting, never fold

    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.children))

        if node.type == "variable_declarator":
            name = _identifier_text(node.child_by_field_name("name"), source_bytes)
            value = node.child_by_field_name("value")
            if name and value is not None and value.type == "call_expression":
                callee = value.child_by_field_name("function")
                callee_text = _get_node_text(callee, source_bytes) if callee is not None else ""
                if callee_text in _EXPRESS_FACTORY_CALLS:
                    server_objects.add(name)
            continue

        if node.type != "call_expression":
            continue
        fn_node = node.child_by_field_name("function")
        if fn_node is None or fn_node.type != "member_expression":
            continue
        prop = fn_node.child_by_field_name("property")
        if prop is None or _get_node_text(prop, source_bytes) != "use":
            continue
        args = _call_arguments(node)
        if len(args) < 2:
            continue
        prefix = _string_literal_value(args[0], source_bytes)
        if prefix is None or not prefix.startswith("/") or len(prefix) < 2:
            continue
        for arg in args[1:]:
            mounted = _identifier_text(arg, source_bytes)
            if mounted is None:
                continue
            server_objects.add(mounted)
            clean = prefix.rstrip("/")
            existing = mounts.get(mounted, clean)
            mounts[mounted] = clean if existing == clean else None

    return _ExpressContext(
        server_objects=frozenset(server_objects),
        mount_prefixes={k: v for k, v in mounts.items() if v is not None},
    )


def _prefixed_normalized(prefix: str | None, path: str) -> str:
    if prefix is None:
        return normalize_endpoint_path(path)
    if path in ("", "/"):
        return normalize_endpoint_path(prefix)
    if path.startswith("/"):
        return normalize_endpoint_path(prefix + path)
    return normalize_endpoint_path(f"{prefix}/{path}")


def _js_call_endpoint(
    call_node: tree_sitter.Node,
    fn_node: tree_sitter.Node,
    source_bytes: bytes,
    express: _ExpressContext = _EMPTY_EXPRESS_CONTEXT,
) -> EndpointEvidence | None:
    args = _call_arguments(call_node)
    if not args:
        return None
    path = _string_literal_value(args[0], source_bytes)
    if path is None or not path.startswith(("/", "http://", "https://")):
        return None
    line_start, line_end = _calc_line_bounds(call_node)

    if fn_node.type == "identifier" and _get_node_text(fn_node, source_bytes) == "fetch":
        method = "GET"
        if len(args) > 1 and args[1].type == "object":
            literal_method = _object_string_prop(args[1], "method", source_bytes)
            if literal_method:
                method = literal_method.upper()
        return EndpointEvidence(
            "calls", method, path, normalize_endpoint_path(path), line_start, line_end
        )

    if fn_node.type == "member_expression":
        prop = fn_node.child_by_field_name("property")
        if prop is None:
            return None
        verb = _get_node_text(prop, source_bytes).casefold()
        if verb not in HTTP_ENDPOINT_METHODS:
            return None
        base = _identifier_text(fn_node.child_by_field_name("object"), source_bytes)
        is_server_object = base is not None and base in express.server_objects
        # Express-style declarations pass a handler (inline function, or any
        # second argument when the receiver is a known same-file server or
        # mounted router object); client calls do not.
        has_inline_handler = any(a.type in _FUNCTION_VALUED_NODE_TYPES for a in args[1:])
        is_declaration = has_inline_handler or (is_server_object and len(args) > 1)
        if is_declaration:
            prefix = express.mount_prefixes.get(base) if base is not None else None
            return EndpointEvidence(
                "declares",
                verb.upper(),
                path,
                _prefixed_normalized(prefix, path),
                line_start,
                line_end,
            )
        return EndpointEvidence(
            "calls", verb.upper(), path, normalize_endpoint_path(path), line_start, line_end
        )
    return None


def _es_import_evidence(node: tree_sitter.Node, source_bytes: bytes) -> list[ImportEvidence]:
    module = _string_literal_value(node.child_by_field_name("source"), source_bytes)
    if not module:
        return []
    line_start, line_end = _calc_line_bounds(node)
    out: list[ImportEvidence] = []
    for child in node.children:
        if child.type != "import_clause":
            continue
        for clause in child.children:
            if clause.type == "identifier":
                out.append(
                    ImportEvidence(
                        _get_node_text(clause, source_bytes),
                        module,
                        "default",
                        line_start,
                        line_end,
                    )
                )
            elif clause.type == "namespace_import":
                for ns_child in clause.children:
                    if ns_child.type == "identifier":
                        out.append(
                            ImportEvidence(
                                _get_node_text(ns_child, source_bytes),
                                module,
                                "*",
                                line_start,
                                line_end,
                            )
                        )
            elif clause.type == "named_imports":
                for spec in clause.children:
                    if spec.type != "import_specifier":
                        continue
                    name_node = spec.child_by_field_name("name")
                    if name_node is None:
                        continue
                    imported = _get_node_text(name_node, source_bytes)
                    alias_node = spec.child_by_field_name("alias")
                    local = (
                        _get_node_text(alias_node, source_bytes)
                        if alias_node is not None
                        else imported
                    )
                    out.append(ImportEvidence(local, module, imported, line_start, line_end))
    return out


def _require_import_evidence(
    declarator: tree_sitter.Node, source_bytes: bytes
) -> list[ImportEvidence]:
    value = declarator.child_by_field_name("value")
    if value is None or value.type != "call_expression":
        return []
    fn = value.child_by_field_name("function")
    if fn is None or fn.type != "identifier" or _get_node_text(fn, source_bytes) != "require":
        return []
    args = _call_arguments(value)
    if not args:
        return []
    module = _string_literal_value(args[0], source_bytes)
    if not module:
        return []
    name_node = declarator.child_by_field_name("name")
    if name_node is None:
        return []
    line_start, line_end = _calc_line_bounds(declarator)
    out: list[ImportEvidence] = []
    if name_node.type == "identifier":
        out.append(
            ImportEvidence(
                _get_node_text(name_node, source_bytes),
                module,
                module,
                line_start,
                line_end,
            )
        )
    elif name_node.type == "object_pattern":
        for pattern in name_node.children:
            if pattern.type == "shorthand_property_identifier_pattern":
                name = _get_node_text(pattern, source_bytes)
                out.append(ImportEvidence(name, module, name, line_start, line_end))
            elif pattern.type == "pair_pattern":
                key_node = pattern.child_by_field_name("key")
                value_pat = pattern.child_by_field_name("value")
                if (
                    key_node is not None
                    and value_pat is not None
                    and value_pat.type == "identifier"
                ):
                    out.append(
                        ImportEvidence(
                            _get_node_text(value_pat, source_bytes),
                            module,
                            _get_node_text(key_node, source_bytes),
                            line_start,
                            line_end,
                        )
                    )
    return out


def _module_import_evidence(root: tree_sitter.Node, source_bytes: bytes) -> list[ImportEvidence]:
    """Collect top-level ES import and CommonJS require bindings for a file."""
    out: list[ImportEvidence] = []
    for child in root.children:
        if child.type == "import_statement":
            out.extend(_es_import_evidence(child, source_bytes))
        elif child.type in ("lexical_declaration", "variable_declaration"):
            for decl in child.children:
                if decl.type == "variable_declarator":
                    out.extend(_require_import_evidence(decl, source_bytes))
    return out


def _walk_evidence_scope(root: tree_sitter.Node, skip_methods: bool):
    """Yield descendant nodes; optionally skip method_definition subtrees.

    Class chunks skip method bodies because each method is its own chunk and
    owns its own evidence.
    """
    pending = list(reversed(root.children))
    while pending:
        node = pending.pop()
        yield node
        if skip_methods and node.type == "method_definition":
            continue
        pending.extend(reversed(node.children))


def _jsx_component_reference(
    node: tree_sitter.Node, source_bytes: bytes
) -> ReferenceEvidence | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    line_start, line_end = _calc_line_bounds(node)
    if name_node.type in ("identifier", "jsx_identifier"):
        name = _get_node_text(name_node, source_bytes)
        if _is_react_component_name(name):
            return ReferenceEvidence(name, "call", line_start, line_end)
        return None
    if name_node.type == "member_expression":
        named = _js_callable_name(name_node, source_bytes)
        if named is not None:
            return ReferenceEvidence(named[0], named[1], line_start, line_end)
    return None


def _extract_js_flow_evidence(
    scope_node: tree_sitter.Node,
    source_bytes: bytes,
    module_imports: list[ImportEvidence],
    skip_methods: bool,
    express: _ExpressContext = _EMPTY_EXPRESS_CONTEXT,
    extra_endpoints: tuple[EndpointEvidence, ...] = (),
) -> tuple[
    tuple[ReferenceEvidence, ...],
    tuple[ImportEvidence, ...],
    tuple[EndpointEvidence, ...],
    bool,
]:
    """Deterministically extract references, imports, and endpoints for one symbol."""
    references: list[ReferenceEvidence] = []
    imports: list[ImportEvidence] = list(module_imports)
    endpoints: list[EndpointEvidence] = list(extra_endpoints)

    for node in _walk_evidence_scope(scope_node, skip_methods):
        node_type = node.type
        if node_type in ("call_expression", "new_expression"):
            fn_field = "function" if node_type == "call_expression" else "constructor"
            fn_node = node.child_by_field_name(fn_field)
            if fn_node is not None:
                named = _js_callable_name(fn_node, source_bytes)
                if named is not None:
                    line_start, line_end = _calc_line_bounds(node)
                    references.append(ReferenceEvidence(named[0], named[1], line_start, line_end))
                if node_type == "call_expression":
                    endpoint = _js_call_endpoint(node, fn_node, source_bytes, express)
                    if endpoint is not None:
                        endpoints.append(endpoint)
        elif node_type == "variable_declarator":
            imports.extend(_require_import_evidence(node, source_bytes))
        elif node_type in ("jsx_self_closing_element", "jsx_opening_element"):
            component_ref = _jsx_component_reference(node, source_bytes)
            if component_ref is not None:
                references.append(component_ref)

    final_refs, refs_truncated = finalize_evidence(references, lambda r: (r.local_name, r.kind))
    final_imports, imports_truncated = finalize_evidence(
        imports, lambda i: (i.local_name, i.source_module, i.imported_name)
    )
    final_endpoints, endpoints_truncated = finalize_evidence(
        endpoints, lambda e: (e.kind, e.http_method, e.path_literal)
    )
    return (
        final_refs,
        final_imports,
        final_endpoints,
        refs_truncated or imports_truncated or endpoints_truncated,
    )


def _extract_declarations_from_node(
    node: tree_sitter.Node,
    source_lines: list[str],
    language: str,
    source_bytes: bytes,
    source_text: str = "",
) -> list[_RawSymbol]:
    symbols: list[_RawSymbol] = []

    target_node = node
    if node.type == "export_statement":
        for child in node.children:
            if child.type in (
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
                "lexical_declaration",
                "variable_declaration",
                "interface_declaration",
                "type_alias_declaration",
                "enum_declaration",
            ):
                target_node = child
                break

    start_line, end_line = _calc_line_bounds(node)
    content = "".join(source_lines[start_line - 1 : end_line])

    node_type = target_node.type

    if node_type in ("function_declaration", "generator_function_declaration"):
        func_name = _find_child_text(
            target_node, {"identifier", "property_identifier"}, source_bytes
        )
        if not func_name:
            func_name = "default" if node.type == "export_statement" else "anonymous"

        is_react = _is_react_context(language, source_text)

        if _is_hook_name(func_name) and is_react:
            sym_type = "hook"
        elif _is_react_component_name(func_name) and is_react:
            sym_type = "react_component"
        elif b"async" in source_bytes[target_node.start_byte : target_node.end_byte]:
            sym_type = "async_function"
        else:
            sym_type = "function"

        symbols.append(_RawSymbol(func_name, sym_type, start_line, end_line, content, node=node))

    elif node_type == "class_declaration":
        class_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if not class_name:
            class_name = "default" if node.type == "export_statement" else "AnonymousClass"

        symbols.append(
            _RawSymbol(
                class_name,
                "class",
                start_line,
                end_line,
                content,
                node=target_node,
                skip_methods=True,
            )
        )

        # Inspect methods inside class_body
        for child in target_node.children:
            if child.type == "class_body":
                for member in child.children:
                    if member.type == "method_definition":
                        m_name = _find_child_text(
                            member,
                            {"property_identifier", "identifier", "private_property_identifier"},
                            source_bytes,
                        )
                        if m_name:
                            m_start, m_end = _calc_line_bounds(member)
                            m_content = "".join(source_lines[m_start - 1 : m_end])
                            qual_m_name = f"{class_name}.{m_name}"
                            m_type = (
                                "async_method"
                                if b"async" in source_bytes[member.start_byte : member.end_byte]
                                else "method"
                            )
                            symbols.append(
                                _RawSymbol(
                                    qual_m_name,
                                    m_type,
                                    m_start,
                                    m_end,
                                    m_content,
                                    node=member,
                                )
                            )

    elif node_type in ("lexical_declaration", "variable_declaration"):
        for decl in target_node.children:
            if decl.type == "variable_declarator":
                var_name: str | None = None
                value_node: tree_sitter.Node | None = None
                decl_start_byte = decl.start_byte
                decl_end_byte = decl.end_byte

                for sub in decl.children:
                    if sub.type in ("identifier", "property_identifier") and var_name is None:
                        var_name = _get_node_text(sub, source_bytes)
                    elif sub.type in ("arrow_function", "function_expression"):
                        value_node = sub

                if not var_name:
                    continue

                if value_node is not None:
                    v_start = value_node.start_byte
                    v_end = value_node.end_byte
                    is_react = _is_react_context(language, source_text)
                    if _is_hook_name(var_name) and is_react:
                        sym_type = "hook"
                    elif _is_react_component_name(var_name) and is_react:
                        sym_type = "react_component"
                    elif (
                        b"async" in source_bytes[v_start:v_end]
                        or b"async" in source_bytes[decl_start_byte:decl_end_byte]
                    ):
                        sym_type = "async_function"
                    else:
                        sym_type = "function"
                else:
                    sym_type = "constant"

                symbols.append(
                    _RawSymbol(var_name, sym_type, start_line, end_line, content, node=decl)
                )

    elif node_type == "interface_declaration":
        if_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if if_name:
            symbols.append(
                _RawSymbol(if_name, "interface", start_line, end_line, content, node=target_node)
            )

    elif node_type == "type_alias_declaration":
        type_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if type_name:
            symbols.append(
                _RawSymbol(type_name, "type_alias", start_line, end_line, content, node=target_node)
            )

    elif node_type == "enum_declaration":
        enum_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if enum_name:
            symbols.append(
                _RawSymbol(enum_name, "enum", start_line, end_line, content, node=target_node)
            )

    return symbols


def _get_parser(relative_path: str) -> tuple[str, tree_sitter.Parser]:
    language_name, lang_obj = _get_language_info(relative_path)
    return language_name, tree_sitter.Parser(lang_obj)


MAX_FALLBACK_CHUNK_CHARS: int = 4000


def _make_module_chunk(
    source: str,
    relative_path: str,
    language: str,
    repository_id: str,
    owner_session_id: str,
    source_lines_stripped: list[str] | None = None,
    parser_version: str = JS_TS_PARSER_VERSION,
) -> list[ParsedCodeChunk]:
    """Create deterministic, bounded fallback module chunk(s) when AST parsing fails
    or returns no symbols. Oversized source files (> MAX_FALLBACK_CHUNK_CHARS) are split into
    consecutive, line-bounded chunks to ensure embedding inputs remain within provider limits.
    """
    if not source:
        return []

    lines_with_nl = source.splitlines(True)
    total_lines = len(lines_with_nl)

    if len(source) <= MAX_FALLBACK_CHUNK_CHARS:
        content_hash = _compute_content_hash(source)
        start_line = 1
        end_line = max(1, total_lines)
        chunk_id = _compute_chunk_id(
            repository_id,
            relative_path,
            "module",
            "<module>",
            start_line,
            end_line,
            content_hash,
            parser_version,
        )
        return [
            ParsedCodeChunk(
                chunk_id=chunk_id,
                repository_id=repository_id,
                owner_session_id=owner_session_id,
                relative_path=relative_path,
                language=language,
                symbol_name="<module>",
                symbol_type="module",
                start_line=start_line,
                end_line=end_line,
                content=source,
                content_hash=content_hash,
                parser_version=parser_version,
            )
        ]

    # Split oversized fallback file line-by-line into bounded chunks
    chunks: list[ParsedCodeChunk] = []
    current_lines: list[str] = []
    current_chars = 0
    chunk_start_line = 1

    for idx, line in enumerate(lines_with_nl, start=1):
        line_len = len(line)

        # If adding this line exceeds max size and we already have accumulated lines, flush chunk
        if current_lines and (current_chars + line_len > MAX_FALLBACK_CHUNK_CHARS):
            chunk_content = "".join(current_lines)
            c_hash = _compute_content_hash(chunk_content)
            chunk_end_line = idx - 1
            c_id = _compute_chunk_id(
                repository_id,
                relative_path,
                "module",
                "<module>",
                chunk_start_line,
                chunk_end_line,
                c_hash,
                parser_version,
            )
            chunks.append(
                ParsedCodeChunk(
                    chunk_id=c_id,
                    repository_id=repository_id,
                    owner_session_id=owner_session_id,
                    relative_path=relative_path,
                    language=language,
                    symbol_name="<module>",
                    symbol_type="module",
                    start_line=chunk_start_line,
                    end_line=chunk_end_line,
                    content=chunk_content,
                    content_hash=c_hash,
                    parser_version=parser_version,
                )
            )
            current_lines = []
            current_chars = 0
            chunk_start_line = idx

        # Handle a single line that exceeds MAX_FALLBACK_CHUNK_CHARS
        if line_len > MAX_FALLBACK_CHUNK_CHARS:
            if current_lines:
                chunk_content = "".join(current_lines)
                c_hash = _compute_content_hash(chunk_content)
                chunk_end_line = idx - 1
                c_id = _compute_chunk_id(
                    repository_id,
                    relative_path,
                    "module",
                    "<module>",
                    chunk_start_line,
                    chunk_end_line,
                    c_hash,
                    parser_version,
                )
                chunks.append(
                    ParsedCodeChunk(
                        chunk_id=c_id,
                        repository_id=repository_id,
                        owner_session_id=owner_session_id,
                        relative_path=relative_path,
                        language=language,
                        symbol_name="<module>",
                        symbol_type="module",
                        start_line=chunk_start_line,
                        end_line=chunk_end_line,
                        content=chunk_content,
                        content_hash=c_hash,
                        parser_version=parser_version,
                    )
                )
                current_lines = []
                current_chars = 0

            # Slice single huge line into MAX_FALLBACK_CHUNK_CHARS slices
            for slice_start in range(0, line_len, MAX_FALLBACK_CHUNK_CHARS):
                slice_str = line[slice_start : slice_start + MAX_FALLBACK_CHUNK_CHARS]
                c_hash = _compute_content_hash(slice_str)
                c_id = _compute_chunk_id(
                    repository_id,
                    relative_path,
                    "module",
                    "<module>",
                    idx,
                    idx,
                    c_hash,
                    parser_version,
                )
                chunks.append(
                    ParsedCodeChunk(
                        chunk_id=c_id,
                        repository_id=repository_id,
                        owner_session_id=owner_session_id,
                        relative_path=relative_path,
                        language=language,
                        symbol_name="<module>",
                        symbol_type="module",
                        start_line=idx,
                        end_line=idx,
                        content=slice_str,
                        content_hash=c_hash,
                        parser_version=parser_version,
                    )
                )
            chunk_start_line = idx + 1
        else:
            current_lines.append(line)
            current_chars += line_len

    if current_lines:
        chunk_content = "".join(current_lines)
        c_hash = _compute_content_hash(chunk_content)
        chunk_end_line = total_lines
        c_id = _compute_chunk_id(
            repository_id,
            relative_path,
            "module",
            "<module>",
            chunk_start_line,
            chunk_end_line,
            c_hash,
            parser_version,
        )
        chunks.append(
            ParsedCodeChunk(
                chunk_id=c_id,
                repository_id=repository_id,
                owner_session_id=owner_session_id,
                relative_path=relative_path,
                language=language,
                symbol_name="<module>",
                symbol_type="module",
                start_line=chunk_start_line,
                end_line=chunk_end_line,
                content=chunk_content,
                content_hash=c_hash,
                parser_version=parser_version,
            )
        )

    return chunks


def _absorb_top_level_registrations(
    root: tree_sitter.Node,
    source_bytes: bytes,
    express: _ExpressContext,
    source_lines: list[str],
    known_symbol_names: set[str],
    extra_endpoints_by_symbol: dict[str, list[EndpointEvidence]],
) -> list[_RawSymbol]:
    """Recover declares evidence from top-level Express route registrations.

    `app.get('/path', ...)` at module top level lives outside every extracted
    symbol, so its declaration evidence was previously lost. Same-file only:
    the receiver must be a known server/router object. When a handler
    argument names a same-file symbol, the declares evidence is attached to
    that symbol's chunk; otherwise (inline handler) a synthetic
    `route_handler` chunk is created for the registration so the endpoint is
    still traceable — its scope walk also captures the handler body's own
    references.
    """
    synthesized: list[_RawSymbol] = []
    for child in root.children:
        if child.type != "expression_statement" or not child.children:
            continue
        call_node = child.children[0]
        if call_node.type != "call_expression":
            continue
        fn_node = call_node.child_by_field_name("function")
        if fn_node is None or fn_node.type != "member_expression":
            continue
        base = _identifier_text(fn_node.child_by_field_name("object"), source_bytes)
        if base is None or base not in express.server_objects:
            continue
        evidence = _js_call_endpoint(call_node, fn_node, source_bytes, express)
        if evidence is None or evidence.kind != "declares":
            continue

        args = _call_arguments(call_node)
        named_handlers = [
            name
            for arg in args[1:]
            if (name := _identifier_text(arg, source_bytes)) is not None
            and name in known_symbol_names
        ]
        if named_handlers:
            for name in named_handlers:
                extra_endpoints_by_symbol.setdefault(name, []).append(evidence)
            continue

        symbol_name = f"{evidence.http_method} {evidence.path_literal}"
        start_line, end_line = _calc_line_bounds(call_node)
        synthesized.append(
            _RawSymbol(
                symbol_name=symbol_name,
                symbol_type="route_handler",
                start_line=start_line,
                end_line=end_line,
                content="".join(source_lines[start_line - 1 : end_line]),
                node=call_node,
            )
        )
        extra_endpoints_by_symbol.setdefault(symbol_name, []).append(evidence)
    return synthesized


def _parse_javascript_source_in_process(
    source: str,
    relative_path: str,
    repository_id: str,
    owner_session_id: str,
) -> list[ParsedCodeChunk]:
    """Parse JS/TS source in-process with try/except for Python-level errors only.

    NOTE: This function does NOT protect against native tree-sitter parser crashes
    (segfaults / access violations). Use _parse_javascript_source_safe() for
    production use.
    """
    if not source or not source.strip():
        return []

    source_lines = source.splitlines(True)
    source_lines_stripped = source.splitlines()

    language, parser = _get_parser(relative_path)
    source_bytes = source.encode("utf-8")

    try:
        tree = parser.parse(source_bytes)
    except Exception:
        # Malformed / unparseable source falls back safely to module chunk
        tree = None

    raw_symbols: list[_RawSymbol] = []

    if tree is not None and tree.root_node is not None:
        for child in tree.root_node.children:
            extracted = _extract_declarations_from_node(
                child, source_lines, language, source_bytes, source_text=source
            )
            raw_symbols.extend(extracted)

    express = _EMPTY_EXPRESS_CONTEXT
    extra_endpoints_by_symbol: dict[str, list[EndpointEvidence]] = {}
    if tree is not None and tree.root_node is not None:
        express = collect_express_context(tree.root_node, source_bytes)
        raw_symbols.extend(
            _absorb_top_level_registrations(
                tree.root_node,
                source_bytes,
                express,
                source_lines,
                {s.symbol_name for s in raw_symbols},
                extra_endpoints_by_symbol,
            )
        )

    # Sort symbols deterministically by start_line, end_line
    raw_symbols.sort(key=lambda s: (s.start_line, s.end_line, s.symbol_name))

    chunks: list[ParsedCodeChunk] = []

    if not raw_symbols:
        return _make_module_chunk(
            source, relative_path, language, repository_id, owner_session_id, source_lines_stripped
        )

    module_imports: list[ImportEvidence] = []
    if tree is not None and tree.root_node is not None:
        module_imports = _module_import_evidence(tree.root_node, source_bytes)

    for sym in raw_symbols:
        content_hash = _compute_content_hash(sym.content)
        chunk_id = _compute_chunk_id(
            repository_id,
            relative_path,
            sym.symbol_type,
            sym.symbol_name,
            sym.start_line,
            sym.end_line,
            content_hash,
            JS_TS_PARSER_VERSION,
        )
        references: tuple[ReferenceEvidence, ...] = ()
        imports: tuple[ImportEvidence, ...] = ()
        endpoints: tuple[EndpointEvidence, ...] = ()
        truncated = False
        if sym.node is not None:
            references, imports, endpoints, truncated = _extract_js_flow_evidence(
                sym.node,
                source_bytes,
                module_imports,
                sym.skip_methods,
                express,
                tuple(extra_endpoints_by_symbol.get(sym.symbol_name, ())),
            )
        chunks.append(
            ParsedCodeChunk(
                chunk_id=chunk_id,
                repository_id=repository_id,
                owner_session_id=owner_session_id,
                relative_path=relative_path,
                language=language,
                symbol_name=sym.symbol_name,
                symbol_type=sym.symbol_type,
                start_line=sym.start_line,
                end_line=sym.end_line,
                content=sym.content,
                content_hash=content_hash,
                parser_version=JS_TS_PARSER_VERSION,
                references=references,
                imports=imports,
                endpoints=endpoints,
                extraction_truncated=truncated,
            )
        )

    return chunks


def _parsed_chunk_from_dict(data: dict) -> ParsedCodeChunk:
    """Rebuild a ParsedCodeChunk from the worker's JSON dict, restoring the
    nested evidence dataclasses that dataclasses.asdict flattened to dicts."""
    plain = dict(data)
    references = tuple(ReferenceEvidence(**item) for item in (plain.pop("references", None) or ()))
    imports = tuple(ImportEvidence(**item) for item in (plain.pop("imports", None) or ()))
    endpoints = tuple(EndpointEvidence(**item) for item in (plain.pop("endpoints", None) or ()))
    return ParsedCodeChunk(
        **plain,
        references=references,
        imports=imports,
        endpoints=endpoints,
    )


def _parse_javascript_source_safe(
    source: str,
    relative_path: str,
    repository_id: str,
    owner_session_id: str,
    timeout_seconds: float = 5.0,
) -> list[ParsedCodeChunk] | None:
    """Parse JS/TS source in an isolated subprocess to survive native parser crashes.

    The subprocess runs the full parse_javascript_source pipeline. If the native
    tree-sitter parser crashes (access violation / segfault), only the child process
    dies, and this function returns None to signal the caller to fall back safely.

    Returns:
        - list[ParsedCodeChunk] on success
        - None if the subprocess crashed or timed out
    """
    input_data = {
        "source": source,
        "relative_path": relative_path,
        "repository_id": repository_id,
        "owner_session_id": owner_session_id,
    }

    input_json = json.dumps(input_data)

    # Ensure worker script can locate sourcetrace package in sys.path
    import os

    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parent.parent.parent)
    if "PYTHONPATH" in env and env["PYTHONPATH"]:
        env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = src_dir

    try:
        result = subprocess.run(
            [sys.executable, str(_WORKER_SCRIPT)],
            input=input_json,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "JS/TS parser subprocess timed out for %s (%.1fs). Falling back to module chunk.",
            relative_path,
            timeout_seconds,
        )
        return None
    except FileNotFoundError:
        logger.error(
            "JS/TS parser worker script not found. Falling back to module chunk.",
        )
        return None
    except OSError as e:
        logger.warning(
            "JS/TS parser subprocess error for %s: %s. Falling back to module chunk.",
            relative_path,
            e,
        )
        return None

    if result.returncode != 0:
        logger.warning(
            "JS/TS parser subprocess for %s crashed with return code %d. "
            "Falling back to module chunk.",
            relative_path,
            result.returncode,
        )
        return None

    stdout = result.stdout.strip()
    if not stdout:
        logger.warning(
            "JS/TS parser subprocess for %s produced no output. Falling back to module chunk.",
            relative_path,
        )
        return None

    try:
        chunk_dicts = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning(
            "JS/TS parser subprocess for %s produced invalid JSON. Falling back to module chunk.",
            relative_path,
        )
        return None

    # Reconstruct ParsedCodeChunk instances (and nested evidence records) from dicts
    chunks = []
    for d in chunk_dicts:
        try:
            chunks.append(_parsed_chunk_from_dict(d))
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            logger.warning(
                "JS/TS parser subprocess for %s returned invalid chunk data: %s. "
                "Falling back to module chunk.",
                relative_path,
                e,
            )
            return None

    return chunks


def parse_javascript_source(
    source: str,
    relative_path: str,
    repository_id: str,
    owner_session_id: str,
) -> list[ParsedCodeChunk]:
    """Parse a JavaScript or TypeScript file statically using tree-sitter.

    This is the public API entry point. It uses subprocess isolation to protect
    against native tree-sitter parser crashes (access violations / segfaults on
    Windows). If the subprocess crashes, a module-level fallback chunk is returned
    to ensure indexing continues without process termination.
    """
    if not source or not source.strip():
        return []

    source = source
    source_lines_stripped = source.splitlines()

    # Use subprocess-safe parsing to survive native tree-sitter crashes
    chunks = _parse_javascript_source_safe(
        source=source,
        relative_path=relative_path,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
    )

    if chunks is not None:
        return chunks

    # Subprocess crashed or failed — fall back to module-level chunk
    # Determine language for the fallback chunk
    language = _get_language_info(relative_path)[0]
    return _make_module_chunk(
        source=source,
        relative_path=relative_path,
        language=language,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        source_lines_stripped=source_lines_stripped,
    )
