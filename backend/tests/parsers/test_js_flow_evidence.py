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
            (i.local_name, i.source_module, i.imported_name)
            for i in _chunk(chunks, name).imports
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
    assert ("this.execute", "attribute_call") in {
        (r.local_name, r.kind) for r in method.references
    }


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
# Version, fallback, and worker round-trip
# ---------------------------------------------------------------------------


def test_parser_version_bumped_for_flow_evidence() -> None:
    assert JS_TS_PARSER_VERSION == "js-ts-treesitter-v2"
    chunks = _parse("function f() { return 1; }\n")
    assert chunks[0].parser_version == "js-ts-treesitter-v2"


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
