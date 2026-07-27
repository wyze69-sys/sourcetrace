"""Language-neutral helpers for flow-evidence extraction (TRACE-001/002).

Shared by the Python and JS/TS parsers. Kept free of parser imports so both
sides can use it without creating an import cycle (python_ast already imports
javascript_ast for module-chunk fallback).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

# Per-category cap on flow evidence items stored per chunk.
FLOW_EVIDENCE_MAX_ITEMS: int = 100

HTTP_ENDPOINT_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options"}
)

# Parser versions whose chunks carry flow evidence. Older v2 chunks are
# still valid evidence — v3 only adds router-prefix/mount folding into
# normalized_path (and, for JS/TS, top-level Express registration recovery),
# so v2 indexes lose some HTTP-edge matches but never lie.
SUPPORTED_FLOW_EVIDENCE_PARSER_VERSIONS: frozenset[str] = frozenset(
    {
        "python-ast-v2",
        "python-ast-v3",
        "js-ts-treesitter-v2",
        "js-ts-treesitter-v3",
    }
)
EVIDENCE_PARSER_VERSIONS: frozenset[str] = SUPPORTED_FLOW_EVIDENCE_PARSER_VERSIONS


def is_flow_evidence_complete(parser_versions: Iterable[str]) -> bool:
    """Determine whether flow evidence is complete based on parser_versions.

    Returns True if and only if parser_versions is non-empty AND every version in it
    is present in SUPPORTED_FLOW_EVIDENCE_PARSER_VERSIONS.
    """
    versions = set(parser_versions)
    if not versions:
        return False
    return versions.issubset(SUPPORTED_FLOW_EVIDENCE_PARSER_VERSIONS)


def normalize_endpoint_path(path_literal: str) -> str:
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


def finalize_evidence(items: list, identity_key: Callable) -> tuple[tuple, bool]:
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
