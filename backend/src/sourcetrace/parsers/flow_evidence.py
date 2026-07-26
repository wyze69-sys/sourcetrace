"""Language-neutral helpers for flow-evidence extraction (TRACE-001/002).

Shared by the Python and JS/TS parsers. Kept free of parser imports so both
sides can use it without creating an import cycle (python_ast already imports
javascript_ast for module-chunk fallback).
"""

from __future__ import annotations

from collections.abc import Callable

# Per-category cap on flow evidence items stored per chunk.
FLOW_EVIDENCE_MAX_ITEMS: int = 100

HTTP_ENDPOINT_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options"}
)


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
