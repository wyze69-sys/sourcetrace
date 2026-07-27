# SourceTrace v1 HTTP Contracts — Flow Trace & Change Impact

* **Status:** Implemented and verified
* **Date:** 2026-07-26
* **Tasks:** TRACE-003, TRACE-005, IMPACT-001
* **Feature IDs:** TRACE-STATIC-FLOW, TRACE-EXPLAIN-GROUNDED, IMPACT-CHANGE-PREVIEW

Supplements `contracts.md` (ARCH-003 foundation set). Both endpoints follow the
platform-wide conventions: JWT Bearer authentication, the standard
`ErrorEnvelope` error shape, and the uniform owner 404 (a repository owned by
another session is indistinguishable from a nonexistent one). Neither endpoint
is a session-activity qualifying action (read-only analysis). Both are
deterministic over the stored index: identical inputs yield identical results,
and static modes make zero LLM/provider calls.

---

## 1. POST `/api/v1/repositories/{repository_id}/trace`

`operationId: traceFeatureFlow` — walk stored flow evidence from a resolved
entry symbol into a bounded node/edge graph with citations and explicit gaps.

### Request

```json
{
  "entry": "Dashboard",        // required, 1..300 chars: symbol or search text
  "mode": "static",            // "static" (default) | "explain"
  "max_depth": 4               // optional, >=1; clamped to server cap (8)
}
```

### Response 200

```json
{
  "repository_id": "repo_...",
  "entry": { "query": "...", "resolved_node_id": "chunk_... | null", "candidates": ["chunk_..."] },
  "nodes": [ { "node_id": "...", "relative_path": "...", "symbol_name": "...",
               "symbol_type": "...", "start_line": 1, "end_line": 9, "snippet": "..." } ],
  "edges": [ { "from_node_id": "...", "to_node_id": "...",
               "kind": "call|import|http", "confidence": "high|medium|low",
               "evidence_label": "...", "evidence_line_start": 4, "evidence_line_end": 4,
               "alternatives": ["chunk_..."] } ],
  "steps": ["chunk_...", "..."],
  "gaps": [ { "kind": "...", "detail": "...", "node_id": "chunk_... | null" } ],
  "explanation": { "text": "... [S1] ... [S2] ...", "cited_steps": [1, 2] }
}
```

* `explanation` is `null` in static mode, and in explain mode when the
  narration was discarded (provider failure or invalid step citations) — the
  static trace is then unchanged and a `explanation_failed` gap is appended.
* Gap kinds: `entry_unresolved`, `repo_stale`, `stale_index`, `depth_truncated`,
  `nodes_truncated`, `fanout_truncated`, `cycle_detected`,
  `unresolved_references`, `endpoint_unmatched`, `explanation_failed`.
* Server-side bounds: depth 8, 50 nodes, 5 edges / 10 evidence items per node.

### Errors

| Status | Condition |
| :--- | :--- |
| 400 | Empty repository ID, or repository status is not `ready` |
| 401 | Missing/invalid Bearer token (`WWW-Authenticate: Bearer`) |
| 404 | Repository not found or owned by another session (uniform) |
| 422 | `mode=explain` on a server without generation capability; malformed body; stored-chunk data error |
| 500 | Storage operation failure (masked detail) |

---

## 2. POST `/api/v1/repositories/{repository_id}/impact`

`operationId: previewChangeImpact` — deterministic static Change Impact
Preview for a selected symbol. Static mode only (no LLM narration mode).

### Request

```json
{
  "symbol": "load_stats",      // required, 1..300 chars: symbol or search text
  "max_depth": 3               // optional, >=1; clamped to server cap (6)
}
```

### Response 200

```json
{
  "repository_id": "repo_...",
  "target": { "query": "...", "resolved_node_id": "chunk_... | null", "candidates": ["chunk_..."] },
  "upstream":   [ /* ImpactItem: dependents that may break when the target changes */ ],
  "downstream": [ /* ImpactItem: dependencies the target transitively relies on */ ],
  "affected_endpoints":  [ { "http_method": "GET", "normalized_path": "/api/v1/stats", "node_id": "chunk_..." } ],
  "affected_components": ["chunk_..."],
  "affected_tests":      ["chunk_..."],
  "risk_level": "low|medium|high|unknown",
  "risk_factors": [ { "kind": "...", "severity": "low|medium|high", "detail": "..." } ],
  "gaps": [ { "kind": "...", "detail": "...", "node_id": "chunk_... | null" } ]
}
```

`ImpactItem`:

```json
{
  "node_id": "chunk_...", "relative_path": "...", "symbol_name": "...",
  "symbol_type": "...", "start_line": 1, "end_line": 9,
  "distance": 2,                      // BFS distance from the target
  "confidence": "high|medium|low",    // weakest edge on the discovery path
  "edge_kind": "call|http",
  "via_node_id": "chunk_...",         // neighbor closer to the target
  "evidence_node_id": "chunk_...",    // chunk containing the cited lines
  "evidence_label": "...",
  "evidence_line_start": 4, "evidence_line_end": 4
}
```

* Ordering is stable: items sort by `(distance, relative_path, start_line,
  symbol_name, node_id)`; endpoints by `(normalized_path, http_method,
  node_id)`; storage return order never affects the result.
* Ambiguous references keep low-confidence edges to every alternative, so a
  potential dependent is never silently dropped.
* `affected_components` = upstream chunks with symbol type
  `react_component`/`hook`; `affected_tests` = upstream chunks matching the
  deterministic test-path heuristic; `affected_endpoints` = endpoints declared
  by the target or its upstream dependents.
* Risk factors are count-based and transparent: `dependent_fanout`,
  `endpoint_exposure`, `no_test_coverage`, `ambiguous_resolution`,
  `impact_truncated`; `risk_level` is the maximum factor severity ("low" with
  no factors, "unknown" when the target did not resolve).
* Gap kinds: `entry_unresolved`, `repo_stale`, `stale_index`, `depth_truncated`,
  `nodes_truncated`, `unresolved_references`, `endpoint_unmatched`,
  `extraction_truncated`.
* Server-side bounds: depth 6, 50 nodes per direction.

### Errors

| Status | Condition |
| :--- | :--- |
| 400 | Empty repository ID, or repository status is not `ready` |
| 401 | Missing/invalid Bearer token (`WWW-Authenticate: Bearer`) |
| 404 | Repository not found or owned by another session (uniform) |
| 422 | Malformed body (e.g., empty `symbol`); stored-chunk data error |
| 500 | Storage operation failure (masked detail) |

---

## 2b. POST `/api/v1/repositories/{repository_id}/impact/diff`

`operationId: previewDiffImpact` — aggregate static impact preview for a
pasted unified diff (IMPACT-003). The indexed repository is treated as the
diff's pre-change baseline: hunks map to indexed chunks by OLD-file line
numbers, every touched chunk becomes a traversal seed, and the union
upstream/downstream impact is computed with the same machinery, ordering, and
bounds as the symbol preview. Seeds never appear in their own impact lists.

### Request

```json
{
  "diff": "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -12,3 +12,3 @@\n ...",
  "max_depth": 3               // optional, >=1; clamped to server cap (6)
}
```

`diff` is 1..200,000 chars. Input that contains no `--- / +++` header with at
least one `@@` hunk is rejected with 422 (standard validation envelope).

### Response 200

Identical to the symbol response except `target` is replaced by `targets`:

```json
{
  "repository_id": "repo_...",
  "targets": [ { "node_id": "chunk_...", "relative_path": "...",
                 "symbol_name": "...", "symbol_type": "...",
                 "start_line": 10, "end_line": 20,
                 "changed_lines": [13, 14] } ],
  "upstream": [ /* ImpactItem, distance = hops from the NEAREST target */ ],
  "downstream": [ /* ImpactItem */ ],
  "affected_endpoints": [], "affected_components": [], "affected_tests": [],
  "risk_level": "low|medium|high|unknown",
  "risk_factors": [], "gaps": []
}
```

* Diff-specific gap kinds (in addition to the symbol preview's):
  - `diff_file_unmatched` — a diff file has no indexed baseline (unknown
    path, ambiguous suffix match, or a file the diff itself adds).
  - `diff_lines_uncovered` — changed lines fall outside every indexed symbol
    chunk (module-level code).
  - `diff_stale` — the diff's context/deleted text disagrees with indexed
    content at a cited `path:line`; the diff base and indexed revision
    differ, so the preview may be inaccurate.
* Path matching: exact, else unique suffix match in either direction;
  ambiguity is reported, never guessed. `risk_level` is `unknown` when no
  target matched.

### Errors

Same table as the symbol endpoint, plus 422 for unparseable diff input.

---

## 2c. Explain mode (IMPACT-004)

Both impact endpoints accept `"mode": "static" | "explain"` (default
`static`). Explain mode mirrors the trace endpoint's contract exactly:

* 422 (standard envelope) when the server has no generation capability.
* The static preview is computed first and is never modified by narration.
* The response gains `"explanation": { "text": "...", "cited_steps": [1, 2] } | null`.
  `[S#]` markers number the impact items in response order — diff targets
  first (diff previews), then upstream, then downstream. Validation is
  marker-only and strict: out-of-range or absent markers, empty output, or a
  provider failure discard the explanation and append an
  `explanation_failed` gap; the static preview is returned unchanged.
* Static mode never constructs or contacts an LLM provider.

## 3. Shared semantics

Both endpoints resolve identifiers through the same shared resolution layer
(`backend/src/sourcetrace/retrieval/trace.py`): import-bound unique path match
→ `high`; unique name match → `medium`; ambiguous → `low` with alternatives
listed. Evidence coverage is bounded by index-time extraction
(`FLOW_EVIDENCE_MAX_ITEMS`, `extraction_truncated`), and chunks indexed before
parser versions `python-ast-v2` / `js-ts-treesitter-v2` carry no flow evidence
(reported as `stale_index`).
