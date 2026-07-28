# SourceTrace — Project State

**Recorded By**: AI Agent
**Last Updated**: 2026-07-28 (post RUNTIME-UX-002)

---

## Current Stage

RUNTIME-UX-002 implemented and verified. Added authenticated, owner-scoped `GET /api/v1/repositories/{repository_id}/files` endpoint returning a deterministic list of unique, path-sorted indexed files for the active generation, with minimal language and chunk_count metadata. Derived strictly from persisted code chunks (0 GitHub API calls, 0 filesystem scans, 0 synthetic trees). Enforces owner isolation with uniform 404 for missing/non-owned repositories, and returns 200 OK with empty files list for repositories with no indexed files. Verified with focused pytest suite (5 passed in `test_repository_files_route.py`), full pytest suite (1380 passed), ruff linter, and git diff --check.

---

## Verified Capabilities

| Capability | Status | Verified by |
|---|---|---|
| FastAPI application shell + CORS middleware | ✅ Verified | pytest (test_cors.py) |
| Anonymous session management (signed cookie) | ✅ Verified | pytest |
| Repository record domain model + validation | ✅ Verified | pytest |
| MongoDB Atlas storage layer (repositories, chunks, sessions) | ✅ Verified | pytest |
| Ownership-scoped API routes (repository CRUD + DELETE + files list) | ✅ Verified | `test_delete_repository_route.py`, `test_repository_files_route.py` |
| ZIP/GitHub source acquisition (safety-first) | ✅ Verified | pytest |
| Repository indexing service | ✅ Verified | pytest |
| Code chunking (Python AST + Subprocess-Isolated Tree-Sitter AST) | ✅ Verified | pytest |
| Per-Manager Initialized Target Tracking (`_initialized_target_keys`) | ✅ Verified | `mongodb.py`, `test_index_initialization.py` |
| Direct Manager `close()` Target Invalidation & Reinitialization | ✅ Verified | `mongodb.py`, `test_index_initialization.py` |
| Client Ownership Tracking (`_owns_client`) | ✅ Verified | `mongodb.py`, `test_index_initialization.py` |
| Repository Custom Settings Constructor Precedence & Isolation | ✅ Verified | `mongo_repositories.py`, `test_index_initialization.py` |
| Genuine Default Target Classification (`("default", db_name)`) | ✅ Verified | `mongodb.py`, `test_index_initialization.py` |
| Process-Shared Default `MongoStorageManager` (`get_default_storage_manager`) | ✅ Verified | `mongodb.py`, `mongo_repositories.py`, `test_index_initialization.py` |
| Index Initialization Error Propagation (No Silent Fallback) | ✅ Verified | `test_index_initialization.py`, `mongo_repositories.py` |
| Centralized Capability Readiness Evaluator (`evaluate_capabilities`) | ✅ Verified | `capabilities.py`, `test_capabilities_consistency.py` |
| Identifier Tokenization & Lexical Term Search (`search_lexical` with `$in` index) | ✅ Verified | `test_lexical.py`, `mongo_repositories.py` |
| Lazy Runtime Index Initialization (`ensure_indexes`) | ✅ Verified | `mongodb.py`, `mongo_repositories.py`, live explain plan (`IXSCAN`) |
| Evidence Search API (`POST /api/v1/repositories/{id}/search`) | ✅ Verified | `search.py`, `test_search_route.py` |
| Capabilities Endpoint (`GET /api/v1/capabilities`) | ✅ Verified | `capabilities.py`, `test_capabilities_consistency.py` |
| Frontend Static Evidence Search UI & Retry Preservation | ✅ Verified | `App.tsx`, `apiClient.ts`, vitest, `npm run build` |
| Worker & Indexing Coordinator Mode Authority | ✅ Verified | `github_indexing.py`, `zip_indexing.py`, `repositories.py` |
| GroundedAnswerService (grounded citations) | ✅ Verified | pytest |
| React + TypeScript + Vite frontend shell | ✅ Verified | npm run build, npm test (39 passed) |
| Render Blueprint backend configuration | ✅ Verified | render.yaml |
| JWT Primitive & Signing Engine (`JWTSigner`) | ✅ Verified | `security.py`, `test_jwt_security.py` |
| FastAPI Bearer Dependency & JWT Session Endpoint (`POST /api/v1/auth/session`) | ✅ Verified | `dependencies.py`, `auth.py`, `test_auth_dependency.py`, `test_auth_route.py` |
| Stateless JWT Bearer Auth on Protected Resource Routes | ✅ Verified | `repositories.py`, `indexing_jobs.py`, `search.py`, `conversations.py`, `test_resource_auth.py`, `test_runtime_openapi.py` |
| Frontend React ApiClient JWT Bearer Storage & Transmission | ✅ Verified | `apiClient.ts`, `types.ts`, `apiClient.test.ts`, `npm run build` |
| Anonymous Identity Continuity Hardening (commit `23ddeab`) | ✅ Verified | `dependencies.py`, `test_auth_route.py`, `test_session_dependency.py`, `apiClient.ts` |
| Auth Code-Quality Cleanup (shared secret resolver, single 401 spec, DRY retry) | ✅ Verified | ruff, pytest, vitest, lint, build, eval |
| **JS/TS Tree-Sitter Worker Stability (tree-sitter pinned 0.25.2)** | ✅ Verified | `pyproject.toml`, `test_js_worker_crash_regression.py` (TRACE-000, `7d86729`) |
| **Index-Time Flow Evidence Extraction (Python `python-ast-v3` + JS/TS `js-ts-treesitter-v2`)** | ✅ Verified | `python_ast.py`, `javascript_ast.py`, `flow_evidence.py`, `test_python_flow_evidence.py`, `test_js_flow_evidence.py`, `test_flow_evidence_roundtrip.py` (TRACE-001/002) |
| **Same-File Router-Prefix Folding (`collect_router_prefixes`)** | ✅ Verified | `python_ast.py`, `test_python_flow_evidence.py` 28 passed incl. cross-file HTTP-edge regression (TRACE-006, `078f2a5`) |
| **Git-Diff Impact Preview (`POST /api/v1/repositories/{id}/impact/diff` + panel diff mode)** | ✅ Verified | `retrieval/diff.py`, `preview_diff()`, `routes/impact.py`, `ImpactPanel.tsx`, `test_diff_impact.py` (17), route tests (5), vitest 54 passed (IMPACT-003, `7fbbae9`) |
| **Diff Eval Coverage in trace-impact-v1 (4 diff cases, gap isolation, multi-seed dedup)** | ✅ Verified | `trace_impact.dataset.v1.json`, `run_trace_impact_eval.py` PASSED, `test_trace_impact_eval.py` 28 passed (EVAL-003, `c294b13`) |
| **Grounded Impact Explanations (`mode=explain` on both impact endpoints + panel control)** | ✅ Verified | `impact_explanation.py`, `routes/impact.py`, `ImpactPanel.tsx`, `test_impact_explanation.py` (10), route tests (6), vitest 58 passed (IMPACT-004, `c427a46`) |
| **Express Endpoint Normalization (mount folding, top-level registration recovery, `route_handler` chunks)** | ✅ Verified | `javascript_ast.py` (`js-ts-treesitter-v3`), `test_js_flow_evidence.py` 26 passed, trace-impact-v1 JS HTTP-edge cases PASSED (TRACE-007, `9e1a6e2`) |
| **JS/TS Flow Eval Coverage (js_sample_repo, 18 total cases, per-case fixtures)** | ✅ Verified | `trace_impact.dataset.v1.json`, `run_trace_impact_eval.py` PASSED (8/6/4 cases, 17/17 citations), `test_trace_impact_eval.py` 31 passed (EVAL-004, `2e2ab1c`) |
| **Static Flow Trace API (`POST /api/v1/repositories/{id}/trace`)** | ✅ Verified | `retrieval/trace.py`, `routes/trace.py`, `test_trace_service.py`, `test_trace_route.py` (TRACE-003, `170b67e`) |
| **Flow Trace Workspace UI (`FlowTracePanel`)** | ✅ Verified | `FlowTracePanel.tsx`, `FlowTracePanel.test.tsx`, vitest 39 passed (TRACE-004, `6e01ddd`) |
| **Grounded Flow Explanations (`mode=explain`, safe degradation)** | ✅ Verified | `generation/trace_explanation.py`, `test_trace_explanation.py`, `test_trace_route.py` (TRACE-005, `bbb3811`) |
| **Change Impact Preview API (`POST /api/v1/repositories/{id}/impact`)** | ✅ Verified | `retrieval/impact.py`, `routes/impact.py`, `test_impact_service.py`, `test_impact_route.py` (IMPACT-001, `1ecf34a`) |
| **Trace & Impact Evaluation Suite (trace-impact-v1, CI-wired)** | ✅ Verified | `run_trace_impact_eval.py` PASSED, `test_trace_impact_eval.py` (evals 25 passed), `ci.yml` (EVAL-002, `7362f9b`) |
| **Impact Workspace Panel (`ImpactPanel`)** | ✅ Verified | `ImpactPanel.tsx`, `ImpactPanel.test.tsx`, `apiClient.test.ts`, vitest 49 passed, build clean (IMPACT-002, `80aa1d0`) |
| **Repository Index Freshness Metadata Models (REPO-001 Phase 1)** | ✅ Verified | `domain.py`, `schemas.py`, `mongo_repositories.py`, `test_refresh_metadata_phase1.py` (8 passed) |
| **Generation-Aware Chunk Storage & Index Migration (REPO-001 Phase 2)** | ✅ Verified | `repositories.py`, `mongo_repositories.py`, `test_refresh_storage_phase2.py` (8 passed) |
| **GitHub SHA Metadata Capture at Index Time (REPO-001 Phase 3)** | ✅ Verified | `acquisition.py`, `indexing.py`, `domain.py`, `test_refresh_github_phase3.py` (11 passed) |
| **GitHub Refresh Endpoint + Generation-Safe Worker (REPO-001 Phase 4)** | ✅ Verified | `workers/github_refresh.py`, `routes/repositories.py`, `test_refresh_route.py` (5), `test_github_refresh_worker.py` (2), full suite 0 failures (`09e7b04`) |
| **Frontend Refresh UX — stale badge, Refresh button, refreshing state (UX-001)** | ✅ Verified | `types.ts`, `App.tsx`, `index.css`, `apiClient.test.ts`; vitest 59 passed, vite build clean (`5b53a72`) |
| **Refresh Response Contract Alignment (`CreateRepositoryResponse`)** | ✅ Verified | `routes/repositories.py`, `test_refresh_route.py`, `App.test.tsx`; pytest 0 failures, vitest 60 passed |
| **On-Demand GitHub Freshness Detection (REPO-001 Phase 5)** | ✅ Verified | `freshness.py`, `config.py`, `routes/repositories.py`, `test_refresh_staleness_phase5.py` (5 passed); pytest 0 failures (`cfa686e`) |
| **Complete Frontend Index Freshness UX (REPO-001 Phase 6)** | ✅ Verified | `types.ts`, `apiClient.ts`, `App.tsx`, `FlowTracePanel.tsx`, `ImpactPanel.tsx`, `index.css`, 67 vitest passed, vite build clean (`4764de2`) |
| **Repository-Staleness Gaps for Flow Trace & Impact (REPO-001 Phase 7)** | ✅ Verified | `retrieval/trace.py`, `retrieval/impact.py`, `routes/trace.py`, `routes/impact.py`, `test_repo_stale_gaps_phase7.py` (3 passed), `trace-impact.md` |
| **Static Evidence Fallback Mode for Grounded Chat (AI-CHAT-001)** | ✅ Verified | `retrieval/service.py`, `generation/service.py`, `routes/conversations.py`, `App.tsx`, `test_static_chat_fallback.py` (`7c90ee4`) |
| **Repository Deletion & Quota Recovery (RUNTIME-UX-001)** | ✅ Verified | `routes/repositories.py`, `schemas.py`, `types.ts`, `App.tsx`, `test_delete_repository_route.py`, `test_live_delete_repo_and_quota.py` (`2445492`) |

---

## Active Task

**RUNTIME-UX-002 completed.** Implemented `GET /api/v1/repositories/{repository_id}/files` endpoint returning a unique, path-sorted list of indexed files for the repository's active generation, with `path`, `language`, and `chunk_count` metadata. Derived exclusively from stored code chunk records. Returns uniform HTTP 404 for non-owned or missing repositories, and 200 OK with empty files list for repositories with no indexed files.

---

## Known Architectural Limitations

1. **MongoDB Atlas Dependency**: Static mode is zero-token (no LLM/embeddings), but requires a reachable MongoDB database (cloud Atlas or local MongoDB instance).
2. **BackgroundTasks Non-Durability**: Background indexing runs via FastAPI `BackgroundTasks`. If the backend process crashes or restarts, active jobs in `processing` status must be retried.
3. **No GC Sweeper**: Old-generation chunk deletion happens synchronously inside the worker after pointer switch. A background sweeper for orphaned generations (e.g. from crashed workers) is not implemented.
4. **Flow-Evidence Gaps**: module-fallback chunks carry no evidence; mounts/prefixes in other files are not resolved. Same-file prefixes are folded for Python and Express. Repos indexed under old parsers keep valid but less-linkable evidence until refreshed.

---

## Verification Facts (Fresh, 2026-07-28 post-RUNTIME-UX-002)

```
pytest tests/api/test_repository_files_route.py                     → 5 passed
pytest                                                                → 1380 passed, 5 skipped
uv run ruff check .                                                  → All checks passed!
git diff --check                                                      → 0 whitespace errors
```
