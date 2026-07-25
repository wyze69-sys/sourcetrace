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

from sourcetrace.models.domain import ParsedCodeChunk

logger = logging.getLogger(__name__)

JS_TS_PARSER_VERSION: str = "js-ts-treesitter-v1"

# Path to the worker script used for subprocess-based safe parsing
_WORKER_SCRIPT: Path = Path(__file__).resolve().parent / "js_parser_worker.py"


@dataclass(frozen=True, slots=True)
class _RawSymbol:
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int
    content: str


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

        symbols.append(_RawSymbol(func_name, sym_type, start_line, end_line, content))

    elif node_type == "class_declaration":
        class_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if not class_name:
            class_name = "default" if node.type == "export_statement" else "AnonymousClass"

        symbols.append(_RawSymbol(class_name, "class", start_line, end_line, content))

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
                                _RawSymbol(qual_m_name, m_type, m_start, m_end, m_content)
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
                        b"async" in source_bytes[v_start : v_end]
                        or b"async" in source_bytes[decl_start_byte : decl_end_byte]
                    ):
                        sym_type = "async_function"
                    else:
                        sym_type = "function"
                else:
                    sym_type = "constant"

                symbols.append(_RawSymbol(var_name, sym_type, start_line, end_line, content))

    elif node_type == "interface_declaration":
        if_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if if_name:
            symbols.append(_RawSymbol(if_name, "interface", start_line, end_line, content))

    elif node_type == "type_alias_declaration":
        type_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if type_name:
            symbols.append(_RawSymbol(type_name, "type_alias", start_line, end_line, content))

    elif node_type == "enum_declaration":
        enum_name = _find_child_text(target_node, {"type_identifier", "identifier"}, source_bytes)
        if enum_name:
            symbols.append(_RawSymbol(enum_name, "enum", start_line, end_line, content))

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

    # Sort symbols deterministically by start_line, end_line
    raw_symbols.sort(key=lambda s: (s.start_line, s.end_line, s.symbol_name))

    chunks: list[ParsedCodeChunk] = []

    if not raw_symbols:
        return _make_module_chunk(
            source, relative_path, language, repository_id, owner_session_id, source_lines_stripped
        )

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
            )
        )

    return chunks


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

    # Reconstruct ParsedCodeChunk instances from dicts
    chunks = []
    for d in chunk_dicts:
        try:
            chunks.append(ParsedCodeChunk(**d))
        except (TypeError, ValueError) as e:
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