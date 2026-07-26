# SourceTrace RAG Evaluation Suite (EVAL-001)

This directory contains the canonical versioned, offline, reproducible RAG evaluation dataset, execution runner, and metrics reporting engine for SourceTrace.

## Structure

```text
evals/
├── README.md             # This document explaining evaluation strategy and metrics
├── dataset.v1.json       # Versioned evaluation dataset (10 verified questions)
├── eval_registry.yaml    # Artifact policy and metric definitions
├── fixtures/             # Verified checked-in synthetic repository fixtures
│   └── sample_repo/      # Checked-in Python source files (auth, config, errors, indexer, models, services)
├── run_eval.py           # Offline evaluation runner and metric calculator
├── results/              # Generated local evaluation results (git-ignored)
└── tests/                # Automated regression tests for evaluation runner and metrics
```

## Dataset Selection & Verification

The default evaluation dataset (`dataset.v1.json`) contains 10 carefully selected questions:

1. `eval-001`: Direct symbol lookup (`auth.py` -> `generate_session_token`)
2. `eval-002`: Cross-file behavior (`services.py` -> `RepositoryService.process_repository` + `auth.py` -> `validate_owner_permissions`)
3. `eval-003`: Configuration behavior (`config.py` -> `AppConfig.is_production`)
4. `eval-004`: Error handling (`errors.py` -> `AccessDeniedError` + `format_error_response`)
5. `eval-005`: Indexing behavior (`indexer.py` -> `scan_repository_files`)
6. `eval-006`: Repository ownership / security (`auth.py` -> `validate_owner_permissions`)
7. `eval-007`: Direct symbol lookup (`models.py` -> `RepositoryItem`)
8. `eval-008`: Cross-file behavior (`services.py` -> `RepositoryService.process_repository` + `errors.py` -> `AccessDeniedError`)
9. `eval-009`: Insufficient evidence question 1 (Quantum database encryption)
10. `eval-010`: Insufficient evidence question 2 (React WebGL pipeline)

Every expected evidence item (`relative_path`, `symbol_name`, `symbol_type`, `start_line`, `end_line`) is strictly validated against the checked-in source files in `evals/fixtures/sample_repo/`. No fixture code is executed or dynamically imported.

## Metrics & Definitions

- **Retrieval Metrics**:
  - `retrieval_recall_at_1`, `retrieval_recall_at_3`, `retrieval_recall_at_5`: Fraction of expected evidence items retrieved in top $k$ results.
  - **Hit Definition**: A result counts as a hit when it matches exact `relative_path`, `symbol_name` (when specified), and line-range overlap (when specified).

- **Citation Metrics**:
  - `citation_validity_rate`: Fraction of extracted citations that match server-controlled retrieved evidence.
  - `citation_precision`: Precision of cited evidence chunks against expected evidence.
  - `citation_recall`: Recall of expected evidence chunks cited by the assistant.
  - `invalid_citation_marker_count`: Number of out-of-range or malformed citation markers in model output.
  - `unknown_citation_marker_count`: Number of unknown citation markers.

- **Grounding Metrics**:
  - `supported_answer_rate`: Fraction of non-insufficient-evidence questions with valid evidence citations.
  - `insufficient_evidence_accuracy`: Accuracy of triggering insufficient evidence on out-of-domain questions.
  - `unsupported_answer_count`: Answers provided without valid citation markers.
  - `no_evidence_short_circuit_count`: Questions short-circuited when 0 chunks were retrieved.
  - **Label**: `offline deterministic evaluation`.

- **Latency Metrics**:
  - Measured with `time.monotonic()` for per-question, mean, p50, p95, and max latency.
  - **Label**: `local fake-provider timings`.

- **Cost Metrics**:
  - Reported as `cost: not measured` with reason `offline fake provider has no billable token usage`.

## How to Run Evaluation

From the repository root:

```bash
python evals/run_eval.py
```

or via `uv`:

```bash
uv run python evals/run_eval.py
```

The runner exits with code `0` when dataset validation and metric invariants pass, and `1` on failure.

Results are saved to `evals/results/eval_result.v1.json`.

---

## Flow Trace & Impact Evaluation Suite (EVAL-002)

A second suite, `trace_impact.dataset.v1.json` + `run_trace_impact_eval.py`, evaluates the
zero-token static analysis services against the same checked-in fixture repository:

- **Trace cases** assert the exact step sequence, edge set (with kind, confidence, and
  evidence line), and gap kinds produced by `FlowTraceService` — including honest
  behavior cases (builtin references skipped, unresolvable entries reported as
  `entry_unresolved`).
- **Impact cases** assert the exact upstream-dependent and downstream-dependency sets
  (with distance and confidence), risk factor kinds, and risk level produced by
  `ChangeImpactService`.
- **Diff cases** (EVAL-003) assert `preview_diff()` behavior end to end: exact
  changed-symbol mapping from unified-diff hunks (old-file coordinates, verified
  against real fixture line text), deterministic multi-seed impact aggregation with
  dependent dedup, and each integrity gap in isolation — `diff_file_unmatched`,
  `diff_lines_uncovered`, and `diff_stale`. Dataset validation enforces that all
  three gap kinds and at least one clean mapping case are always covered.
- **Citation validity** re-verifies every produced citation against the fixture source:
  the cited lines must lie inside the cited chunk and the evidence label must appear on
  the exact cited line. Required to be 100%.
- **Determinism** runs every case twice with reversed storage return order; results must
  be identical.

- **JS/TS cases** (EVAL-004) run against a dedicated `fixtures/js_sample_repo/` fixture
  (selected per case via `repository_fixture`) covering ES default/named/aliased imports,
  JSX component chains, axios and fetch (with method option) client variants, mounted
  Express routers with named and inline handlers (synthetic `route_handler` chunks),
  ambiguous unimported names (low-confidence resolution), and deliberate gap cases —
  an orphan endpoint call (`endpoint_unmatched`) and an aliased relative import
  (`unresolved_references`, the documented alias limitation). These live in a separate
  fixture so their intentional gaps don't leak into the Python cases' expectations.

Dataset expectations are validated against the real parser output of each case's fixture
at load time — an expected symbol that is not an indexed chunk fails validation, so the
dataset cannot silently drift from the fixtures.

Run from the repository root:

```bash
uv run python evals/run_trace_impact_eval.py
```

Results are saved to `evals/results/eval_result.trace_impact.v1.json`. Regression tests
live in `evals/tests/test_trace_impact_eval.py`.
