"""Offline unit tests for JS/TS flow-evidence extraction (TRACE-002).

Covers ES/CommonJS import bindings, call and JSX component references,
fetch/client endpoint calls, Express-style inline declarations, scope
ownership, dedupe/determinism/caps, the parser version bump, and the
worker JSON round-trip that must restore evidence dataclasses.
"""

from __future__ import annotations

from sourcetrace.models.domain import (
    EndpointEvidence,
    ImportEvidence,
    ReferenceEvidence,
)
from sourcetrace.parsers.flow_evidence import FLOW_EVIDENCE_MAX_ITEMS
from sourcetrace.parsers.javascript_ast import (
    JS_TS_PARSER_VERSION,
    _parse_javascript_source_in_process,
    parse_javascript_source,
)


def _parse(source: str, relative_path: str = "src/app.js") -> list:
    return _parse_javascript_source_in_process(
        source=source,
        relative_path=relative_path,
        repository_id="repo_flow",
        owner_session_id="sess_flow",
    )


def _chunk(chunks: list, symbol_name: str):
    matches = [c for c in chunks if c.symbol_name == symbol_name]
    assert len(matches) == 1, f"expected exactly one chunk named {symbol_name}"
    return matches[0]


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_es_import_forms_attach_to_every_chunk() -> None:
    source = """import axios from 'axios';
import { helper as h, plain } from './utils';
import * as everything from './all';

export function first() { return h(); }
export function second() { return plain(); }
"""
    chunks = _parse(source)
    expected = {
        ("axios", "axios", "default"),
        ("h", "./utils", "helper"),
        ("plain", "./utils", "plain"),
        ("everything", "./all", "*"),
    }
    for name in ("first", "second"):
        entries = {
            (i.local_name, i.source_module, i.imported_name) for i in _chunk(chunks, name).imports
        }
        assert expected <= entries


def test_commonjs_require_identifier_and_destructured() -> None:
    source = """const db = require('../config/db');
const { list, save: persist } = require('./repo');

function run() { return persist(list(db)); }
"""
    chunks = _parse(source)
    run = _chunk(chunks, "run")
    entries = {(i.local_name, i.source_module, i.imported_name) for i in run.imports}
    assert ("db", "../config/db", "../config/db") in entries
    assert ("list", "./repo", "list") in entries
    assert ("persist", "./repo", "save") in entries


def test_function_scope_require_is_captured() -> None:
    source = """function lazy() {
  const late = require('./late');
  return late();
}
"""
    chunks = _parse(source)
    lazy = _chunk(chunks, "lazy")
    entries = {(i.local_name, i.source_module) for i in lazy.imports}
    assert ("late", "./late") in entries


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_same_file_helper_and_member_chain_calls() -> None:
    source = """function helper(x) { return x; }

function main(v) {
  const a = helper(v);
  return db.session.commit(a);
}
"""
    chunks = _parse(source)
    main = _chunk(chunks, "main")
    refs = {(r.local_name, r.kind) for r in main.references}
    assert ("helper", "call") in refs
    assert ("db.session.commit", "attribute_call") in refs


def test_new_expression_yields_call_reference() -> None:
    source = """function build() { return new Repository(); }
"""
    chunks = _parse(source)
    build = _chunk(chunks, "build")
    assert ("Repository", "call") in {(r.local_name, r.kind) for r in build.references}


def test_jsx_component_references(relative_path: str = "src/App.jsx") -> None:
    source = """export function App() {
  return (
    <main>
      <Dashboard title="x" />
      <Layout.Sidebar>
        <StatsCard />
      </Layout.Sidebar>
    </main>
  );
}
"""
    chunks = _parse(source, relative_path)
    app = _chunk(chunks, "App")
    refs = {(r.local_name, r.kind) for r in app.references}
    assert ("Dashboard", "call") in refs
    assert ("StatsCard", "call") in refs
    assert ("Layout.Sidebar", "attribute_call") in refs
    assert not any(name == "main" for name, _ in refs)


def test_duplicate_calls_dedupe_to_earliest_line() -> None:
    source = """function helper() { return 1; }

function main() {
  helper();
  helper();
  return helper();
}
"""
    chunks = _parse(source)
    main = _chunk(chunks, "main")
    helper_refs = [r for r in main.references if r.local_name == "helper"]
    assert len(helper_refs) == 1
    assert helper_refs[0].line_start == 4


def test_evidence_is_deterministic_across_parses() -> None:
    source = """import { a } from './a';

export function one() { return a(two()); }
export function two() { return fetch('/api/x'); }
"""
    c1 = _parse(source)
    c2 = _parse(source)
    for x, y in zip(c1, c2, strict=True):
        assert x.references == y.references
        assert x.imports == y.imports
        assert x.endpoints == y.endpoints
        assert x.extraction_truncated == y.extraction_truncated


def test_reference_cap_sets_truncated_flag() -> None:
    calls = "\n".join(f"  fn_{i}();" for i in range(FLOW_EVIDENCE_MAX_ITEMS + 10))
    source = f"function big() {{\n{calls}\n}}\n"
    chunks = _parse(source)
    big = _chunk(chunks, "big")
    assert len(big.references) == FLOW_EVIDENCE_MAX_ITEMS
    assert big.extraction_truncated is True


# ---------------------------------------------------------------------------
# Scope ownership
# ---------------------------------------------------------------------------


def test_class_chunk_does_not_absorb_method_body_calls() -> None:
    source = """class Service {
  run() { return this.execute(); }
}
"""
    chunks = _parse(source)
    cls = _chunk(chunks, "Service")
    method = _chunk(chunks, "Service.run")
    assert {r.local_name for r in cls.references} == set()
    assert ("this.execute", "attribute_call") in {(r.local_name, r.kind) for r in method.references}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_fetch_defaults_to_get_and_reads_method_option() -> None:
    source = """export async function load() {
  return fetch('/api/v1/items');
}

export async function create(body) {
  return fetch('/api/v1/items', { method: 'POST', body });
}
"""
    chunks = _parse(source)
    load = _chunk(chunks, "load")
    create = _chunk(chunks, "create")
    assert [(e.kind, e.http_method, e.normalized_path) for e in load.endpoints] == [
        ("calls", "GET", "/api/v1/items")
    ]
    assert [(e.kind, e.http_method, e.normalized_path) for e in create.endpoints] == [
        ("calls", "POST", "/api/v1/items")
    ]


def test_client_call_strips_host_query_and_normalizes_params() -> None:
    source = """export async function fetchUser(id) {
  return axios.get('https://api.example.com/users/:id/profile?full=1');
}
"""
    chunks = _parse(source)
    fetch_user = _chunk(chunks, "fetchUser")
    assert fetch_user.endpoints == (
        EndpointEvidence(
            kind="calls",
            http_method="GET",
            path_literal="https://api.example.com/users/:id/profile?full=1",
            normalized_path="/users/{}/profile",
            line_start=2,
            line_end=2,
        ),
    )


def test_express_inline_handler_declares_endpoint() -> None:
    source = """export function registerRoutes(app) {
  app.post('/users/:id/logs', (req, res) => { res.send('ok'); });
  return app;
}
"""
    chunks = _parse(source)
    register = _chunk(chunks, "registerRoutes")
    declared = [(e.kind, e.http_method, e.normalized_path) for e in register.endpoints]
    assert declared == [("declares", "POST", "/users/{}/logs")]


def test_client_call_without_handler_stays_calls_kind() -> None:
    source = """export function push(payload) {
  return apiClient.post('/api/v1/logs', payload);
}
"""
    chunks = _parse(source)
    push = _chunk(chunks, "push")
    assert [(e.kind, e.http_method) for e in push.endpoints] == [("calls", "POST")]


def test_non_path_and_dynamic_paths_produce_no_endpoints() -> None:
    source = """export function reads(store, id) {
  store.get('cache-key');
  return fetch(`/items/${id}`);
}
"""
    chunks = _parse(source)
    reads = _chunk(chunks, "reads")
    assert reads.endpoints == ()


# ---------------------------------------------------------------------------
# Express mounts and top-level registrations (TRACE-007)
# ---------------------------------------------------------------------------


def test_mounted_router_prefix_is_folded_into_normalized_path() -> None:
    source = """const express = require('express');
const router = express.Router();

router.get('/:id/summary', (req, res) => {
  res.json({});
});

const app = express();
app.use('/api/v1/reports', router);
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "GET /:id/summary")
    assert handler.symbol_type == "route_handler"
    assert handler.endpoints == (
        EndpointEvidence(
            kind="declares",
            http_method="GET",
            # Literal stays as written; only the normalized form is mounted.
            path_literal="/:id/summary",
            normalized_path="/api/v1/reports/{}/summary",
            line_start=4,
            line_end=6,
        ),
    )


def test_top_level_registration_with_named_handler_attaches_to_that_symbol() -> None:
    source = """const express = require('express');
const app = express();

function listUsers(req, res) {
  res.json(loadUsers());
}

app.get('/api/v1/users', listUsers);
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "listUsers")
    declared = [e for e in handler.endpoints if e.kind == "declares"]
    assert [(e.http_method, e.normalized_path) for e in declared] == [("GET", "/api/v1/users")]
    # No synthetic chunk when a named same-file handler owns the route.
    assert not any(c.symbol_type == "route_handler" for c in chunks)


def test_top_level_inline_registration_synthesizes_route_handler_chunk() -> None:
    source = """const express = require('express');
const app = express();

app.post('/api/v1/logs', (req, res) => {
  recordLog(req.body);
  res.status(201).end();
});
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "POST /api/v1/logs")
    assert handler.symbol_type == "route_handler"
    assert handler.start_line == 4 and handler.end_line == 7
    assert "recordLog" in handler.content
    declared = [e for e in handler.endpoints if e.kind == "declares"]
    assert [(e.http_method, e.normalized_path) for e in declared] == [("POST", "/api/v1/logs")]
    # The handler body's own references are owned by the synthetic chunk.
    assert any(r.local_name == "recordLog" for r in handler.references)


def test_named_handler_registration_on_server_object_is_a_declaration() -> None:
    source = """const express = require('express');
const app = express();

function health(req, res) {
  res.send('ok');
}

function setup() {
  app.get('/health', health);
}
"""
    chunks = _parse(source)
    setup = _chunk(chunks, "setup")
    declared = [e for e in setup.endpoints if e.kind == "declares"]
    assert [(e.http_method, e.normalized_path) for e in declared] == [("GET", "/health")]


def test_conflicting_mounts_never_fold_a_prefix() -> None:
    source = """const express = require('express');
const router = express.Router();

router.get('/items', (req, res) => res.json([]));

const app = express();
app.use('/api/a', router);
app.use('/api/b', router);
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "GET /items")
    assert handler.endpoints[0].normalized_path == "/items"


def test_computed_mount_prefix_is_never_guessed() -> None:
    source = """const express = require('express');
const router = express.Router();

router.get('/items', (req, res) => res.json([]));

const app = express();
app.use(BASE_PATH + '/api', router);
"""
    chunks = _parse(source)
    handler = _chunk(chunks, "GET /items")
    assert handler.endpoints[0].normalized_path == "/items"


def test_client_calls_on_unknown_objects_remain_calls() -> None:
    source = """async function pushLog(client) {
  return client.post('/api/v1/logs', payload);
}
"""
    chunks = _parse(source)
    pusher = _chunk(chunks, "pushLog")
    assert [(e.kind, e.http_method) for e in pusher.endpoints] == [("calls", "POST")]


def test_client_call_resolves_to_mounted_express_handler_through_flow_trace() -> None:
    """Cross-file regression: the exact gap TRACE-007 closes for JS/TS."""
    from datetime import UTC, datetime

    from sourcetrace.models.domain import CodeChunk, RetrievalResult
    from sourcetrace.retrieval.trace import FlowTraceService

    server_source = """const express = require('express');
const router = express.Router();

router.get('/summary', (req, res) => {
  res.json({});
});

const app = express();
app.use('/api/v1/reports', router);
"""
    client_source = """export async function fetchReportSummary() {
  const res = await fetch('/api/v1/reports/summary');
  return res.json();
}
"""
    parsed = _parse(server_source, "server/routes.js") + _parse(client_source, "client/api.js")
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
        def list_by_repository(self, owner_session_id, repository_id, generation_id=None):
            return list(chunks)

        def search_lexical(
            self, owner_session_id, repository_id, query_text, limit=5, generation_id=None
        ):
            hits = [c for c in chunks if query_text in c.symbol_name]
            return [RetrievalResult(chunk=c, score=1.0) for c in hits[:limit]]

    result = FlowTraceService(_Repo()).trace("sess_flow", "repo_flow", "fetchReportSummary")

    http_edges = [e for e in result.edges if e.kind == "http"]
    assert len(http_edges) == 1
    assert http_edges[0].evidence_label == "GET /api/v1/reports/summary"
    target = next(c for c in chunks if c.chunk_id == http_edges[0].to_node_id)
    assert target.symbol_type == "route_handler"
    assert not any(g.kind == "endpoint_unmatched" for g in result.gaps)


# ---------------------------------------------------------------------------
# Version, fallback, and worker round-trip
# ---------------------------------------------------------------------------


def test_parser_version_bumped_for_flow_evidence() -> None:
    assert JS_TS_PARSER_VERSION == "js-ts-treesitter-v3"
    chunks = _parse("function f() { return 1; }\n")
    assert chunks[0].parser_version == "js-ts-treesitter-v3"


def test_module_fallback_chunk_has_empty_evidence() -> None:
    chunks = _parse("export default { key: 'value' };\n")
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.symbol_name == "<module>"
        assert chunk.references == ()
        assert chunk.imports == ()
        assert chunk.endpoints == ()
        assert chunk.extraction_truncated is False


def test_worker_roundtrip_restores_evidence_dataclasses() -> None:
    source = """import { helper } from './utils';

export async function sync() {
  const res = await fetch('/api/v1/sync', { method: 'POST' });
  return helper(res);
}
"""
    chunks = parse_javascript_source(
        source=source,
        relative_path="src/sync.js",
        repository_id="repo_worker",
        owner_session_id="sess_worker",
    )
    sync = _chunk(chunks, "sync")
    assert sync.references and isinstance(sync.references[0], ReferenceEvidence)
    assert sync.imports and isinstance(sync.imports[0], ImportEvidence)
    assert sync.endpoints and isinstance(sync.endpoints[0], EndpointEvidence)
    assert sync.endpoints[0] == EndpointEvidence(
        kind="calls",
        http_method="POST",
        path_literal="/api/v1/sync",
        normalized_path="/api/v1/sync",
        line_start=4,
        line_end=4,
    )
    refs = {(r.local_name, r.kind) for r in sync.references}
    assert ("helper", "call") in refs
    assert ("fetch", "call") in refs
