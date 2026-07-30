"""Offline, reproducible GroundedAnswerService evaluation runner (EVAL-001 Part 2/3)."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = BACKEND_DIR / "src"
EVAL_DIR = Path(__file__).resolve().parent

if str(SRC_DIR) not in sys.path and SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from run_retrieval_eval import (
    FakeEvalCodeChunkRepository,
    FakeEvalRepositoryRepository,
    validate_retrieved_citation,
)

from sourcetrace.core.exceptions import GenerationError
from sourcetrace.generation.client import GenerationMessage, GenerationProvider
from sourcetrace.generation.service import GroundedAnswerService
from sourcetrace.models.domain import (
    CodeChunk,
    GroundedAnswerResult,
)
from sourcetrace.parsers.python_ast import parse_python_source
from sourcetrace.retrieval.service import SemanticRetrievalService

DEFAULT_SCENARIO_SET_PATH = BACKEND_DIR / "tests" / "evaluation" / "scenario_set.v1.json"
DEFAULT_RESULTS_DIR = BACKEND_DIR / "tests" / "evaluation" / "results"
DEFAULT_GROUNDED_OUTPUT_FILE = DEFAULT_RESULTS_DIR / "grounded_eval_result.v1.json"
DEFAULT_GROUNDED_TELEMETRY_FILE = DEFAULT_RESULTS_DIR / "grounded_eval_telemetry.v1.json"


class DeterministicFakeGenerationProvider(GenerationProvider):
    """Deterministic offline fake LLM provider for grounded-answer evaluation."""

    def __init__(
        self, behavior: str = "text", output_text: str = "", error_message: str = ""
    ) -> None:
        self._behavior = behavior
        self._output_text = output_text
        self._error_message = error_message
        self.call_count = 0
        self.last_messages: Sequence[GenerationMessage] = ()

    @property
    def model_identifier(self) -> str:
        return "deterministic_eval_fake_v1"

    def generate(self, messages: Sequence[GenerationMessage]) -> str:
        self.call_count += 1
        self.last_messages = messages

        if self._behavior == "raise_error":
            raise GenerationError(self._error_message or "Simulated provider error")

        if self._behavior == "return_empty":
            return ""

        if self._behavior == "return_invalid_type":
            return None  # type: ignore[return-value]

        return self._output_text


@dataclass(frozen=True)
class GroundedScenarioCase:
    id: str
    name: str
    category: str
    question: str
    repository_fixture: str
    simulated_behavior: str
    simulated_output_text: str
    simulated_error_message: str
    expected_provider_called: bool
    expected_answer_mode: str
    expected_insufficient_evidence: bool
    expected_citations_count: int
    expect_safe_fallback: bool
    expected_reason_code: str


@dataclass
class GroundedEvalMetricsReport:
    total_scenarios: int
    grounded_answer_pass_rate: float
    valid_citation_marker_rate: float
    citation_coverage_rate: float
    uncited_provider_safe_fallback_rate: float
    provider_failure_safe_fallback_rate: float
    unsupported_question_safety_rate: float


def validate_scenario_set(
    scenario_set_path: Path, base_dir: Path
) -> tuple[list[GroundedScenarioCase], str, str, list[str]]:
    """Validate scenario set JSON schema and file dependencies."""
    errors: list[str] = []
    cases: list[GroundedScenarioCase] = []

    if not scenario_set_path.exists():
        return [], "", "", [f"Scenario set file not found: {scenario_set_path}"]

    try:
        with open(scenario_set_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as err:
        return [], "", "", [f"Scenario set JSON invalid: {err}"]

    corpus_ver = data.get("corpus_version", "")
    scenario_ver = data.get("scenario_set_version", "")
    if not corpus_ver or not scenario_ver:
        errors.append("corpus_version and scenario_set_version required")

    scenarios_raw = data.get("scenarios")
    if not isinstance(scenarios_raw, list) or not scenarios_raw:
        errors.append("scenarios array required and must not be empty")
        return [], corpus_ver, scenario_ver, errors

    seen_ids: set[str] = set()

    for idx, sc in enumerate(scenarios_raw):
        sc_id = sc.get("id")
        if not isinstance(sc_id, str) or not sc_id.strip():
            errors.append(f"Scenario index {idx}: id must be non-empty string")
            continue
        if sc_id in seen_ids:
            errors.append(f"Scenario id {sc_id}: duplicate ID")
        seen_ids.add(sc_id)

        q_text = sc.get("question")
        if not isinstance(q_text, str) or not q_text.strip():
            errors.append(f"Scenario {sc_id}: question string required")

        repo_fix = sc.get("repository_fixture")
        if not isinstance(repo_fix, str) or not repo_fix.strip():
            errors.append(f"Scenario {sc_id}: repository_fixture path string required")
            continue

        fix_dir = base_dir / repo_fix
        if not fix_dir.exists() or not fix_dir.is_dir():
            errors.append(f"Scenario {sc_id}: fixture directory {repo_fix} not found")

        sim_provider = sc.get("simulated_provider", {})
        behavior = sim_provider.get("behavior", "text")
        output_text = sim_provider.get("output_text", "")
        error_msg = sim_provider.get("error_message", "")

        exp = sc.get("expected", {})

        cases.append(
            GroundedScenarioCase(
                id=sc_id,
                name=sc.get("name", sc_id),
                category=sc.get("category", "general"),
                question=q_text,
                repository_fixture=repo_fix,
                simulated_behavior=behavior,
                simulated_output_text=output_text,
                simulated_error_message=error_msg,
                expected_provider_called=bool(exp.get("provider_called", True)),
                expected_answer_mode=exp.get("answer_mode", "normal"),
                expected_insufficient_evidence=bool(exp.get("insufficient_evidence", False)),
                expected_citations_count=int(exp.get("expected_citations_count", 0)),
                expect_safe_fallback=bool(exp.get("expect_safe_fallback", False)),
                expected_reason_code=exp.get("reason_code", "UNKNOWN"),
            )
        )

    return cases, corpus_ver, scenario_ver, errors


def run_grounded_evaluation(
    scenario_set_path: Path = DEFAULT_SCENARIO_SET_PATH,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    base_dir: Path = BACKEND_DIR,
) -> tuple[GroundedEvalMetricsReport, list[dict[str, Any]], bool]:
    """Execute GroundedAnswerService benchmark scenarios and output deterministic report."""
    cases, corpus_ver, scenario_ver, errors = validate_scenario_set(scenario_set_path, base_dir)
    if errors:
        raise ValueError(f"Scenario set validation failed: {errors}")

    owner_session_id = "eval_session_owner_001"
    repository_id = "eval_repo_001"
    active_generation_id = "eval_gen_001"

    repo_repo = FakeEvalRepositoryRepository(owner_session_id, repository_id, active_generation_id)
    chunk_repo = FakeEvalCodeChunkRepository()

    fix_dir = base_dir / cases[0].repository_fixture
    file_line_counts: dict[str, int] = {}

    for py_file in sorted(fix_dir.glob("*.py")):
        content = py_file.read_text(encoding="utf-8")
        file_line_counts[py_file.name] = len(content.splitlines())
        parsed = parse_python_source(
            source=content,
            relative_path=py_file.name,
            repository_id=repository_id,
            owner_session_id=owner_session_id,
        )
        for p in parsed:
            now = datetime.now(UTC)
            c_active = CodeChunk(
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
                generation_id=active_generation_id,
            )
            c_other_gen = CodeChunk(
                chunk_id=f"{p.chunk_id}_gen002",
                repository_id=p.repository_id,
                owner_session_id=p.owner_session_id,
                relative_path=p.relative_path,
                language=p.language,
                symbol_name=p.symbol_name,
                symbol_type=p.symbol_type,
                start_line=p.start_line,
                end_line=p.end_line,
                content=f"{p.content}\n# STALE GENERATION CHUNK",
                content_hash=f"{p.content_hash}_gen002",
                parser_version=p.parser_version,
                created_at=now,
                generation_id="eval_gen_002",
            )
            chunk_repo.add_chunks((c_active, c_other_gen))

    retrieval_service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
    )

    per_scenario: list[dict[str, Any]] = []
    latencies: list[float] = []

    passed_count = 0
    total_returned_citations = 0
    valid_returned_citations = 0

    grounded_valid_count = 0
    grounded_total_count = 0

    uncited_fallback_correct = 0
    uncited_fallback_total = 0

    failure_fallback_correct = 0
    failure_fallback_total = 0

    no_evidence_correct = 0
    no_evidence_total = 0

    for sc in cases:
        provider = DeterministicFakeGenerationProvider(
            behavior=sc.simulated_behavior,
            output_text=sc.simulated_output_text,
            error_message=sc.simulated_error_message,
        )
        answer_service = GroundedAnswerService(
            retrieval_service=retrieval_service,
            generation_provider=provider,
        )

        t0 = time.monotonic()
        res: GroundedAnswerResult = answer_service.generate_answer(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            question=sc.question,
            limit=5,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        latencies.append(elapsed_ms)

        provider_was_called = provider.call_count > 0

        # Validate citations returned
        citations_valid = True
        for cit in res.citations:
            total_returned_citations += 1
            # Find chunk ID for citation if available
            cit_chunk_id = next(
                (
                    item.chunk_id
                    for item in getattr(res, "_evidence_items", ())
                    if item.citation == cit
                ),
                "",
            )
            if not cit_chunk_id and hasattr(chunk_repo, "_chunks"):
                # Fall back to finding matching chunk in repo
                for c in chunk_repo._chunks:
                    if (
                        c.relative_path == cit.relative_path
                        and c.symbol_name == cit.symbol_name
                        and c.start_line == cit.start_line
                        and c.end_line == cit.end_line
                    ):
                        cit_chunk_id = c.chunk_id
                        break

            val_res = validate_retrieved_citation(
                citation=cit,
                chunk_id=cit_chunk_id,
                file_line_counts=file_line_counts,
                expected_owner_session_id=owner_session_id,
                expected_repository_id=repository_id,
                expected_generation_id=active_generation_id,
                code_chunk_repo=chunk_repo,
            )
            if val_res.is_valid:
                valid_returned_citations += 1
            else:
                citations_valid = False

        # Category specific checks
        if sc.category in ("valid_grounded", "multiple_citations", "orientation"):
            grounded_total_count += 1
            if citations_valid and len(res.citations) == sc.expected_citations_count:
                grounded_valid_count += 1

        if sc.category == "uncited_provider":
            uncited_fallback_total += 1
            if (
                res.answer_mode == "insufficient_evidence"
                and res.insufficient_evidence is True
                and sc.simulated_output_text not in res.answer
            ):
                uncited_fallback_correct += 1

        if sc.category == "invalid_marker":
            if res.answer_mode == "insufficient_evidence" and res.insufficient_evidence is True:
                pass  # Handled in expected checks

        if sc.category == "provider_failure":
            failure_fallback_total += 1
            if (
                res.answer_mode == "static_guidance"
                and res.insufficient_evidence is False
                and sc.simulated_error_message not in res.answer
            ):
                failure_fallback_correct += 1

        if sc.category == "no_evidence":
            no_evidence_total += 1
            if not provider_was_called and res.insufficient_evidence is True:
                no_evidence_correct += 1

        # Check scenario contract assertions
        provider_match = provider_was_called == sc.expected_provider_called
        mode_match = res.answer_mode == sc.expected_answer_mode
        insuff_match = res.insufficient_evidence == sc.expected_insufficient_evidence
        cit_count_match = len(res.citations) == sc.expected_citations_count

        scenario_passed = (
            provider_match and mode_match and insuff_match and cit_count_match and citations_valid
        )

        if scenario_passed:
            passed_count += 1

        per_scenario.append(
            {
                "id": sc.id,
                "name": sc.name,
                "category": sc.category,
                "passed": scenario_passed,
                "provider_called": provider_was_called,
                "answer_mode": res.answer_mode,
                "insufficient_evidence": res.insufficient_evidence,
                "citation_count": len(res.citations),
                "citation_valid": citations_valid,
                "reason_code": sc.expected_reason_code if scenario_passed else "CONTRACT_MISMATCH",
            }
        )

    tot_sc = len(cases)
    pass_rate = round(passed_count / tot_sc, 4) if tot_sc > 0 else 1.0

    cit_marker_rate = (
        round(valid_returned_citations / total_returned_citations, 4)
        if total_returned_citations > 0
        else 1.0
    )

    cit_cov_rate = (
        round(grounded_valid_count / grounded_total_count, 4) if grounded_total_count > 0 else 1.0
    )

    uncited_rate = (
        round(uncited_fallback_correct / uncited_fallback_total, 4)
        if uncited_fallback_total > 0
        else 1.0
    )

    failure_rate = (
        round(failure_fallback_correct / failure_fallback_total, 4)
        if failure_fallback_total > 0
        else 1.0
    )

    no_ev_rate = round(no_evidence_correct / no_evidence_total, 4) if no_evidence_total > 0 else 1.0

    metrics = GroundedEvalMetricsReport(
        total_scenarios=tot_sc,
        grounded_answer_pass_rate=pass_rate,
        valid_citation_marker_rate=cit_marker_rate,
        citation_coverage_rate=cit_cov_rate,
        uncited_provider_safe_fallback_rate=uncited_rate,
        provider_failure_safe_fallback_rate=failure_rate,
        unsupported_question_safety_rate=no_ev_rate,
    )

    overall_passed = (
        metrics.grounded_answer_pass_rate == 1.0
        and metrics.valid_citation_marker_rate == 1.0
        and metrics.citation_coverage_rate == 1.0
        and metrics.uncited_provider_safe_fallback_rate == 1.0
        and metrics.provider_failure_safe_fallback_rate == 1.0
        and metrics.unsupported_question_safety_rate == 1.0
    )

    os.makedirs(results_dir, exist_ok=True)

    out_payload = {
        "corpus_version": corpus_ver,
        "scenario_set_version": scenario_ver,
        "passed": overall_passed,
        "metrics": asdict(metrics),
        "per_scenario": per_scenario,
    }

    with open(results_dir / "grounded_eval_result.v1.json", "w", encoding="utf-8") as f:
        json.dump(out_payload, f, indent=2)

    s_lat = sorted(latencies)
    telemetry_payload = {
        "corpus_version": corpus_ver,
        "scenario_set_version": scenario_ver,
        "mean_latency_ms": round(sum(s_lat) / len(s_lat), 3),
        "p50_latency_ms": round(s_lat[len(s_lat) // 2], 3),
        "p95_latency_ms": round(s_lat[int(len(s_lat) * 0.95)], 3),
        "max_latency_ms": round(s_lat[-1], 3),
        "per_scenario_latency_ms": {
            sc["id"]: sc_lat for sc, sc_lat in zip(per_scenario, latencies, strict=True)
        },
    }
    with open(results_dir / "grounded_eval_telemetry.v1.json", "w", encoding="utf-8") as f:
        json.dump(telemetry_payload, f, indent=2)

    return metrics, per_scenario, overall_passed


def main() -> int:
    metrics, per_sc, passed = run_grounded_evaluation()
    print("\n--- SOURCETRACE GROUNDED-ANSWER QUALITY EVALUATION REPORT ---")
    print(f"Status:                      {'PASSED' if passed else 'FAILED'}")
    print(f"Total Scenarios:             {metrics.total_scenarios}")
    print(f"Grounded Answer Pass Rate:   {metrics.grounded_answer_pass_rate:.2%}")
    print(f"Valid Citation Marker Rate:  {metrics.valid_citation_marker_rate:.2%}")
    print(f"Citation Coverage Rate:      {metrics.citation_coverage_rate:.2%}")
    print(f"Uncited Safe Fallback Rate:  {metrics.uncited_provider_safe_fallback_rate:.2%}")
    print(f"Provider Failure Fallback:   {metrics.provider_failure_safe_fallback_rate:.2%}")
    print(f"Unsupported Question Safety: {metrics.unsupported_question_safety_rate:.2%}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
