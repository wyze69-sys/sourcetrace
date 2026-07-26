"""Regression tests for the trace & impact evaluation runner (EVAL-002)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_SRC = ROOT_DIR / "backend" / "src"
if str(BACKEND_SRC) not in sys.path and BACKEND_SRC.exists():
    sys.path.insert(0, str(BACKEND_SRC))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from evals.run_trace_impact_eval import (  # noqa: E402
    DEFAULT_DATASET_PATH,
    RESULT_FILE_NAME,
    StaticChunkRepository,
    load_fixture_chunks,
    run_evaluation,
    validate_dataset,
)

FIXTURE_DIR = ROOT_DIR / "evals" / "fixtures" / "sample_repo"


def _load_dataset() -> dict:
    return json.loads(DEFAULT_DATASET_PATH.read_text(encoding="utf-8"))


def test_canonical_dataset_validates_cleanly() -> None:
    trace_cases, impact_cases, diff_cases, fixture_dir, errors = validate_dataset(
        DEFAULT_DATASET_PATH, ROOT_DIR
    )
    assert errors == []
    assert len(trace_cases) >= 3
    assert len(impact_cases) >= 3
    assert len(diff_cases) >= 3
    assert fixture_dir is not None and fixture_dir.is_dir()


def test_validation_rejects_symbol_not_in_fixture(tmp_path: Path) -> None:
    data = _load_dataset()
    data["trace_cases"][0]["expected_steps"][0]["symbol_name"] = "does_not_exist_xyz"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    _, _, _, _, errors = validate_dataset(tampered, ROOT_DIR)

    assert any("does_not_exist_xyz" in e for e in errors)


def test_validation_rejects_unsafe_fixture_path(tmp_path: Path) -> None:
    data = _load_dataset()
    data["repository_fixture"] = "../outside"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    _, _, _, _, errors = validate_dataset(tampered, ROOT_DIR)

    assert any("safe relative path" in e for e in errors)


def test_validation_requires_diff_gap_kind_coverage(tmp_path: Path) -> None:
    data = _load_dataset()
    # Drop the diff_stale case: the coverage rule must reject the dataset.
    data["diff_cases"] = [
        c for c in data["diff_cases"] if "diff_stale" not in c["expected_gap_kinds"]
    ]
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    _, _, _, _, errors = validate_dataset(tampered, ROOT_DIR)

    assert any("diff_stale" in e for e in errors)


def test_full_evaluation_passes_with_perfect_metrics(tmp_path: Path) -> None:
    report, success = run_evaluation(results_dir=tmp_path)

    assert success is True
    assert report.failures == []
    assert report.trace_step_accuracy == 1.0
    assert report.trace_edge_accuracy == 1.0
    assert report.trace_gap_accuracy == 1.0
    assert report.impact_upstream_accuracy == 1.0
    assert report.impact_downstream_accuracy == 1.0
    assert report.risk_factor_accuracy == 1.0
    assert report.risk_level_accuracy == 1.0
    assert report.total_diff_cases >= 3
    assert report.diff_target_accuracy == 1.0
    assert report.diff_impact_accuracy == 1.0
    assert report.diff_risk_accuracy == 1.0
    assert report.diff_gap_accuracy == 1.0
    assert report.citation_validity_rate == 1.0
    assert report.citations_checked > 0
    assert report.determinism_verified is True


def test_result_file_is_written_with_expected_shape(tmp_path: Path) -> None:
    _, success = run_evaluation(results_dir=tmp_path)
    payload = json.loads((tmp_path / RESULT_FILE_NAME).read_text(encoding="utf-8"))

    assert payload["passed"] is success is True
    assert payload["dataset_version"] == "v1"
    metrics = payload["metrics"]
    for key in (
        "trace_step_accuracy",
        "impact_upstream_accuracy",
        "diff_target_accuracy",
        "diff_gap_accuracy",
        "citation_validity_rate",
        "determinism_verified",
        "failures",
    ):
        assert key in metrics


def test_wrong_expectation_fails_the_evaluation(tmp_path: Path) -> None:
    # Claim an upstream dependent that does not exist: the runner must FAIL,
    # proving it detects drift instead of rubber-stamping.
    data = _load_dataset()
    data["impact_cases"][0]["expected_upstream"].append(
        {
            "relative_path": "models.py",
            "symbol_name": "RepositoryItem.get_summary",
            "distance": 1,
            "confidence": "high",
        }
    )
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    report, success = run_evaluation(dataset_path=tampered, results_dir=tmp_path)

    assert success is False
    assert report.impact_upstream_accuracy < 1.0
    assert any("ti-impact-001" in f for f in report.failures)


def test_wrong_diff_target_expectation_fails_the_evaluation(tmp_path: Path) -> None:
    data = _load_dataset()
    # Claim the multi-seed diff case also changed generate_session_token.
    data["diff_cases"][0]["expected_targets"].append(
        {
            "relative_path": "auth.py",
            "symbol_name": "generate_session_token",
            "changed_lines": [5],
        }
    )
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    report, success = run_evaluation(dataset_path=tampered, results_dir=tmp_path)

    assert success is False
    assert report.diff_target_accuracy < 1.0
    assert any("ti-diff-001" in f for f in report.failures)


def test_wrong_diff_gap_expectation_fails_the_evaluation(tmp_path: Path) -> None:
    data = _load_dataset()
    # The stale case must NOT pass if we pretend it produces no gaps...
    stale_case = next(
        c for c in data["diff_cases"] if "diff_stale" in c["expected_gap_kinds"]
    )
    stale_case["expected_gap_kinds"] = ["diff_stale", "diff_lines_uncovered"]
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    report, success = run_evaluation(dataset_path=tampered, results_dir=tmp_path)

    assert success is False
    assert report.diff_gap_accuracy < 1.0


def test_wrong_confidence_expectation_fails_the_evaluation(tmp_path: Path) -> None:
    data = _load_dataset()
    data["trace_cases"][0]["expected_edges"][0]["confidence"] = "low"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    report, success = run_evaluation(dataset_path=tampered, results_dir=tmp_path)

    assert success is False
    assert report.trace_edge_accuracy < 1.0


def test_static_chunk_repository_exposes_only_read_methods() -> None:
    chunks = load_fixture_chunks(FIXTURE_DIR)
    repo = StaticChunkRepository(chunks)

    listed = repo.list_by_repository("eval_session_ti_001", "eval_repo_ti_001")
    assert len(listed) == len(chunks)

    hits = repo.search_lexical(
        "eval_session_ti_001", "eval_repo_ti_001", "validate_owner_permissions"
    )
    assert [r.chunk.symbol_name for r in hits] == ["validate_owner_permissions"]

    # The static services must not need anything beyond these two methods.
    assert not hasattr(repo, "search_vectors")
    assert not hasattr(repo, "save_many")


def test_fixture_chunks_carry_flow_evidence() -> None:
    chunks = load_fixture_chunks(FIXTURE_DIR)
    by_symbol = {c.symbol_name: c for c in chunks}

    process = by_symbol["RepositoryService.process_repository"]
    assert {r.local_name for r in process.references} == {
        "validate_owner_permissions",
        "AccessDeniedError",
    }
    assert {i.source_module for i in process.imports} == {"auth", "config", "errors"}
    assert all(c.parser_version == "python-ast-v3" for c in chunks)
