# SourceTrace Offline Evaluation Benchmark Suite & CI Quality Gate (EVAL-001)

This evaluation suite provides reproducible, offline benchmarks for measuring SourceTrace code retrieval, citation validity, grounded-answer mechanics, safe fallback controls, and unified CI quality gates without calling external LLM APIs.

---

## 1. Corpus & Scenario Versioning
- **Corpus Location**: `backend/tests/fixtures/eval_corpus_v1/` (`corpus_version: "v1.0.0"`)
- **Question Set**: `backend/tests/evaluation/question_set.v1.json` (`evaluation_set_version: "v1.0.0"`)
- **Scenario Set**: `backend/tests/evaluation/scenario_set.v1.json` (`scenario_set_version: "v1.0.0"`)
- **Quality Gate Config**: `backend/tests/evaluation/eval_thresholds.v1.json` (`thresholds_version: "v1.0.0"`)

---

## 2. Part 1: Offline Retrieval & Citation Benchmark
- **Runner**: `backend/tests/evaluation/run_retrieval_eval.py`
- **Metrics**:
  - `retrieval_recall_at_1`, `retrieval_recall_at_3`, `retrieval_recall_at_5`: Proportion of expected target file paths and symbols retrieved in top-K evidence items.
  - `citation_validity_rate`: Percentage of returned citations passing path existence, line range bounds (`1 <= start_line <= end_line <= max_lines`), and scope isolation checks.
  - `unsupported_question_safety_rate`: Verification that out-of-domain questions produce zero retrieved chunks and explicit insufficient evidence states without fabricated citations.

---

## 3. Part 2: Offline Grounded-Answer Quality & Safety Benchmark
- **Runner**: `backend/tests/evaluation/run_grounded_eval.py`
- **Metrics**:
  - `grounded_answer_pass_rate`: Fraction of scenarios satisfying their scenario contract.
  - `valid_citation_marker_rate`: Fraction of returned citations passing path, line bounds, and scope validation.
  - `citation_coverage_rate`: Fraction of valid grounded answer scenarios where 100% of provider-cited markers resolved to valid evidence.
  - `uncited_provider_safe_fallback_rate`: Fraction of uncited provider answer scenarios safely downgraded to `insufficient_evidence`.
  - `provider_failure_safe_fallback_rate`: Fraction of provider failure scenarios safely converted to `static_guidance`.
  - `unsupported_question_safety_rate`: Fraction of no-evidence scenarios skipping LLM provider calls safely.

---

## 4. Part 3: Unified Evaluation Reporting & Quality Gate Thresholds
- **Runner**: `backend/tests/evaluation/run_unified_eval.py`
- **Output Contract**:
  - `unified_eval_result.v1.json`: Machine-readable unified report (100% byte-for-byte deterministic across runs).
  - `unified_eval_telemetry.v1.json`: Non-baseline execution latency telemetry.
- **Configured Quality Gate Thresholds**:
  - `retrieval_recall_at_5` >= `0.80` (Baseline: `0.8889`)
  - `retrieval_citation_validity_rate` == `1.00` (Baseline: `1.0000`)
  - `retrieval_unsupported_question_safety_rate` == `1.00` (Baseline: `1.0000`)
  - `grounded_answer_pass_rate` == `1.00` (Baseline: `1.0000`)
  - `valid_citation_marker_rate` == `1.00` (Baseline: `1.0000`)
  - `citation_coverage_rate` == `1.00` (Baseline: `1.0000`)
  - `uncited_provider_safe_fallback_rate` == `1.00` (Baseline: `1.0000`)
  - `provider_failure_safe_fallback_rate` == `1.00` (Baseline: `1.0000`)

---

## 5. Commands to Run Benchmarks

From `D:\PROJECT\SourceTrace\backend`:

### Run Unified Gate Command (Primary CI Entrypoint):
```bash
uv run python tests/evaluation/run_unified_eval.py
```

### Specify Custom Output Directory (For CI / Isolated Runs):
```bash
uv run python tests/evaluation/run_unified_eval.py --results-dir /tmp/eval_results
```

### Run Individual Evaluators:
```bash
uv run python tests/evaluation/run_retrieval_eval.py
uv run python tests/evaluation/run_grounded_eval.py
```

### Run Full Pytest Evaluation Suite:
```bash
uv run pytest tests/evaluation/
```

---

## 6. Local Failure Interpretation

When `run_unified_eval.py` exits with code `1`, check `reason_code` in `unified_eval_result.v1.json`:
- `RETRIEVAL_EVAL_FAILED`: An internal exception occurred during retrieval evaluation or question set loading.
- `GROUNDED_EVAL_FAILED`: An internal exception occurred during grounded-answer evaluation or scenario set loading.
- `THRESHOLD_VIOLATION`: One or more gate metrics dropped below configured baseline thresholds.
- `VERSION_MISMATCH`: Set version identifiers do not match configured threshold versions.
- `CONFIG_NOT_FOUND`: Quality gate threshold configuration file `eval_thresholds.v1.json` was missing or malformed.

---

## 7. Limitations Note
This evaluation suite verifies **mechanical grounding contracts**, retrieval recall, citation resolution, scope isolation, and safe fallback behavior offline. It does **not** evaluate subjective human writing aesthetics, semantic factual correctness of live model outputs, or live LLM provider performance.
