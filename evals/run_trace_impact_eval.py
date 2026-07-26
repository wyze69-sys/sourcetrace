"""Offline, reproducible evaluation runner for static flow trace and change
impact preview (EVAL-002).

Indexes the checked-in fixture repository with the real python-ast-v2 parser,
runs the production FlowTraceService and ChangeImpactService against a
static-only in-memory chunk repository (no embeddings, no LLM, no network),
and scores the results against the versioned expectations in
trace_impact.dataset.v1.json.

Scored properties:
- step/edge/gap correctness of traces (exact, deterministic expectations)
- upstream/downstream correctness of impact previews
- risk factor and risk level correctness
- citation validity: every produced evidence line must lie inside the chunk
  it cites and the evidence label must appear on that exact fixture line
- determinism: reversed storage return order must yield identical results
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure backend/src is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_SRC = ROOT_DIR / "backend" / "src"
if str(BACKEND_SRC) not in sys.path and BACKEND_SRC.exists():
    sys.path.insert(0, str(BACKEND_SRC))

from sourcetrace.models.domain import CodeChunk, RetrievalResult  # noqa: E402
from sourcetrace.parsers.python_ast import parse_python_source  # noqa: E402
from sourcetrace.retrieval.impact import ChangeImpactService  # noqa: E402
from sourcetrace.retrieval.trace import FlowTraceService  # noqa: E402

DEFAULT_DATASET_PATH = ROOT_DIR / "evals" / "trace_impact.dataset.v1.json"
DEFAULT_RESULTS_DIR = ROOT_DIR / "evals" / "results"
RESULT_FILE_NAME = "eval_result.trace_impact.v1.json"

_FIXED_CREATED_AT = datetime(2026, 7, 26, 0, 0, 0, tzinfo=UTC)
_OWNER = "eval_session_ti_001"
_REPOSITORY = "eval_repo_ti_001"

_VALID_CONFIDENCES = {"high", "medium", "low"}
_VALID_RISK_LEVELS = {"low", "medium", "high", "unknown"}


class StaticChunkRepository:
    """In-memory chunk repository exposing only the two methods the static
    trace and impact services are allowed to call."""

    def __init__(self, chunks: Sequence[CodeChunk]) -> None:
        self._chunks = list(chunks)

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[CodeChunk]:
        return [
            c
            for c in self._chunks
            if c.owner_session_id == owner_session_id
            and c.repository_id == repository_id
        ]

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
    ) -> list[RetrievalResult]:
        needle = query_text.casefold()
        matches = [
            c
            for c in self.list_by_repository(owner_session_id, repository_id)
            if needle in c.symbol_name.casefold()
        ]
        matches.sort(
            key=lambda c: (c.relative_path, c.start_line, c.symbol_name, c.chunk_id)
        )
        return [RetrievalResult(chunk=c, score=1.0) for c in matches[:limit]]


def load_fixture_chunks(fixture_dir: Path, *, reverse: bool = False) -> list[CodeChunk]:
    """Parse fixture files with the real parser into static-mode chunks."""
    chunks: list[CodeChunk] = []
    files = sorted(fixture_dir.glob("*.py"), reverse=reverse)
    for py_file in files:
        content = py_file.read_text(encoding="utf-8")
        for parsed in parse_python_source(
            source=content,
            relative_path=py_file.name,
            repository_id=_REPOSITORY,
            owner_session_id=_OWNER,
        ):
            chunks.append(
                CodeChunk(
                    chunk_id=parsed.chunk_id,
                    repository_id=parsed.repository_id,
                    owner_session_id=parsed.owner_session_id,
                    relative_path=parsed.relative_path,
                    language=parsed.language,
                    symbol_name=parsed.symbol_name,
                    symbol_type=parsed.symbol_type,
                    start_line=parsed.start_line,
                    end_line=parsed.end_line,
                    content=parsed.content,
                    content_hash=parsed.content_hash,
                    parser_version=parsed.parser_version,
                    created_at=_FIXED_CREATED_AT,
                    references=parsed.references,
                    imports=parsed.imports,
                    endpoints=parsed.endpoints,
                    extraction_truncated=parsed.extraction_truncated,
                )
            )
    return chunks


@dataclass
class TraceCase:
    id: str
    entry: str
    expected_steps: list[tuple[str, str]]
    expected_edges: list[tuple[str, str, str, str, int]]
    expected_gap_kinds: set[str]


@dataclass
class ImpactCase:
    id: str
    symbol: str
    expected_upstream: list[tuple[str, str, int, str]]
    expected_downstream: list[tuple[str, str, int, str]]
    expected_risk_factor_kinds: set[str]
    expected_risk_level: str
    expected_gap_kinds: set[str]


@dataclass
class DiffCase:
    id: str
    diff: str
    expected_targets: list[tuple[str, str, tuple[int, ...]]]
    expected_upstream: list[tuple[str, str, int, str]]
    expected_downstream: list[tuple[str, str, int, str]]
    expected_risk_factor_kinds: set[str]
    expected_risk_level: str
    expected_gap_kinds: set[str]


@dataclass
class TraceImpactMetricReport:
    total_trace_cases: int
    total_impact_cases: int
    total_diff_cases: int
    diff_target_accuracy: float
    diff_impact_accuracy: float
    diff_risk_accuracy: float
    diff_gap_accuracy: float
    trace_step_accuracy: float
    trace_edge_accuracy: float
    trace_gap_accuracy: float
    impact_upstream_accuracy: float
    impact_downstream_accuracy: float
    risk_factor_accuracy: float
    risk_level_accuracy: float
    citation_validity_rate: float
    citations_checked: int
    invalid_citation_count: int
    determinism_verified: bool
    mean_latency_ms: float
    max_latency_ms: float
    cost_status: str
    cost_reason: str
    evaluation_label: str
    failures: list[str] = field(default_factory=list)


def validate_dataset(
    dataset_path: Path, root_dir: Path
) -> tuple[list[TraceCase], list[ImpactCase], list[DiffCase], Path | None, list[str]]:
    errors: list[str] = []

    if not dataset_path.exists():
        return [], [], [], None, [f"Dataset file does not exist: {dataset_path}"]
    try:
        with open(dataset_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001 - report any parse failure verbatim
        return [], [], [], None, [f"Dataset file is invalid JSON: {e}"]

    if data.get("schema_version") != 1:
        errors.append(
            f"Invalid schema_version: expected 1, got {data.get('schema_version')}"
        )

    fixture_rel = data.get("repository_fixture", "")
    if (
        not isinstance(fixture_rel, str)
        or not fixture_rel.strip()
        or Path(fixture_rel).is_absolute()
        or ".." in fixture_rel.replace("\\", "/").split("/")
    ):
        errors.append("'repository_fixture' must be a safe relative path")
        return [], [], [], None, errors
    fixture_dir = root_dir / fixture_rel
    if not fixture_dir.is_dir():
        errors.append(f"Fixture directory does not exist: {fixture_rel}")
        return [], [], [], None, errors

    # Ground expectations against the real parse: every expected (path, symbol)
    # must exist as an indexed chunk, so the dataset cannot drift from fixtures.
    known_symbols = {
        (c.relative_path, c.symbol_name) for c in load_fixture_chunks(fixture_dir)
    }

    def check_symbol(case_id: str, path: str, symbol: str) -> bool:
        if (path, symbol) not in known_symbols:
            errors.append(
                f"Case {case_id}: expected symbol ({path}, {symbol}) is not an "
                "indexed chunk of the fixture repository"
            )
            return False
        return True

    seen_ids: set[str] = set()

    def check_id(case_id: Any) -> str | None:
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append("Every case needs a non-empty string 'id'")
            return None
        if case_id in seen_ids:
            errors.append(f"Duplicate case id: {case_id}")
        seen_ids.add(case_id)
        return case_id

    trace_cases: list[TraceCase] = []
    for raw in data.get("trace_cases", []):
        case_id = check_id(raw.get("id"))
        if case_id is None:
            continue
        entry = raw.get("entry", "")
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"Case {case_id}: 'entry' must be a non-empty string")
            continue
        steps: list[tuple[str, str]] = []
        for step in raw.get("expected_steps", []):
            path, symbol = step.get("relative_path", ""), step.get("symbol_name", "")
            if check_symbol(case_id, path, symbol):
                steps.append((path, symbol))
        edges: list[tuple[str, str, str, str, int]] = []
        for edge in raw.get("expected_edges", []):
            confidence = edge.get("confidence", "")
            if confidence not in _VALID_CONFIDENCES:
                errors.append(f"Case {case_id}: invalid edge confidence {confidence!r}")
                continue
            edges.append(
                (
                    edge.get("from_symbol", ""),
                    edge.get("to_symbol", ""),
                    edge.get("kind", ""),
                    confidence,
                    int(edge.get("evidence_line", 0)),
                )
            )
        trace_cases.append(
            TraceCase(
                id=case_id,
                entry=entry,
                expected_steps=steps,
                expected_edges=edges,
                expected_gap_kinds=set(raw.get("expected_gap_kinds", [])),
            )
        )

    impact_cases: list[ImpactCase] = []
    for raw in data.get("impact_cases", []):
        case_id = check_id(raw.get("id"))
        if case_id is None:
            continue
        symbol = raw.get("symbol", "")
        if not isinstance(symbol, str) or not symbol.strip():
            errors.append(f"Case {case_id}: 'symbol' must be a non-empty string")
            continue

        def parse_items(
            key: str, raw: dict = raw, case_id: str = case_id
        ) -> list[tuple[str, str, int, str]]:
            items: list[tuple[str, str, int, str]] = []
            for item in raw.get(key, []):
                path = item.get("relative_path", "")
                sym = item.get("symbol_name", "")
                confidence = item.get("confidence", "")
                if confidence not in _VALID_CONFIDENCES:
                    errors.append(
                        f"Case {case_id}: invalid item confidence {confidence!r}"
                    )
                    continue
                if check_symbol(case_id, path, sym):
                    items.append((path, sym, int(item.get("distance", 0)), confidence))
            return items

        risk_level = raw.get("expected_risk_level", "")
        if risk_level not in _VALID_RISK_LEVELS:
            errors.append(f"Case {case_id}: invalid expected_risk_level {risk_level!r}")
            continue
        impact_cases.append(
            ImpactCase(
                id=case_id,
                symbol=symbol,
                expected_upstream=parse_items("expected_upstream"),
                expected_downstream=parse_items("expected_downstream"),
                expected_risk_factor_kinds=set(
                    raw.get("expected_risk_factor_kinds", [])
                ),
                expected_risk_level=risk_level,
                expected_gap_kinds=set(raw.get("expected_gap_kinds", [])),
            )
        )

    diff_cases: list[DiffCase] = []
    for raw in data.get("diff_cases", []):
        case_id = check_id(raw.get("id"))
        if case_id is None:
            continue
        diff_text = raw.get("diff", "")
        if not isinstance(diff_text, str) or not diff_text.strip():
            errors.append(f"Case {case_id}: 'diff' must be a non-empty string")
            continue

        targets: list[tuple[str, str, tuple[int, ...]]] = []
        for target in raw.get("expected_targets", []):
            path = target.get("relative_path", "")
            symbol = target.get("symbol_name", "")
            if check_symbol(case_id, path, symbol):
                targets.append(
                    (path, symbol, tuple(int(n) for n in target.get("changed_lines", [])))
                )

        def parse_diff_items(
            key: str, raw: dict = raw, case_id: str = case_id
        ) -> list[tuple[str, str, int, str]]:
            items: list[tuple[str, str, int, str]] = []
            for item in raw.get(key, []):
                path = item.get("relative_path", "")
                sym = item.get("symbol_name", "")
                confidence = item.get("confidence", "")
                if confidence not in _VALID_CONFIDENCES:
                    errors.append(
                        f"Case {case_id}: invalid item confidence {confidence!r}"
                    )
                    continue
                if check_symbol(case_id, path, sym):
                    items.append((path, sym, int(item.get("distance", 0)), confidence))
            return items

        risk_level = raw.get("expected_risk_level", "")
        if risk_level not in _VALID_RISK_LEVELS:
            errors.append(f"Case {case_id}: invalid expected_risk_level {risk_level!r}")
            continue
        diff_cases.append(
            DiffCase(
                id=case_id,
                diff=diff_text,
                expected_targets=targets,
                expected_upstream=parse_diff_items("expected_upstream"),
                expected_downstream=parse_diff_items("expected_downstream"),
                expected_risk_factor_kinds=set(
                    raw.get("expected_risk_factor_kinds", [])
                ),
                expected_risk_level=risk_level,
                expected_gap_kinds=set(raw.get("expected_gap_kinds", [])),
            )
        )

    if len(trace_cases) < 3:
        errors.append(f"Dataset needs at least 3 trace cases, got {len(trace_cases)}")
    if len(impact_cases) < 3:
        errors.append(f"Dataset needs at least 3 impact cases, got {len(impact_cases)}")
    if len(diff_cases) < 3:
        errors.append(f"Dataset needs at least 3 diff cases, got {len(diff_cases)}")
    covered_diff_gaps: set[str] = set()
    for case in diff_cases:
        covered_diff_gaps |= case.expected_gap_kinds
    required_diff_gaps = {"diff_file_unmatched", "diff_lines_uncovered", "diff_stale"}
    missing = required_diff_gaps - covered_diff_gaps
    if missing:
        errors.append(
            f"Diff cases must collectively cover gap kinds {sorted(required_diff_gaps)}; "
            f"missing: {sorted(missing)}"
        )
    if diff_cases and not any(
        c.expected_targets and not c.expected_gap_kinds for c in diff_cases
    ):
        errors.append(
            "Diff cases must include at least one clean changed-symbol mapping "
            "(non-empty expected_targets with no expected gaps)"
        )
    trace_ids = [c.id for c in trace_cases]
    impact_ids = [c.id for c in impact_cases]
    diff_ids = [c.id for c in diff_cases]
    if (
        trace_ids != sorted(trace_ids)
        or impact_ids != sorted(impact_ids)
        or diff_ids != sorted(diff_ids)
    ):
        errors.append("Cases are not in deterministic sorted id order")

    return trace_cases, impact_cases, diff_cases, fixture_dir, errors


def _check_citation(
    fixture_dir: Path,
    by_id: dict[str, CodeChunk],
    evidence_node_id: str,
    label: str,
    line_start: int,
    line_end: int,
    failures: list[str],
    context: str,
) -> bool:
    """A citation is valid when its lines lie inside the cited chunk and the
    evidence label is actually present on the cited fixture line."""
    chunk = by_id.get(evidence_node_id)
    if chunk is None:
        failures.append(f"{context}: cited node {evidence_node_id} is not indexed")
        return False
    if not (chunk.start_line <= line_start <= line_end <= chunk.end_line):
        failures.append(
            f"{context}: evidence lines {line_start}-{line_end} outside chunk "
            f"span {chunk.start_line}-{chunk.end_line}"
        )
        return False
    file_lines = (fixture_dir / chunk.relative_path).read_text(
        encoding="utf-8"
    ).splitlines()
    if line_start > len(file_lines):
        failures.append(f"{context}: evidence line {line_start} beyond file end")
        return False
    cited_text = file_lines[line_start - 1]
    token = label.split(" ", 1)[-1] if " " in label else label
    token = token.rsplit(".", 1)[-1]
    if token not in cited_text:
        failures.append(
            f"{context}: label {label!r} not found on cited line {line_start}: "
            f"{cited_text!r}"
        )
        return False
    return True


def run_evaluation(
    dataset_path: Path = DEFAULT_DATASET_PATH,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    root_dir: Path = ROOT_DIR,
) -> tuple[TraceImpactMetricReport, bool]:
    trace_cases, impact_cases, diff_cases, fixture_dir, validation_errors = (
        validate_dataset(dataset_path, root_dir)
    )
    if validation_errors:
        report = TraceImpactMetricReport(
            total_trace_cases=0,
            total_impact_cases=0,
            total_diff_cases=0,
            diff_target_accuracy=0.0,
            diff_impact_accuracy=0.0,
            diff_risk_accuracy=0.0,
            diff_gap_accuracy=0.0,
            trace_step_accuracy=0.0,
            trace_edge_accuracy=0.0,
            trace_gap_accuracy=0.0,
            impact_upstream_accuracy=0.0,
            impact_downstream_accuracy=0.0,
            risk_factor_accuracy=0.0,
            risk_level_accuracy=0.0,
            citation_validity_rate=0.0,
            citations_checked=0,
            invalid_citation_count=0,
            determinism_verified=False,
            mean_latency_ms=0.0,
            max_latency_ms=0.0,
            cost_status="not measured",
            cost_reason="zero-token static services have no billable usage",
            evaluation_label="offline deterministic trace/impact evaluation",
            failures=[f"VALIDATION: {e}" for e in validation_errors],
        )
        return report, False

    assert fixture_dir is not None
    forward_chunks = load_fixture_chunks(fixture_dir)
    reversed_chunks = load_fixture_chunks(fixture_dir, reverse=True)
    by_id = {c.chunk_id: c for c in forward_chunks}

    forward_repo = StaticChunkRepository(forward_chunks)
    reversed_repo = StaticChunkRepository(reversed_chunks)

    failures: list[str] = []
    latencies: list[float] = []
    determinism_ok = True

    citations_checked = 0
    invalid_citations = 0

    step_hits = edge_hits = gap_hits = 0
    for case in trace_cases:
        service = FlowTraceService(forward_repo)
        started = time.monotonic()
        result = service.trace(_OWNER, _REPOSITORY, case.entry)
        latencies.append((time.monotonic() - started) * 1000.0)

        if FlowTraceService(reversed_repo).trace(_OWNER, _REPOSITORY, case.entry) != result:
            determinism_ok = False
            failures.append(f"{case.id}: result depends on storage return order")

        actual_steps = [
            (by_id[node_id].relative_path, by_id[node_id].symbol_name)
            for node_id in result.steps
        ]
        if actual_steps == case.expected_steps:
            step_hits += 1
        else:
            failures.append(
                f"{case.id}: steps {actual_steps} != expected {case.expected_steps}"
            )

        actual_edges = {
            (
                by_id[e.from_node_id].symbol_name,
                by_id[e.to_node_id].symbol_name,
                e.kind,
                e.confidence,
                e.evidence_line_start,
            )
            for e in result.edges
        }
        if actual_edges == set(case.expected_edges):
            edge_hits += 1
        else:
            failures.append(
                f"{case.id}: edges {sorted(actual_edges)} != expected "
                f"{sorted(set(case.expected_edges))}"
            )

        actual_gaps = {g.kind for g in result.gaps}
        if actual_gaps == case.expected_gap_kinds:
            gap_hits += 1
        else:
            failures.append(
                f"{case.id}: gap kinds {sorted(actual_gaps)} != expected "
                f"{sorted(case.expected_gap_kinds)}"
            )

        for e in result.edges:
            citations_checked += 1
            if not _check_citation(
                fixture_dir,
                by_id,
                e.from_node_id,
                e.evidence_label,
                e.evidence_line_start,
                e.evidence_line_end,
                failures,
                f"{case.id} edge {e.evidence_label}",
            ):
                invalid_citations += 1

    diff_target_hits = diff_impact_hits = diff_risk_hits = diff_gap_hits = 0
    for case in diff_cases:
        service = ChangeImpactService(forward_repo)
        started = time.monotonic()
        result = service.preview_diff(_OWNER, _REPOSITORY, case.diff)
        latencies.append((time.monotonic() - started) * 1000.0)

        if (
            ChangeImpactService(reversed_repo).preview_diff(_OWNER, _REPOSITORY, case.diff)
            != result
        ):
            determinism_ok = False
            failures.append(f"{case.id}: result depends on storage return order")

        actual_targets = [
            (t.relative_path, t.symbol_name, t.changed_lines) for t in result.targets
        ]
        if actual_targets == case.expected_targets:
            diff_target_hits += 1
        else:
            failures.append(
                f"{case.id}: targets {actual_targets} != expected {case.expected_targets}"
            )

        def diff_to_tuples(items) -> list[tuple[str, str, int, str]]:
            return [
                (i.relative_path, i.symbol_name, i.distance, i.confidence)
                for i in items
            ]

        if (
            diff_to_tuples(result.upstream) == case.expected_upstream
            and diff_to_tuples(result.downstream) == case.expected_downstream
        ):
            diff_impact_hits += 1
        else:
            failures.append(
                f"{case.id}: impact upstream {diff_to_tuples(result.upstream)} / "
                f"downstream {diff_to_tuples(result.downstream)} != expected "
                f"{case.expected_upstream} / {case.expected_downstream}"
            )

        actual_factor_kinds = {f.kind for f in result.risk_factors}
        if (
            actual_factor_kinds == case.expected_risk_factor_kinds
            and result.risk_level == case.expected_risk_level
        ):
            diff_risk_hits += 1
        else:
            failures.append(
                f"{case.id}: risk {result.risk_level}/{sorted(actual_factor_kinds)} != "
                f"expected {case.expected_risk_level}/"
                f"{sorted(case.expected_risk_factor_kinds)}"
            )

        actual_gaps = {g.kind for g in result.gaps}
        if actual_gaps == case.expected_gap_kinds:
            diff_gap_hits += 1
        else:
            failures.append(
                f"{case.id}: gap kinds {sorted(actual_gaps)} != expected "
                f"{sorted(case.expected_gap_kinds)}"
            )

        for item in list(result.upstream) + list(result.downstream):
            citations_checked += 1
            if not _check_citation(
                fixture_dir,
                by_id,
                item.evidence_node_id,
                item.evidence_label,
                item.evidence_line_start,
                item.evidence_line_end,
                failures,
                f"{case.id} item {item.symbol_name}",
            ):
                invalid_citations += 1

    upstream_hits = downstream_hits = factor_hits = level_hits = impact_gap_hits = 0
    for case in impact_cases:
        service = ChangeImpactService(forward_repo)
        started = time.monotonic()
        result = service.preview(_OWNER, _REPOSITORY, case.symbol)
        latencies.append((time.monotonic() - started) * 1000.0)

        if (
            ChangeImpactService(reversed_repo).preview(_OWNER, _REPOSITORY, case.symbol)
            != result
        ):
            determinism_ok = False
            failures.append(f"{case.id}: result depends on storage return order")

        def to_tuples(items) -> list[tuple[str, str, int, str]]:
            return [
                (i.relative_path, i.symbol_name, i.distance, i.confidence)
                for i in items
            ]

        if to_tuples(result.upstream) == case.expected_upstream:
            upstream_hits += 1
        else:
            failures.append(
                f"{case.id}: upstream {to_tuples(result.upstream)} != expected "
                f"{case.expected_upstream}"
            )
        if to_tuples(result.downstream) == case.expected_downstream:
            downstream_hits += 1
        else:
            failures.append(
                f"{case.id}: downstream {to_tuples(result.downstream)} != expected "
                f"{case.expected_downstream}"
            )

        actual_factor_kinds = {f.kind for f in result.risk_factors}
        if actual_factor_kinds == case.expected_risk_factor_kinds:
            factor_hits += 1
        else:
            failures.append(
                f"{case.id}: risk factors {sorted(actual_factor_kinds)} != expected "
                f"{sorted(case.expected_risk_factor_kinds)}"
            )
        if result.risk_level == case.expected_risk_level:
            level_hits += 1
        else:
            failures.append(
                f"{case.id}: risk level {result.risk_level} != expected "
                f"{case.expected_risk_level}"
            )
        actual_gaps = {g.kind for g in result.gaps}
        if actual_gaps == case.expected_gap_kinds:
            impact_gap_hits += 1
        else:
            failures.append(
                f"{case.id}: gap kinds {sorted(actual_gaps)} != expected "
                f"{sorted(case.expected_gap_kinds)}"
            )

        for item in list(result.upstream) + list(result.downstream):
            citations_checked += 1
            if not _check_citation(
                fixture_dir,
                by_id,
                item.evidence_node_id,
                item.evidence_label,
                item.evidence_line_start,
                item.evidence_line_end,
                failures,
                f"{case.id} item {item.symbol_name}",
            ):
                invalid_citations += 1

    n_trace = len(trace_cases)
    n_impact = len(impact_cases)
    n_diff = len(diff_cases)
    citation_validity = (
        (citations_checked - invalid_citations) / citations_checked
        if citations_checked
        else 0.0
    )
    trace_gap_accuracy = gap_hits / n_trace
    impact_gap_accuracy = impact_gap_hits / n_impact

    report = TraceImpactMetricReport(
        total_trace_cases=n_trace,
        total_impact_cases=n_impact,
        total_diff_cases=n_diff,
        diff_target_accuracy=round(diff_target_hits / n_diff, 4) if n_diff else 0.0,
        diff_impact_accuracy=round(diff_impact_hits / n_diff, 4) if n_diff else 0.0,
        diff_risk_accuracy=round(diff_risk_hits / n_diff, 4) if n_diff else 0.0,
        diff_gap_accuracy=round(diff_gap_hits / n_diff, 4) if n_diff else 0.0,
        trace_step_accuracy=round(step_hits / n_trace, 4),
        trace_edge_accuracy=round(edge_hits / n_trace, 4),
        trace_gap_accuracy=round((trace_gap_accuracy + impact_gap_accuracy) / 2, 4),
        impact_upstream_accuracy=round(upstream_hits / n_impact, 4),
        impact_downstream_accuracy=round(downstream_hits / n_impact, 4),
        risk_factor_accuracy=round(factor_hits / n_impact, 4),
        risk_level_accuracy=round(level_hits / n_impact, 4),
        citation_validity_rate=round(citation_validity, 4),
        citations_checked=citations_checked,
        invalid_citation_count=invalid_citations,
        determinism_verified=determinism_ok,
        mean_latency_ms=round(sum(latencies) / len(latencies), 3),
        max_latency_ms=round(max(latencies), 3),
        cost_status="not measured",
        cost_reason="zero-token static services have no billable usage",
        evaluation_label="offline deterministic trace/impact evaluation",
        failures=failures,
    )

    success = (
        report.trace_step_accuracy == 1.0
        and report.trace_edge_accuracy == 1.0
        and report.trace_gap_accuracy == 1.0
        and report.impact_upstream_accuracy == 1.0
        and report.impact_downstream_accuracy == 1.0
        and report.risk_factor_accuracy == 1.0
        and report.risk_level_accuracy == 1.0
        and report.diff_target_accuracy == 1.0
        and report.diff_impact_accuracy == 1.0
        and report.diff_risk_accuracy == 1.0
        and report.diff_gap_accuracy == 1.0
        and report.citation_validity_rate == 1.0
        and report.citations_checked > 0
        and report.determinism_verified
    )

    os.makedirs(results_dir, exist_ok=True)
    payload = {
        "dataset_path": str(dataset_path),
        "dataset_version": "v1",
        "passed": success,
        "metrics": asdict(report),
    }
    with open(results_dir / RESULT_FILE_NAME, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return report, success


def print_report(report: TraceImpactMetricReport, success: bool) -> None:
    print("\n=======================================================")
    print("  SOURCETRACE TRACE & IMPACT EVALUATION REPORT (v1)    ")
    print("=======================================================\n")
    print(f"Evaluation Mode:  {report.evaluation_label}")
    print(f"Trace Cases:      {report.total_trace_cases}")
    print(f"Impact Cases:     {report.total_impact_cases}")
    print(f"Diff Cases:       {report.total_diff_cases}")
    print(f"Overall Status:   {'PASSED' if success else 'FAILED'}\n")
    print("--- Trace Metrics ---")
    print(f"  Step Accuracy:             {report.trace_step_accuracy:.2%}")
    print(f"  Edge Accuracy:             {report.trace_edge_accuracy:.2%}")
    print(f"  Gap Accuracy:              {report.trace_gap_accuracy:.2%}")
    print("\n--- Impact Metrics ---")
    print(f"  Upstream Accuracy:         {report.impact_upstream_accuracy:.2%}")
    print(f"  Downstream Accuracy:       {report.impact_downstream_accuracy:.2%}")
    print(f"  Risk Factor Accuracy:      {report.risk_factor_accuracy:.2%}")
    print(f"  Risk Level Accuracy:       {report.risk_level_accuracy:.2%}")
    print("\n--- Diff Impact Metrics ---")
    print(f"  Target Mapping Accuracy:   {report.diff_target_accuracy:.2%}")
    print(f"  Impact Set Accuracy:       {report.diff_impact_accuracy:.2%}")
    print(f"  Risk Accuracy:             {report.diff_risk_accuracy:.2%}")
    print(f"  Gap Accuracy:              {report.diff_gap_accuracy:.2%}")
    print("\n--- Citation & Determinism ---")
    print(f"  Citation Validity:         {report.citation_validity_rate:.2%}")
    print(f"  Citations Checked:         {report.citations_checked}")
    print(f"  Invalid Citations:         {report.invalid_citation_count}")
    print(f"  Determinism Verified:      {report.determinism_verified}")
    print("\n--- Latency ---")
    print(f"  Mean Latency:              {report.mean_latency_ms:.3f} ms")
    print(f"  Max Latency:               {report.max_latency_ms:.3f} ms")
    print("\n--- Provider Cost ---")
    print(f"  Status: {report.cost_status} ({report.cost_reason})")
    if report.failures:
        print("\n--- Failures ---")
        for failure in report.failures:
            print(f"  FAIL: {failure}")
    print("=======================================================\n")


def main() -> int:
    report, success = run_evaluation()
    print_report(report, success)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
