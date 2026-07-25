"""Unit and integration regression tests for SourceTrace evaluation runner (EVAL-001)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_SRC = ROOT_DIR / "backend" / "src"
if str(BACKEND_SRC) not in sys.path and BACKEND_SRC.exists():
    sys.path.insert(0, str(BACKEND_SRC))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from evals.run_eval import (
    DEFAULT_DATASET_PATH,
    DEFAULT_RESULT_OUTPUT_FILE,
    FakeEmbeddingProvider,
    FakeGenerationProvider,
    MetricReport,
    run_evaluation,
    validate_dataset,
)


def test_dataset_loads_successfully():
    """Verify canonical dataset loads and passes validation cleanly."""
    items, errors = validate_dataset(DEFAULT_DATASET_PATH, ROOT_DIR)
    assert not errors, f"Dataset validation errors: {errors}"
    assert len(items) >= 10
    insufficient_count = sum(1 for item in items if item.expected_insufficient_evidence)
    assert insufficient_count >= 2


def test_dataset_ids_are_unique():
    """Verify all question IDs in dataset are unique and sorted."""
    items, errors = validate_dataset(DEFAULT_DATASET_PATH, ROOT_DIR)
    assert not errors
    ids = [item.id for item in items]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)


def test_invalid_dataset_fails(tmp_path: Path):
    """Verify invalid JSON schema fails validation."""
    invalid_file = tmp_path / "invalid.json"
    invalid_file.write_text(json.dumps({"schema_version": 99, "questions": []}), encoding="utf-8")
    items, errors = validate_dataset(invalid_file, ROOT_DIR)
    assert errors
    assert any("schema_version" in err for err in errors)


def test_absolute_and_traversal_paths_fail(tmp_path: Path):
    """Verify absolute paths and directory traversal are rejected."""
    bad_data = {
        "schema_version": 1,
        "questions": [
            {
                "id": "eval-bad-path",
                "question": "Bad path question?",
                "category": "security",
                "repository_fixture": "../evals/fixtures/sample_repo",
                "expected_insufficient_evidence": False,
                "expected_evidence": [],
            }
        ],
    }
    bad_file = tmp_path / "bad_path.json"
    bad_file.write_text(json.dumps(bad_data), encoding="utf-8")
    items, errors = validate_dataset(bad_file, ROOT_DIR)
    assert errors
    assert any("safe relative path" in err for err in errors)


def test_missing_fixture_fails(tmp_path: Path):
    """Verify non-existent fixture directory fails validation."""
    bad_data = {
        "schema_version": 1,
        "questions": [
            {
                "id": "eval-missing-fixture",
                "question": "Missing fixture?",
                "category": "test",
                "repository_fixture": "evals/fixtures/non_existent_dir_12345",
                "expected_insufficient_evidence": False,
                "expected_evidence": [],
            }
        ],
    }
    bad_file = tmp_path / "missing_fixture.json"
    bad_file.write_text(json.dumps(bad_data), encoding="utf-8")
    items, errors = validate_dataset(bad_file, ROOT_DIR)
    assert errors
    assert any("does not exist" in err for err in errors)


def test_invalid_line_ranges_fail(tmp_path: Path):
    """Verify out-of-bounds line numbers fail dataset validation."""
    bad_data = {
        "schema_version": 1,
        "questions": [
            {
                "id": "eval-bad-lines",
                "question": "Bad lines?",
                "category": "test",
                "repository_fixture": "evals/fixtures/sample_repo",
                "expected_insufficient_evidence": False,
                "expected_evidence": [
                    {
                        "relative_path": "auth.py",
                        "symbol_name": "generate_session_token",
                        "symbol_type": "function",
                        "start_line": 500,
                        "end_line": 600,
                    }
                ],
            }
        ],
    }
    bad_file = tmp_path / "bad_lines.json"
    bad_file.write_text(json.dumps(bad_data), encoding="utf-8")
    items, errors = validate_dataset(bad_file, ROOT_DIR)
    assert errors
    assert any("invalid for file line count" in err for err in errors)


def test_deterministic_fake_retrieval_produces_hits():
    """Verify fake retrieval on sample fixture produces expected evidence hits."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert success is True
    assert metrics.retrieval_recall_at_5 == 1.0
    assert metrics.expected_evidence_hit_count > 0


def test_exact_retrieval_metric_calculations():
    """Verify recall@1, recall@3, recall@5 calculations on deterministic output."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert 0.0 <= metrics.retrieval_recall_at_1 <= 1.0
    assert 0.0 <= metrics.retrieval_recall_at_3 <= 1.0
    assert metrics.retrieval_recall_at_3 <= metrics.retrieval_recall_at_5


def test_citation_precision_recall_calculations():
    """Verify citation validity rate, precision, and recall metrics."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert metrics.citation_validity_rate == 1.0
    assert metrics.citation_recall == 1.0
    assert 0.0 < metrics.citation_precision <= 1.0


def test_unknown_markers_are_counted_as_invalid():
    """Verify marker parsing treats unknown or out-of-range markers as invalid."""
    provider = FakeGenerationProvider()
    res = provider.generate([])
    assert res == "I do not have enough retrieved evidence from the indexed repository to answer this question."


def test_insufficient_evidence_behavior_measured_correctly():
    """Verify insufficient evidence accuracy is 100% for out-of-domain questions."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert metrics.insufficient_evidence_accuracy == 1.0
    assert metrics.no_evidence_short_circuit_count >= 2


def test_latency_metrics_structural_assertions():
    """Verify latency metrics are non-negative, ordered, and structurally valid."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert metrics.mean_latency_ms >= 0.0
    assert metrics.p50_latency_ms >= 0.0
    assert metrics.p95_latency_ms >= metrics.p50_latency_ms
    assert metrics.max_latency_ms >= metrics.p95_latency_ms
    assert metrics.timing_label == "local fake-provider timings"


def test_cost_reported_as_unavailable():
    """Verify provider cost is explicitly reported as not measured for offline fake provider."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert metrics.cost_status == "not measured"
    assert "offline fake provider" in metrics.cost_reason


def test_result_json_is_stable_and_schema_valid():
    """Verify result JSON file is created, valid JSON, and schema-compliant."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert DEFAULT_RESULT_OUTPUT_FILE.exists()
    content = json.loads(DEFAULT_RESULT_OUTPUT_FILE.read_text(encoding="utf-8"))
    assert content["passed"] is True
    assert content["dataset_version"] == "v1"
    assert "metrics" in content
    assert "per_question" in content


def test_runner_exits_non_zero_on_invalid_input(tmp_path: Path):
    """Verify evaluation runner returns success=False on invalid dataset input."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("invalid json content", encoding="utf-8")
    metrics, per_question, success = run_evaluation(bad_file, tmp_path / "results", ROOT_DIR)
    assert success is False
    assert metrics.total_questions == 0


def test_runner_does_not_require_network_or_db():
    """Verify evaluation runner executes entirely offline without database or API keys."""
    metrics, per_question, success = run_evaluation(DEFAULT_DATASET_PATH, ROOT_DIR / "evals" / "results", ROOT_DIR)
    assert success is True
    assert metrics.evaluation_label == "offline deterministic evaluation"
