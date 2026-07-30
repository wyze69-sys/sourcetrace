# SourceTrace Codebase Map

This map lets agents navigate directly to the relevant area. Update it whenever files, module responsibilities, API boundaries, or test locations change. Capability status and canonical ownership are tracked separately in `docs/FEATURE_REGISTRY.yaml`.

## Root

- `README.md` — only public Markdown document; portfolio overview for GitHub.
- `.gitignore` — excludes secrets, runtime data, internal agent documents, dependencies, and build output.
- `AGENTS.md` — local mandatory AI execution contract; ignored by Git.
- `docs/AGENT_TASKS.yaml` — local machine-readable ordered task queue.
- `docs/PROJECT_STATE.md` — local concise current state and exact next action.
- `docs/CODEBASE_MAP.md` — this navigation map.
- `docs/LAST_HANDOFF.md` — latest task result for the next agent.
- `docs/ARCHITECTURE.md` — canonical target architecture.
- `docs/ROADMAP.md` — phase-level plan.
- `docs/MULTI_AGENT.md` — parallel-agent coordination rules.
- `docs/decisions/0001-anonymous-session-ownership.md` — ADR 0001: Anonymous Signed-Cookie Session and Resource Ownership Model.
- `docs/decisions/0002-source-retention-and-cleanup.md` — ADR 0002: Source Retention, Quotas, and Cleanup Architecture.
- `docs/database/0001-mongodb-resource-contracts.md` — MongoDB Atlas collection schemas, public opaque IDs, indexing, and vector search filter rules.
- `docs/api/v1/contracts.md` — SourceTrace v1 HTTP API contracts, session activity rules, and error envelope specifications (ARCH-003 foundation set; predates JWT auth, search, capabilities, trace, and impact routes).
- `docs/api/v1/trace-impact.md` — HTTP contracts for the static flow trace and change impact preview endpoints (TRACE-003/005, IMPACT-001).
- `docs/api/v1/openapi.yaml` — OpenAPI 3.1 YAML specification for v1 foundation endpoints.
- `docs/api/v1/verify_contracts.py` — automated contract verification script for ARCH-003.

## Decisions, Database Contracts, and API Specs

- `docs/decisions/` — Architecture Decision Records (ADRs) for security, identity, data model, retention, limits, and API choices.
- `docs/database/` — MongoDB Atlas collection contracts, index specifications, and vector search definitions.
- `docs/api/` — OpenAPI 3.1 specifications and HTTP JSON contracts.

## Frontend

Current status: React TypeScript application foundation (`FE-001`) completed under `frontend/` with browser QA verification passed.

- `frontend/AGENTS.md` — frontend-only agent constraints.
- `frontend/package.json` — application scripts (`dev`, `build`, `lint`, `test`) and dependencies.
- `frontend/vite.config.ts` — Vite dev proxy (`/api` -> `http://127.0.0.1:8000`) and Vitest configuration.
- `frontend/eslint.config.js` — ESLint flat configuration.
- `frontend/src/main.tsx` — React entry point.
- `frontend/src/app/App.tsx` — question-first workspace application shell with Understand hero, starter questions, workspace navigation bar (Understand, Files, Find code, Advanced analysis accordion), dynamic status, rail, loading/ready/error states.
- `frontend/src/app/App.test.tsx` — unit tests for loading, ready, error, no-fake-data UI states, and question-first workspace navigation requirements.
- `frontend/src/app/FlowTracePanel.tsx` — Feature Flow Trace workspace panel (entry query, static/explain mode, trace steps with confidence badges and citations, gaps, honest empty/error states).
- `frontend/src/app/FlowTracePanel.test.tsx` — unit tests for trace panel rendering, mode handling, explanation display, and degradation states.
- `frontend/src/app/ImpactPanel.tsx` — Change Impact Preview workspace panel (symbol input, risk card with transparent factors, upstream/downstream lists with distance/confidence badges, expandable evidence citations, affected endpoints/tests, gaps, honest empty/loading/error states).
- `frontend/src/app/ImpactPanel.test.tsx` — unit tests for impact panel submission, rendering, citation expansion, unresolved/zero-impact states, error handling, loading state, and repository-change reset.
- `frontend/src/app/RepoExplorerPanel.tsx` — Repository Explorer panel with file tree and safe line-numbered source code viewer (expandable folders, folder-before-file sorting, file selection state, plain-text code view with line-number gutter, language badge, partial content gap notice banner, loading/error states with retry, race-condition protection).
- `frontend/src/app/RepoExplorerPanel.test.tsx` — unit and integration tests for flat-to-nested file tree conversion, folder-before-file sorting, selection state, code viewer rendering, line numbers, partial content notice, plain-text HTML safety, loading/empty/error states, repo selection request trigger, and stale request cancellation.
- `frontend/src/services/types.ts` — TypeScript types matching OpenAPI 3.1 HTTP contract (including `RepositoryFileItem`, `RepositoryFileListResponse`, and `RepositoryFileContentResponse`).
- `frontend/src/services/apiClient.ts` — typed API client (`getHealth()`, `listRepositoryFiles()`, `getRepositoryFileContent()`, `credentials: 'include'`, safe `ErrorEnvelope` handling).
- `frontend/src/services/apiClient.test.ts` — unit tests for `ApiClient` and `ApiError`.
- `frontend/src/styles/index.css` — calm evidence-led workspace design tokens (`--color-base: #f8fafc`, `#0f172a`), workspace navigation tabs, starter questions, and CSS styles.
- `frontend/src/test/setup.ts` — Vitest setup with `@testing-library/jest-dom/vitest`.

The detailed target feature layout is in `frontend/AGENTS.md`. Do not create a second frontend root.

## Backend

Current status: early prototype; review before reusing. Work from `backend/` for Python commands.

### API

- `backend/src/sourcetrace/api/app.py` — FastAPI application factory (`create_app()`), error handlers, and `/api/v1` routes.
- `backend/src/sourcetrace/api/dependencies.py` — FastAPI dependency injection (`get_current_session`, `get_current_owner_id`/`CurrentOwnerId` JWT Bearer guard, `get_jwt_signer`, `get_session_repository`, `get_repository_repository`, `get_indexing_job_repository`, `get_session_signer`).
- `backend/src/sourcetrace/api/schemas.py` — HTTP request/response transport schemas (`Repository`, `IndexingJob`, `RepositoryListResponse`, `RepositoryFileItem`, `RepositoryFileListResponse`, `ErrorEnvelope`, `TokenResponse`) and the shared `UNAUTHORIZED_RESPONSE` OpenAPI 401 spec reused by every protected route.
- `backend/src/sourcetrace/api/errors.py` — Global FastAPI exception handlers for standard error envelope responses (`404`, `422`, `500`).
- `backend/src/sourcetrace/api/routes/health.py` — `GET /api/v1/health` status route.
- `backend/src/sourcetrace/api/routes/auth.py` — `POST /api/v1/auth/session` anonymous JWT Bearer access-token provisioning route.
- `backend/src/sourcetrace/api/routes/capabilities.py` — `GET /api/v1/capabilities` server capabilities route.
- `backend/src/sourcetrace/api/routes/repositories.py` — `POST /api/v1/repositories` (import GitHub repository), `GET /api/v1/repositories`, `GET /api/v1/repositories/{repository_id}`, and `GET /api/v1/repositories/{repository_id}/files` ownership-scoped routes.
- `backend/src/sourcetrace/api/routes/search.py` — `POST /api/v1/repositories/{repository_id}/search` static evidence citation search route.
- `backend/src/sourcetrace/api/routes/trace.py` — `POST /api/v1/repositories/{repository_id}/trace` static flow trace route (`mode=static` zero-token; `mode=explain` adds step-marker-validated LLM narration, 422 without generation capability).
- `backend/src/sourcetrace/api/routes/impact.py` — `POST /api/v1/repositories/{repository_id}/impact` (symbol) and `POST /api/v1/repositories/{repository_id}/impact/diff` (pasted unified diff) change impact preview routes (`mode=static` zero-token default; `mode=explain` adds marker-validated LLM narration, 422 without generation capability; uniform owner 404; 422 on unparseable diffs).
- `backend/src/sourcetrace/api/routes/indexing_jobs.py` — `GET /api/v1/indexing-jobs/{job_id}` ownership-scoped status polling route.
- `backend/src/sourcetrace/api/routes/conversations.py` — `POST /api/v1/repositories/{repository_id}/conversations` (create conversation & grounded answer), `GET /api/v1/repositories/{repository_id}/conversations/{conversation_id}` (history read), `POST /api/v1/repositories/{repository_id}/conversations/{conversation_id}/messages` (send message).

### Core

- `backend/src/sourcetrace/core/config.py` — typed application settings (`SOURCETRACE_*`) including session configuration.
- `backend/src/sourcetrace/core/capabilities.py` — pure capability and provider readiness evaluator (`evaluate_capabilities()`).
- `backend/src/sourcetrace/core/security.py` — session security primitives (`SessionSigner`, `JWTSigner` stateless HS256 Bearer tokens, shared `_coerce_secret()` resolver, `generate_owner_session_id()`, `generate_conversation_id()`, `generate_message_id()`, HMAC-SHA256 cookie tokens).
- `backend/src/sourcetrace/core/exceptions.py` — application exception hierarchy.
- `backend/src/sourcetrace/core/logging.py` — prototype logging.

### Ingestion and parsing

- `backend/src/sourcetrace/ingestion/limits.py` — immutable ingestion security limit constants (ZIP, GitHub, file sizes, compression ratio, timeout).
- `backend/src/sourcetrace/ingestion/validation.py` — pure ingestion security validators (GitHub URL, redirect policy, IP address SSRF, archive member path safety).
- `backend/src/sourcetrace/ingestion/archive.py` — safe offline ZIP archive inspection, extraction context manager (`safe_extract_zip()`), metadata manifest, and limit enforcement.
- `backend/src/sourcetrace/ingestion/upload_staging.py` — safe streamed ZIP upload staging store (`FileSystemUploadStagingStore`), non-decompressing preflight, exclusive creation (`O_CREAT | O_EXCL`), chunk size validation, symlink resolution rejection, short write detection, and stale cleanup.
- `backend/src/sourcetrace/ingestion/github_archive.py` — safe public GitHub archive downloader context manager (`safe_download_github_archive()`), DNS/SSRF verification, manual redirect handling, and streaming byte-limit checks.
- `backend/src/sourcetrace/ingestion/acquisition.py` — managed acquired-source handoff abstractions (`AcquiredSource`, `AcquiredSourceConsumer`, `acquire_github_source()`, `acquire_zip_source()`) and job-state coordination runner (`AcquisitionRunner`).
- `backend/src/sourcetrace/ingestion/scanner.py` — deterministic Python file scanner (`scan_python_sources()`, path safety, exclusion policy, encoding detection, GitHub wrapper stripping).
- `backend/src/sourcetrace/ingestion/service.py` — `IngestionService` orchestrating atomic repository slot reservation, session timestamp refresh, opaque repo_/job_ ID generation, pending repository/job pair persistence, and compensating rollback.
- `backend/src/sourcetrace/ingestion/indexing.py` — `RepositoryIndexingService` and `index_acquired_source` orchestrating deterministic scanner -> AST parser -> provider embedding -> `CodeChunkRepository` pipeline with `IndexingLifecycleObserver` protocol.
- `backend/src/sourcetrace/ingestion/lifecycle.py` — `IndexingLifecycleCoordinator` connecting `AcquisitionRunner`, `RepositoryIndexingService`, and repository/job state transitions.
- `backend/src/sourcetrace/parsers/python_ast.py` — deterministic Python AST symbol parser (`parse_python_source()`, `parse_acquired_source()`, qualified names, exact inclusive line ranges, SHA-256 hashes, chunk IDs, module fallback) with flow-evidence extraction (`python-ast-v3`: references, import bindings, endpoint declares/calls, same-file literal router-prefix folding via `collect_router_prefixes()`).
- `backend/src/sourcetrace/parsers/javascript_ast.py` — repository-agnostic subprocess-isolated tree-sitter JS/TS symbol parser (`js-ts-treesitter-v3`) with React component/hook heuristics and flow-evidence extraction (references, imports, fetch/axios endpoint calls, route declares), same-file Express context (`collect_express_context()`: literal mount-prefix folding, top-level registration recovery, synthetic `route_handler` chunks).
- `backend/src/sourcetrace/parsers/flow_evidence.py` — language-neutral flow-evidence helpers shared by both parsers (`normalize_endpoint_path()`, `finalize_evidence()` dedup/cap, `FLOW_EVIDENCE_MAX_ITEMS=100` per category).

### Embeddings

- `backend/src/sourcetrace/embeddings/provider.py` — typed provider protocol (`EmbeddingProvider`) and injectable OpenAI-compatible adapter (`OpenAIEmbeddingAdapter`).
- `backend/src/sourcetrace/embeddings/service.py` — deterministic chunk embedding service (`embed_chunks()`) converting `ParsedCodeChunk` to `CodeChunk`.
- `backend/tests/embeddings/test_provider.py` — offline unit tests for embedding provider protocol, adapter, batching, index validation, and error masking.
- `backend/tests/embeddings/test_service.py` — offline unit tests for `embed_chunks()` metadata preservation, order, and error handling.

### Retrieval, generation, and storage

- `backend/src/sourcetrace/retrieval/service.py` — `SemanticRetrievalService` implementing provider-neutral, repository-readiness-validated evidence retrieval, query-planning fallback for zero or weak direct results (top score < 0.75), whole-token orientation intent detection (`_is_orientation_question`), orientation evidence retrieval strategy (`README`/docs 1.0, manifests 0.95, entrypoints 0.90, routing 0.80), search result validation, deterministic tie-breaker ranking, and safe evidence snippet construction.
- `backend/src/sourcetrace/retrieval/trace.py` — `FlowTraceService` deterministic zero-token static flow tracer plus the shared resolution layer (`build_flow_indexes()`, `resolve_reference()`, `resolve_endpoint_call()`, `chunk_sort_key()`) reused by the impact service; bounded traversal with confidence-scored edges, citations, and explicit gaps.
- `backend/src/sourcetrace/retrieval/impact.py` — `ChangeImpactService` deterministic zero-token change impact previewer (bounded multi-seed upstream/downstream BFS, path-weakest confidence, affected endpoints/components/tests classification, count-based risk factors, explicit gaps) with `preview()` for symbols and `preview_diff()` for pasted unified diffs.
- `backend/src/sourcetrace/retrieval/diff.py` — deterministic unified-diff parser (`parse_unified_diff()`, `DiffParseError`) producing per-file old-coordinate changed-line sets and staleness samples; pure text processing, no diff application.
- `backend/src/sourcetrace/generation/client.py` — `GenerationMessage` dataclass, `GenerationProvider` protocol, and `OpenAIGenerationAdapter` with lazy client initialization (`_get_client()`), configuration isolation (`llm_*` settings only), and safe error masking.
- `backend/src/sourcetrace/generation/prompts.py` — `build_grounded_prompt` constructing deterministic provider-neutral system/user prompt messages with untrusted source code isolation, evidence markers (`[E1]`), orientation system instructions (`ORIENTATION_SYSTEM_INSTRUCTIONS`), and prompt budget truncation.
- `backend/src/sourcetrace/generation/service.py` — `GroundedAnswerService` coordinating evidence retrieval, prompt construction, LLM generation, no-evidence short-circuiting, structured `answer_mode` responses, deterministic static guidance fallback (`_build_static_guidance`), server-controlled citation marker extraction, and safe answer validation.
- `backend/src/sourcetrace/generation/trace_explanation.py` — `TraceExplanationService` producing step-marker-validated LLM narration of an already-computed static flow trace (explain mode); invalid citations or provider failure discard the explanation, never the trace.
- `backend/src/sourcetrace/generation/impact_explanation.py` — `ImpactExplanationService` producing item-marker-validated LLM narration of an already-computed static impact preview (symbol or diff; markers number diff targets, then upstream, then downstream); same strict discard semantics.
- `backend/prompts/prompt_registry.yaml` — canonical production-prompt registry; no prompt is active yet.
- `backend/prompts/answer/` — future versioned grounded-answer and insufficient-evidence prompts.
- `backend/prompts/retrieval/` — future versioned query-rewrite/retrieval prompts.
- `backend/prompts/shared/` — future shared prompt fragments; keep provider-independent.
- `backend/src/sourcetrace/storage/mongodb.py` — PyMongo connection lifecycle (`MongoStorageManager`) and 6 canonical collection constants.
- `backend/src/sourcetrace/storage/repositories.py` — typed repository protocols (`AnonymousSessionRepository`, `RepositoryRepository`, `ConversationRepository`, `MessageRepository`, etc.) enforcing ownership scope.
- `backend/src/sourcetrace/storage/mongo_repositories.py` — concrete PyMongo repositories (`MongoAnonymousSessionRepository`, `MongoRepositoryRepository`, `MongoIndexingJobRepository`, `MongoCodeChunkRepository`, `MongoConversationRepository`, `MongoMessageRepository`) with owner_session_id scoping.
- `backend/src/sourcetrace/models/domain.py` — contract-aligned dataclass domain records (`AnonymousSession`, `RepositoryRecord`, `ParsedCodeChunk`, `CodeChunk`, `ConversationRecord`, `MessageRecord`, `CitationRecord`, `EvidenceSnippetRecord`, `RetrievedEvidence`, `GroundedEvidenceResult`, `GroundedAnswerResult`).
- `backend/src/sourcetrace/main.py` — prototype application entry point.
- `backend/src/sourcetrace/cli.py` — prototype CLI; local CLI is deferred and this is not an MVP priority.


### Maintenance workers

- `backend/src/sourcetrace/workers/session_cleanup.py` — idempotent MongoDB session-retention sweeper (`SessionRetentionSweeper`, `SessionCleanupReport`).
- `backend/src/sourcetrace/workers/github_indexing.py` — background worker executing acquisition, lifecycle coordinator, and indexing for public GitHub repositories (`run_github_indexing`).
- `backend/src/sourcetrace/workers/zip_indexing.py` — background worker executing acquisition, lifecycle coordinator, and indexing for staged ZIP uploads (`run_zip_indexing`), featuring process control pass-through and safe staging deletion error suppression.

### Backend configuration and tests

- `backend/README.md` — minimal package description for Hatchling package build.
- `backend/pyproject.toml` — FastAPI backend dependencies (`pymongo`, `fastapi`, `pydantic-settings`, etc.).
- `backend/uv.lock` — prototype Python lockfile.
- `backend/.env.example` — local variable-name template with no secrets; ignored by Git by owner preference.
- `backend/data/fixtures/` — safe synthetic backend fixtures.
- `backend/data/samples/` — deliberately public sample inputs only.
- `backend/data/runtime/` — temporary local source/runtime artifacts; contents ignored by Git.
- `backend/tests/api/test_health.py` — `GET /api/v1/health` and 404 fallback API tests.
- `backend/tests/api/test_errors.py` — isolated FastAPI error envelope tests (413, 422, 429, 500).
- `backend/tests/api/test_session_dependency.py` — offline unit and integration tests for get_current_session dependency, Set-Cookie attributes, and session provisioning.
- `backend/tests/api/test_read_routes.py` — offline integration tests for GET /repositories, GET /repositories/{id}, and GET /indexing-jobs/{id} read routes.
- `backend/tests/api/test_repository_files_route.py` — offline integration tests for GET /repositories/{id}/files read route.
- `backend/tests/api/test_repository_write_routes.py` — offline integration tests for POST /repositories write route, validation, quota, persistence, scheduling, and error handling.
- `backend/tests/api/test_repository_upload_route.py` — offline integration tests for POST /repositories/upload ZIP upload route, metadata validation, response-before-scheduling construction, and failure compensation.
- `backend/tests/api/test_runtime_openapi.py` — runtime FastAPI OpenAPI spec assertions for explicit operation IDs, IndexingJob progress limits, ErrorEnvelope refs, and 500 error behavior.
- `backend/tests/core/test_config.py` — offline `Settings` environment loading tests.
- `backend/tests/core/test_security.py` — offline unit tests for `SessionSigner`, HMAC-SHA256 tokens, secret length validation, and secret protection.
- `backend/tests/workers/test_session_cleanup.py` — offline unit tests for idempotent session sweeper, atomic claim token leases, and child-to-parent deletion.
- `backend/tests/workers/test_github_indexing.py` — offline unit and integration tests for background GitHub indexing worker composition, claim safety, and error containment.
- `backend/tests/workers/test_zip_indexing.py` — offline unit and integration tests for background ZIP indexing worker execution, setup failure compensation, process control exception pass-through, and staging cleanup exception suppression.
- `backend/tests/api/test_capabilities_consistency.py` — comprehensive capability and provider readiness consistency unit tests.
- `backend/tests/storage/test_storage_foundation.py` — offline storage manager and protocol signature tests.
- `backend/tests/storage/test_index_initialization.py` — unit tests for index initialization failure propagation, process caching, and isolation.
- `backend/tests/storage/test_mongo_repositories.py` — offline unit tests for concrete PyMongo session, repository, and job persistence with ownership filtering.
- `backend/tests/storage/test_refresh_metadata_phase1.py` — focused unit tests for REPO-001 Phase 1 index freshness metadata models, defaults, and BSON serialization compatibility.
- `backend/tests/storage/test_refresh_storage_phase2.py` — focused unit tests for REPO-001 Phase 2 generation-aware chunk storage, index migration, filtering, pointer switching, and generation deletion.
- `backend/tests/ingestion/test_security_primitives.py` — 117 focused offline tests for ingestion security limits, URL/redirect/address/path validators, and exception hierarchy.
- `backend/tests/ingestion/test_upload_staging.py` — 30 offline unit and integration tests for staging store configuration, non-decompressing ZIP preflight, exclusive creation retry, short write detection, symlink resolution rejection, and stale cleanup arguments.
- `backend/tests/ingestion/test_archive.py` — 23 offline adversarial tests for ZIP archive extraction, streaming limits, member types, corrupt archives, and directory cleanup.
- `backend/tests/ingestion/test_github_archive.py` — 16 offline adversarial tests for public GitHub archive download, DNS/SSRF checks, redirect controls, streaming limits, and file cleanup.
- `backend/tests/ingestion/test_service.py` — offline unit tests for `IngestionService`, quota enforcement, atomic slot reservation, and compensating rollback.
- `backend/tests/ingestion/test_acquisition.py` — 10 offline integration tests for acquired-source handoff contexts, ZIP/GitHub compositions, and job-state acquisition runner.
- `backend/tests/ingestion/test_scanner.py` — 44 offline tests for deterministic Python file discovery, exclusion policies, path security, encoding detection, and GitHub wrapper stripping.
- `backend/tests/indexing/test_service.py` — 9 offline integration tests for RepositoryIndexingService, ZIP/GitHub fixtures, scope/identity preservation, empty repo handling, safe error masking, and determinism.
- `backend/tests/indexing/test_static_mode.py` — integration tests for zero-token static mode repository indexing and citation retrieval without AI keys.
- `backend/tests/retrieval/test_service.py` — 78 offline unit tests for `SemanticRetrievalService`, query validation, repository readiness, provider validation, vector search, result validation, deterministic ranking, character limits, vector-free evidence output, and process control.
- `backend/tests/retrieval/test_lexical.py` — unit tests for `MongoCodeChunkRepository.search_lexical()` and `SemanticRetrievalService` static lexical search fallback.
- `backend/tests/generation/test_client.py` — 13 offline unit tests for `OpenAIGenerationAdapter`, lazy client construction, configuration isolation, error masking, process control pass-through.
- `backend/tests/generation/test_prompts.py` — 5 offline unit tests for `build_grounded_prompt`, message roles, marker assignment, prompt injection isolation, budget truncation, security checks.
- `backend/tests/generation/test_service.py` — 16 offline unit tests for `GroundedAnswerService`, no-evidence short-circuit, marker parsing, server-controlled citation metadata, exception safety, process control.
- `backend/tests/parsers/test_python_ast.py` — 44 offline tests for static Python AST parsing, qualified symbol names, decorator offsets, line ranges, SHA-256 content hashes, chunk IDs, and module fallbacks.
- `backend/tests/parsers/test_python_flow_evidence.py` — offline tests for Python flow-evidence extraction (references, import bindings, endpoint declares/calls, caps).
- `backend/tests/parsers/test_js_flow_evidence.py` — offline tests for JS/TS flow-evidence extraction (references, imports, fetch/axios calls, route declares).
- `backend/tests/parsers/test_js_worker_crash_regression.py` — regression test proving the pinned tree-sitter version survives the crash fixture (TRACE-000).
- `backend/tests/storage/test_flow_evidence_roundtrip.py` — offline tests for flow-evidence persistence round-trip through the MongoDB chunk repository.
- `backend/tests/retrieval/test_trace_service.py` — offline unit tests for `FlowTraceService` resolution, confidence, bounds, cycles, staleness, and determinism.
- `backend/tests/api/test_trace_route.py` — offline route tests for POST trace (auth, uniform 404, readiness, explain mode capability gating and degradation, zero-provider-call proof).
- `backend/tests/generation/test_trace_explanation.py` — offline unit tests for `TraceExplanationService` marker validation and failure discard.
- `backend/tests/retrieval/test_impact_service.py` — offline unit tests for `ChangeImpactService` (upstream/downstream discovery, alternative-edge dependents, classification, risk factors, bounds, gaps, determinism).
- `backend/tests/api/test_impact_route.py` — offline route tests for POST impact and POST impact/diff (auth, uniform 404, readiness, 422 on non-diff input, response shapes, zero-provider-call proofs).
- `backend/tests/retrieval/test_diff_impact.py` — offline unit tests for unified-diff parsing (old-coordinate mapping, /dev/null, prefixes, malformed input) and `preview_diff()` (target seeding, aggregation, seed exclusion, diff_file_unmatched/diff_lines_uncovered/diff_stale gaps, suffix path matching, ambiguity refusal, determinism).
- `backend/tests/api/test_repository_file_content_route.py` — offline route and unit tests for GET /api/v1/repositories/{id}/files/content (auth, uniform 404, path safety validation, line ordering, and partial content flag).
- `backend/tests/retrieval/test_retrieval_grounding_quality.py` — regression tests for question-answering quality: orientation intent → `answer_mode="orientation"`, unrelated chunks rejected for intent questions, startup intent selects entry-point evidence, auth intent selects auth/token evidence, owner/repo/generation scope isolation verified.


- `backend/tests/fixtures/parser_fixtures/` — 11 synthetic Python test fixtures for AST parser and scanner testing.


## RAG evaluation

Current status: reproducible RAG evaluation harness, dataset, runner, metrics, and regression tests (`EVAL-001`) completed.

- `backend/tests/fixtures/eval_corpus_v1/` — versioned synthetic Python repository corpus fixture (`main.py`, `routes.py`, `auth.py`, `services.py`, `config.py`, `errors.py`, `analytics.py`).
- `backend/tests/evaluation/question_set.v1.json` — versioned benchmark cases mapping structured questions to expected paths/symbols and line ranges.
- `backend/tests/evaluation/run_retrieval_eval.py` — deterministic offline retrieval and citation benchmark runner producing machine-readable JSON output.
- `backend/tests/evaluation/test_eval_suite.py` — regression tests covering question-set schema validation, stable output, recall@K metrics, citation validity/scope isolation, and unsupported question safety.
- `backend/tests/evaluation/README.md` — internal documentation for corpus location, question-set format, metrics, and limitations.
- `evals/eval_registry.yaml` — canonical evaluation artifact policy, metric families, and suite registry (rag-v1, trace-impact-v1).
- `evals/dataset.v1.json` — 10 verified evaluation questions mapping to exact relative paths, symbols, and line ranges in fixture source.
- `evals/trace_impact.dataset.v1.json` — versioned trace/impact/diff evaluation cases (exact steps, edges, confidence, gaps, upstream/downstream sets, risk factors) verified against the real parser output; cases may select a fixture via `repository_fixture`.
- `evals/fixtures/js_sample_repo/` — dedicated JS/TS fixture (ES/aliased imports, JSX chain, axios/fetch variants, mounted Express router + synthetic route handler, ambiguous names, deliberate unmatched/unresolved gap sources) kept separate so intentional gaps don't leak into Python-case expectations.
- `evals/run_trace_impact_eval.py` — offline deterministic runner scoring `FlowTraceService` and `ChangeImpactService` against fixture expectations, with fixture-line citation validity and reversed-storage-order determinism checks.
- `evals/tests/test_trace_impact_eval.py` — 9 regression tests for dataset validation, perfect-metric pass, result schema, and tamper-detection failure modes.
- `evals/fixtures/sample_repo/` — checked-in synthetic Python repository source files (`auth.py`, `config.py`, `errors.py`, `indexer.py`, `models.py`, `services.py`).
- `evals/run_eval.py` — offline evaluation runner executing questions deterministically via test doubles through production retrieval and grounded answer service interfaces.
- `evals/tests/test_eval_runner.py` — 16 regression unit and integration tests covering dataset validation, retrieval hit accuracy, citation precision/recall, insufficient evidence short-circuiting, latency metrics, and JSON output schema.
- `evals/README.md` — dataset selection criteria, metrics definitions, and execution guide.
- `evals/results/eval_result.v1.json` — machine-readable evaluation metrics report output (ignored by Git).

## CI & Deployment Documentation

- `.github/workflows/ci.yml` — GitHub Actions CI pipeline running backend lint/tests/contracts, frontend lint/tests/build, evaluation-suite regression tests, and both offline evaluation runners (RAG + trace/impact).
- `docs/deployment.md` — production deployment documentation detailing Vercel, Render, MongoDB Atlas Vector Search configuration, cookie security, environment variables, build/start commands, and honest product boundaries.

## Internal documentation update matrix

When code changes, update the matching local document before handoff:

- Any task status/current capability/blocker → `docs/PROJECT_STATE.md` and `docs/LAST_HANDOFF.md`
- Added, removed, renamed, or repurposed files/modules → `docs/CODEBASE_MAP.md`
- Capability lifecycle or canonical owner changes → `docs/FEATURE_REGISTRY.yaml`
- API route/schema/error behavior → `docs/api/` contract documents and map
- MongoDB collection/index/ownership behavior → `docs/database/` documents and map
- Architecture/security/product decision → `docs/decisions/` record and `docs/ARCHITECTURE.md`
- Phase/task ordering → `docs/ROADMAP.md` and coordinator update to `docs/AGENT_TASKS.yaml`
- Public portfolio behavior/setup → `README.md`

Never update documentation with planned behavior presented as completed behavior. Include verification evidence in the handoff.
