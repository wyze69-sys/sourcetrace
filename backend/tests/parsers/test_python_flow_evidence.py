"""Offline unit tests for Python flow-evidence extraction (TRACE-001).

Covers ReferenceEvidence, ImportEvidence, and EndpointEvidence extraction:
same-file helper calls, dotted attribute calls, import binding forms,
FastAPI/Flask-style endpoint declarations, HTTP client endpoint calls,
scope ownership, deduplication, determinism, caps, and line evidence.
"""

from __future__ import annotations

from sourcetrace.models.domain import (
    EndpointEvidence,
    ImportEvidence,
    ReferenceEvidence,
)
from sourcetrace.parsers.flow_evidence import FLOW_EVIDENCE_MAX_ITEMS
from sourcetrace.parsers.python_ast import (
    PYTHON_AST_PARSER_VERSION,
    _normalize_endpoint_path,
    parse_python_source,
)


def _parse(source: str) -> list:
    return parse_python_source(
        source=source,
        relative_path="src/app.py",
        repository_id="repo_flow",
        owner_session_id="sess_flow",
    )


def _chunk(chunks: list, symbol_name: str):
    matches = [c for c in chunks if c.symbol_name == symbol_name]
    assert len(matches) == 1, f"expected exactly one chunk named {symbol_name}"
    return matches[0]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_same_file_helper_call_is_captured() -> None:
    source = """def helper(x):
    return x + 1


def main(value):
    return helper(value)
"""
    chunks = _parse(source)
    main = _chunk(chunks, "main")
    assert (
        ReferenceEvidence(local_name="helper", kind="call", line_start=6, line_end=6)
        in main.references
    )


def test_dotted_attribute_call_preserves_chain() -> None:
    source = """import os.path


def resolve(p):
    return os.path.join(p, "x")
"""
    chunks = _parse(source)
    resolve = _chunk(chunks, "resolve")
    refs = {(r.local_name, r.kind) for r in resolve.references}
    assert ("os.path.join", "attribute_call") in refs


def test_call_on_call_result_keeps_final_attribute_only() -> None:
    source = """def run(factory):
    return factory().execute()
"""
    chunks = _parse(source)
    run = _chunk(chunks, "run")
    refs = {(r.local_name, r.kind) for r in run.references}
    assert ("execute", "attribute_call") in refs
    assert ("factory", "call") in refs


def test_duplicate_calls_dedupe_to_earliest_line() -> None:
    source = """def helper():
    return 1


def main():
    helper()
    helper()
    return helper()
"""
    chunks = _parse(source)
    main = _chunk(chunks, "main")
    helper_refs = [r for r in main.references if r.local_name == "helper"]
    assert len(helper_refs) == 1
    assert helper_refs[0].line_start == 6


def test_references_are_deterministic_across_parses() -> None:
    source = """import json


def alpha(data):
    parsed = json.loads(data)
    return beta(parsed)


def beta(x):
    return json.dumps(x)
"""
    c1 = _parse(source)
    c2 = _parse(source)
    for a, b in zip(c1, c2, strict=True):
        assert a.references == b.references
        assert a.imports == b.imports
        assert a.endpoints == b.endpoints
        assert a.extraction_truncated == b.extraction_truncated


def test_reference_cap_sets_truncated_flag() -> None:
    calls = "\n".join(f"    fn_{i}()" for i in range(FLOW_EVIDENCE_MAX_ITEMS + 10))
    source = f"def big():\n{calls}\n"
    chunks = _parse(source)
    big = _chunk(chunks, "big")
    assert len(big.references) == FLOW_EVIDENCE_MAX_ITEMS
    assert big.extraction_truncated is True


# ---------------------------------------------------------------------------
# Scope ownership
# ---------------------------------------------------------------------------


def test_nested_function_calls_belong_to_inner_chunk_only() -> None:
    source = """def outer():
    def inner():
        return deep_helper()

    return inner
"""
    chunks = _parse(source)
    outer = _chunk(chunks, "outer")
    inner = _chunk(chunks, "outer.inner")
    outer_names = {r.local_name for r in outer.references}
    inner_names = {r.local_name for r in inner.references}
    assert "deep_helper" not in outer_names
    assert "deep_helper" in inner_names


def test_class_chunk_does_not_absorb_method_body_calls() -> None:
    source = """class Service:
    def run(self):
        return self.execute()
"""
    chunks = _parse(source)
    cls = _chunk(chunks, "Service")
    method = _chunk(chunks, "Service.run")
    assert {r.local_name for r in cls.references} == set()
    assert ("self.execute", "attribute_call") in {
        (r.local_name, r.kind) for r in method.references
    }


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_module_imports_attach_to_every_symbol_chunk() -> None:
    source = """from services.auth import verify_token as verify
import logging


def a():
    return verify("t")


def b():
    return logging.getLogger()
"""
    chunks = _parse(source)
    expected_from = ImportEvidence(
        local_name="verify",
        source_module="services.auth",
        imported_name="verify_token",
        line_start=1,
        line_end=1,
    )
    expected_plain = ImportEvidence(
        local_name="logging",
        source_module="logging",
        imported_name="logging",
        line_start=2,
        line_end=2,
    )
    for name in ("a", "b"):
        chunk = _chunk(chunks, name)
        assert expected_from in chunk.imports
        assert expected_plain in chunk.imports


def test_function_level_and_relative_imports() -> None:
    source = """def lazy():
    from . import sibling
    from ..pkg import thing
    return sibling, thing
"""
    chunks = _parse(source)
    lazy = _chunk(chunks, "lazy")
    entries = {(i.local_name, i.source_module, i.imported_name) for i in lazy.imports}
    assert ("sibling", ".", "sibling") in entries
    assert ("thing", "..pkg", "thing") in entries


def test_dotted_plain_import_binds_top_level_name() -> None:
    source = """import os.path


def f():
    return os.path
"""
    chunks = _parse(source)
    f = _chunk(chunks, "f")
    entries = {(i.local_name, i.source_module) for i in f.imports}
    assert ("os", "os.path") in entries


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_fastapi_style_decorator_declares_endpoint() -> None:
    source = """@router.post("/api/v1/repositories/{repository_id}/trace")
def trace(repository_id: str):
    return None
"""
    chunks = _parse(source)
    trace = _chunk(chunks, "trace")
    assert trace.endpoints == (
        EndpointEvidence(
            kind="declares",
            http_method="POST",
            path_literal="/api/v1/repositories/{repository_id}/trace",
            normalized_path="/api/v1/repositories/{}/trace",
            line_start=1,
            line_end=1,
        ),
    )


def test_flask_route_decorator_expands_methods() -> None:
    source = """@app.route("/users/<int:user_id>", methods=["GET", "DELETE"])
def user(user_id):
    return None
"""
    chunks = _parse(source)
    user = _chunk(chunks, "user")
    declared = {(e.http_method, e.normalized_path) for e in user.endpoints}
    assert declared == {("GET", "/users/{}"), ("DELETE", "/users/{}")}


def test_flask_route_without_methods_defaults_to_get() -> None:
    source = """@app.route("/health")
def health():
    return "ok"
"""
    chunks = _parse(source)
    health = _chunk(chunks, "health")
    assert [(e.kind, e.http_method) for e in health.endpoints] == [("declares", "GET")]


def test_apirouter_prefix_is_folded_into_normalized_path() -> None:
    source = """from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/repositories", tags=["impact"])


@router.post("/{repository_id}/impact")
def preview_change_impact(repository_id: str):
    return None
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "preview_change_impact")
    assert handler.endpoints == (
        EndpointEvidence(
            kind="declares",
            http_method="POST",
            # The literal stays exactly as written in source (citation honesty);
            # only the comparable normalized form gains the router prefix.
            path_literal="/{repository_id}/impact",
            normalized_path="/api/v1/repositories/{}/impact",
            line_start=6,
            line_end=6,
        ),
    )


def test_blueprint_url_prefix_is_folded_including_route_decorator() -> None:
    source = """from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/users/<int:user_id>", methods=["GET", "DELETE"])
def user(user_id):
    return None
"""
    chunks = _parse(source)
    user = _chunk(chunks, "user")
    declared = {(e.http_method, e.normalized_path) for e in user.endpoints}
    assert declared == {("GET", "/admin/users/{}"), ("DELETE", "/admin/users/{}")}


def test_root_path_on_prefixed_router_declares_the_prefix_itself() -> None:
    source = """router = APIRouter(prefix="/api/v1/items")


@router.get("/")
def list_items():
    return []
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "list_items")
    assert handler.endpoints[0].normalized_path == "/api/v1/items"
    assert handler.endpoints[0].path_literal == "/"


def test_trailing_slash_prefix_is_normalized_before_folding() -> None:
    source = """router = APIRouter(prefix="/api/v1/things/")


@router.get("/{thing_id}")
def get_thing(thing_id: str):
    return None
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "get_thing")
    assert handler.endpoints[0].normalized_path == "/api/v1/things/{}"


def test_computed_prefix_is_never_guessed() -> None:
    source = """PREFIX = "/api/v1"
router = APIRouter(prefix=PREFIX + "/repos")


@router.get("/{repo_id}")
def get_repo(repo_id: str):
    return None
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "get_repo")
    # Non-literal prefix: the declared path is kept unprefixed rather than
    # fabricating a normalized path from an unresolved expression.
    assert handler.endpoints[0].normalized_path == "/{}"


def test_unprefixed_app_decorator_is_unchanged() -> None:
    source = """router = APIRouter(prefix="/api/v1/other")


@app.get("/api/v1/health")
def health():
    return "ok"
"""
    chunks = _parse(source)
    health = _chunk(chunks, "health")
    assert health.endpoints[0].normalized_path == "/api/v1/health"


def test_prefixed_declares_matches_client_call_through_flow_trace() -> None:
    """Cross-file regression: the exact gap this fix closes (client calls the
    full path; the handler declares only the suffix under a router prefix)."""
    from datetime import UTC, datetime

    from sourcetrace.models.domain import CodeChunk, RetrievalResult
    from sourcetrace.retrieval.trace import FlowTraceService

    handler_source = """router = APIRouter(prefix="/api/v1/stats")


@router.get("/summary")
def read_summary():
    return None
"""
    client_source = """def fetch_summary(client):
    return client.get("/api/v1/stats/summary")
"""
    parsed = parse_python_source(
        source=handler_source,
        relative_path="backend/routes/stats.py",
        repository_id="repo_flow",
        owner_session_id="sess_flow",
    ) + parse_python_source(
        source=client_source,
        relative_path="backend/client.py",
        repository_id="repo_flow",
        owner_session_id="sess_flow",
    )
    now = datetime(2026, 7, 26, tzinfo=UTC)
    chunks = [
        CodeChunk(
            chunk_id=p.chunk_id,
            repository_id=p.repository_id,
            owner_session_id=p.owner_session_id,
            relative_path=p.relative_path,
            language=p.language,
            symbol_name=p.symbol_name,
            symbol_type=p.symbol_type,
            start_line=p.start_line,
            end_line=p.end_line,
            content=p.content,
            content_hash=p.content_hash,
            parser_version=p.parser_version,
            created_at=now,
            references=p.references,
            imports=p.imports,
            endpoints=p.endpoints,
        )
        for p in parsed
    ]

    class _Repo:
        def list_by_repository(self, owner_session_id, repository_id):
            return list(chunks)

        def search_lexical(self, owner_session_id, repository_id, query_text, limit=5):
            hits = [c for c in chunks if query_text in c.symbol_name]
            return [RetrievalResult(chunk=c, score=1.0) for c in hits[:limit]]

    result = FlowTraceService(_Repo()).trace("sess_flow", "repo_flow", "fetch_summary")

    http_edges = [e for e in result.edges if e.kind == "http"]
    assert len(http_edges) == 1
    assert http_edges[0].evidence_label == "GET /api/v1/stats/summary"
    assert not any(g.kind == "endpoint_unmatched" for g in result.gaps)


def test_http_client_call_yields_calls_endpoint_with_host_stripped() -> None:
    source = """import requests


def push(payload):
    return requests.post("https://api.example.com/v1/things?q=1", json=payload)
"""
    chunks = _parse(source)
    push = _chunk(chunks, "push")
    assert push.endpoints == (
        EndpointEvidence(
            kind="calls",
            http_method="POST",
            path_literal="https://api.example.com/v1/things?q=1",
            normalized_path="/v1/things",
            line_start=5,
            line_end=5,
        ),
    )


def test_declaring_decorator_not_double_counted_as_call() -> None:
    source = """@router.get("/items")
def items():
    return client.get("/downstream")
"""
    chunks = _parse(source)
    items = _chunk(chunks, "items")
    kinds = [(e.kind, e.path_literal) for e in items.endpoints]
    assert ("declares", "/items") in kinds
    assert ("calls", "/downstream") in kinds
    assert len(kinds) == 2


def test_dynamic_path_produces_no_endpoint_evidence() -> None:
    source = """def fetch(client, item_id):
    return client.get(f"/items/{item_id}")
"""
    chunks = _parse(source)
    fetch = _chunk(chunks, "fetch")
    assert fetch.endpoints == ()


def test_non_path_string_argument_is_not_an_endpoint() -> None:
    source = """def fetch(store):
    return store.get("cache-key")
"""
    chunks = _parse(source)
    fetch = _chunk(chunks, "fetch")
    assert fetch.endpoints == ()


def test_normalize_endpoint_path_forms() -> None:
    assert _normalize_endpoint_path("/a/{id}/b") == "/a/{}/b"
    assert _normalize_endpoint_path("/a/<int:x>") == "/a/{}"
    assert _normalize_endpoint_path("/a/:name") == "/a/{}"
    assert _normalize_endpoint_path("http://h.example/p/{v}?x=1") == "/p/{}"
    assert _normalize_endpoint_path("https://h.example") == "/"
    assert _normalize_endpoint_path("") == ""


# ---------------------------------------------------------------------------
# Version and defaults
# ---------------------------------------------------------------------------


def test_parser_version_bumped_for_flow_evidence() -> None:
    assert PYTHON_AST_PARSER_VERSION == "python-ast-v3"
    chunks = _parse("def f():\n    return 1\n")
    assert chunks[0].parser_version == "python-ast-v3"


def test_symbol_free_module_chunk_has_empty_evidence() -> None:
    chunks = _parse("VALUE = 1\nOTHER = VALUE + 1\n")
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.references == ()
        assert chunk.imports == ()
        assert chunk.endpoints == ()
        assert chunk.extraction_truncated is False
