"""Offline, reproducible unified RAG evaluation runner & CI quality gate (EVAL-001 Part 3/3)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = BACKEND_DIR / "src"
EVAL_DIR = Path(__file__).resolve().parent

if str(SRC_DIR) not in sys.path and SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from run_grounded_eval import (
    DEFAULT_SCENARIO_SET_PATH,
    run_grounded_evaluation,
)
from run_retrieval_eval import (
    DEFAULT_QUESTION_SET_PATH,
    run_retrieval_evaluation,
)

DEFAULT_THRESHOLDS_PATH = BACKEND_DIR / "tests" / "evaluation" / "eval_thresholds.v1.json"
DEFAULT_RESULTS_DIR = BACKEND_DIR / "tests" / "evaluation" / "results"
DEFAULT_UNIFIED_OUTPUT_FILE = DEFAULT_RESULTS_DIR / "unified_eval_result.v1.json"
DEFAULT_UNIFIED_TELEMETRY_FILE = DEFAULT_RESULTS_DIR / "unified_eval_telemetry.v1.json"


def load_threshold_config(
    thresholds_path: Path,
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    """Load and validate evaluation thresholds configuration file safely."""
    if not thresholds_path.exists():
        return {}, (
            "CONFIG_NOT_FOUND",
            "Evaluation threshold configuration is unavailable.",
        )
    try:
        with open(thresholds_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}, ("CONFIG_INVALID", "Evaluation threshold configuration is invalid.")

    if not isinstance(data, dict):
        return {}, ("CONFIG_INVALID", "Evaluation threshold configuration is invalid.")

    if "thresholds" not in data or not isinstance(data["thresholds"], dict):
        return {}, ("CONFIG_INVALID", "Evaluation threshold configuration is invalid.")

    return data, None


def run_unified_evaluation(
    thresholds_path: Path = DEFAULT_THRESHOLDS_PATH,
    question_set_path: Path = DEFAULT_QUESTION_SET_PATH,
    scenario_set_path: Path = DEFAULT_SCENARIO_SET_PATH,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    base_dir: Path = BACKEND_DIR,
) -> tuple[dict[str, Any], bool]:
    """Execute unified evaluation suite and apply quality gate thresholds."""
    t0 = time.monotonic()
    config_data, config_err = load_threshold_config(thresholds_path)

    if config_err is not None:
        reason_code, failure_reason = config_err
        out_payload = {
            "corpus_version": "unknown",
            "evaluation_set_version": "unknown",
            "scenario_set_version": "unknown",
            "thresholds_version": "unknown",
            "passed": False,
            "status": "FAILED",
            "reason_code": reason_code,
            "failure_reason": failure_reason,
            "evaluations": {},
            "gate_checks": [],
        }
        os.makedirs(results_dir, exist_ok=True)
        with open(results_dir / "unified_eval_result.v1.json", "w", encoding="utf-8") as f:
            json.dump(out_payload, f, indent=2)
        return out_payload, False

    thresholds = config_data.get("thresholds", {})
    thresh_ver = config_data.get("thresholds_version", "v1.0.0")

    # 1. Execute Retrieval Evaluation
    retrieval_passed_internal = False
    retrieval_metrics_dict: dict[str, Any] = {}
    try:
        ret_metrics, ret_per_q, ret_passed = run_retrieval_evaluation(
            question_set_path, results_dir, base_dir
        )
        retrieval_passed_internal = ret_passed
        retrieval_metrics_dict = asdict(ret_metrics)
    except Exception:
        retrieval_metrics_dict = {}

    # 2. Execute Grounded-Answer Evaluation
    grounded_passed_internal = False
    grounded_metrics_dict: dict[str, Any] = {}
    try:
        gnd_metrics, gnd_per_sc, gnd_passed = run_grounded_evaluation(
            scenario_set_path, results_dir, base_dir
        )
        grounded_passed_internal = gnd_passed
        grounded_metrics_dict = asdict(gnd_metrics)
    except Exception:
        grounded_metrics_dict = {}

    # Load version info from question_set and scenario_set safely
    try:
        with open(question_set_path, encoding="utf-8") as f:
            q_data = json.load(f)
        corpus_ver = q_data.get("corpus_version", "unknown")
        eval_set_ver = q_data.get("evaluation_set_version", "unknown")
    except Exception:
        corpus_ver = "unknown"
        eval_set_ver = "unknown"

    try:
        with open(scenario_set_path, encoding="utf-8") as f:
            sc_data = json.load(f)
        scenario_set_ver = sc_data.get("scenario_set_version", "unknown")
    except Exception:
        scenario_set_ver = "unknown"

    # Version mismatch check
    version_mismatch = (
        corpus_ver != config_data.get("corpus_version")
        or eval_set_ver != config_data.get("evaluation_set_version")
        or scenario_set_ver != config_data.get("scenario_set_version")
        or thresh_ver != config_data.get("thresholds_version")
    )

    gate_checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    primary_reason_code = "PASS"

    if not retrieval_passed_internal or not retrieval_metrics_dict:
        reasons.append("RETRIEVAL_EVAL_FAILED: Retrieval evaluation contract failed.")
        if primary_reason_code == "PASS":
            primary_reason_code = "RETRIEVAL_EVAL_FAILED"

    if not grounded_passed_internal or not grounded_metrics_dict:
        reasons.append("GROUNDED_EVAL_FAILED: Grounded-answer evaluation contract failed.")
        if primary_reason_code == "PASS":
            primary_reason_code = "GROUNDED_EVAL_FAILED"

    if version_mismatch:
        reasons.append(
            "VERSION_MISMATCH: Version identifiers do not match threshold configuration."
        )
        if primary_reason_code == "PASS":
            primary_reason_code = "VERSION_MISMATCH"

    # Define mandatory threshold checks
    metric_mappings = [
        ("retrieval_recall_at_5", retrieval_metrics_dict.get("retrieval_recall_at_5"), ">="),
        (
            "retrieval_citation_validity_rate",
            retrieval_metrics_dict.get("citation_validity_rate"),
            "==",
        ),
        (
            "retrieval_unsupported_question_safety_rate",
            retrieval_metrics_dict.get("unsupported_question_safety_rate"),
            "==",
        ),
        (
            "grounded_answer_pass_rate",
            grounded_metrics_dict.get("grounded_answer_pass_rate"),
            "==",
        ),
        (
            "valid_citation_marker_rate",
            grounded_metrics_dict.get("valid_citation_marker_rate"),
            "==",
        ),
        (
            "citation_coverage_rate",
            grounded_metrics_dict.get("citation_coverage_rate"),
            "==",
        ),
        (
            "uncited_provider_safe_fallback_rate",
            grounded_metrics_dict.get("uncited_provider_safe_fallback_rate"),
            "==",
        ),
        (
            "provider_failure_safe_fallback_rate",
            grounded_metrics_dict.get("provider_failure_safe_fallback_rate"),
            "==",
        ),
    ]

    for metric_name, actual_val, op in metric_mappings:
        target_thresh = thresholds.get(metric_name)
        if target_thresh is None or actual_val is None:
            check_passed = False
            reasons.append(f"MISSING_RESULT_DATA: Metric '{metric_name}' missing from output.")
            if primary_reason_code == "PASS":
                primary_reason_code = "MISSING_RESULT_DATA"
        else:
            if op == ">=":
                check_passed = actual_val >= target_thresh
            else:
                check_passed = actual_val == target_thresh

            if not check_passed:
                reasons.append(
                    f"THRESHOLD_VIOLATION: Metric '{metric_name}' ({actual_val}) {op} "
                    f"threshold ({target_thresh}) failed."
                )
                if primary_reason_code == "PASS":
                    primary_reason_code = "THRESHOLD_VIOLATION"

        gate_checks.append(
            {
                "metric": metric_name,
                "actual": actual_val,
                "threshold": target_thresh,
                "operator": op,
                "passed": check_passed,
                "reason_code": "PASS" if check_passed else "THRESHOLD_VIOLATION",
            }
        )

    overall_passed = (
        not reasons
        and retrieval_passed_internal
        and grounded_passed_internal
        and all(c["passed"] for c in gate_checks)
    )

    out_payload = {
        "corpus_version": corpus_ver,
        "evaluation_set_version": eval_set_ver,
        "scenario_set_version": scenario_set_ver,
        "thresholds_version": thresh_ver,
        "passed": overall_passed,
        "status": "PASSED" if overall_passed else "FAILED",
        "reason_code": primary_reason_code if not overall_passed else "PASS",
        "failure_reason": "; ".join(reasons) if reasons else None,
        "evaluations": {
            "retrieval": {
                "passed": retrieval_passed_internal,
                "metrics": retrieval_metrics_dict,
            },
            "grounded_answer": {
                "passed": grounded_passed_internal,
                "metrics": grounded_metrics_dict,
            },
        },
        "gate_checks": gate_checks,
    }

    os.makedirs(results_dir, exist_ok=True)
    with open(results_dir / "unified_eval_result.v1.json", "w", encoding="utf-8") as f:
        json.dump(out_payload, f, indent=2)

    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 3)
    telemetry_payload = {
        "corpus_version": corpus_ver,
        "evaluation_set_version": eval_set_ver,
        "scenario_set_version": scenario_set_ver,
        "thresholds_version": thresh_ver,
        "total_execution_ms": elapsed_ms,
    }
    with open(results_dir / "unified_eval_telemetry.v1.json", "w", encoding="utf-8") as f:
        json.dump(telemetry_payload, f, indent=2)

    return out_payload, overall_passed


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified RAG Evaluation Runner & Quality Gate")
    parser.add_argument("--thresholds-file", type=Path, default=DEFAULT_THRESHOLDS_PATH)
    parser.add_argument("--question-set-path", type=Path, default=DEFAULT_QUESTION_SET_PATH)
    parser.add_argument("--scenario-set-path", type=Path, default=DEFAULT_SCENARIO_SET_PATH)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)

    args = parser.parse_args()

    report, passed = run_unified_evaluation(
        thresholds_path=args.thresholds_file,
        question_set_path=args.question_set_path,
        scenario_set_path=args.scenario_set_path,
        results_dir=args.results_dir,
    )

    print("\n--- SOURCETRACE UNIFIED EVALUATION & CI QUALITY GATE REPORT ---")
    print(f"Status:             {report['status']}")
    print(f"Corpus Version:     {report['corpus_version']}")
    print(f"Evaluation Set:     {report['evaluation_set_version']}")
    print(f"Scenario Set:       {report['scenario_set_version']}")
    print(f"Thresholds Version: {report['thresholds_version']}")
    print(f"Reason Code:        {report['reason_code']}")
    if report["failure_reason"]:
        print(f"Failure Details:    {report['failure_reason']}")

    print("\nGate Checks Summary:")
    for gc in report.get("gate_checks", []):
        status_str = "PASS" if gc["passed"] else "FAIL"
        print(
            f"  - [{status_str}] {gc['metric']}: actual={gc['actual']} "
            f"{gc['operator']} threshold={gc['threshold']}"
        )
    print()

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
