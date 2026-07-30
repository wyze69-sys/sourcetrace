"""Offline, reproducible retrieval and citation evaluation runner (EVAL-001 Part 1/3)."""

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
if str(SRC_DIR) not in sys.path and SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from sourcetrace.models.domain import (
    CitationRecord,
    CodeChunk,
    GroundedEvidenceResult,
    RepositoryRecord,
    RetrievalResult,
)
from sourcetrace.parsers.python_ast import parse_python_source
from sourcetrace.retrieval.service import SemanticRetrievalService
from sourcetrace.storage.repositories import (
    CodeChunkRepository,
    RepositoryRepository,
)

DEFAULT_QUESTION_SET_PATH = BACKEND_DIR / "tests" / "evaluation" / "question_set.v1.json"
DEFAULT_RESULTS_DIR = BACKEND_DIR / "tests" / "evaluation" / "results"
DEFAULT_OUTPUT_FILE = DEFAULT_RESULTS_DIR / "retrieval_eval_result.v1.json"
DEFAULT_TELEMETRY_FILE = DEFAULT_RESULTS_DIR / "retrieval_eval_telemetry.v1.json"

ALL_GENERATIONS_SENTINELS: frozenset[Any] = frozenset(
    {"*", "__ALL_GENERATIONS__", "ALL_GENERATIONS"}
)


def _matches_generation(chunk_generation_id: str | None, requested_generation_id: Any) -> bool:
    """Match chunk generation_id against requested generation_id according to production rules."""
    if requested_generation_id in ALL_GENERATIONS_SENTINELS:
        return True
    return chunk_generation_id == requested_generation_id


class FakeEvalRepositoryRepository(RepositoryRepository):
    """Offline fake repository repository supporting multiple records for benchmark evaluation."""

    def __init__(
        self,
        owner_session_id: str,
        repository_id: str,
        active_generation_id: str | None = "eval_gen_001",
    ) -> None:
        now = datetime.now(UTC)
        self.records: dict[tuple[str, str], RepositoryRecord] = {}
        rec = RepositoryRecord(
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            name="eval_corpus_v1",
            source_type="github",
            status="ready",
            file_count=7,
            chunk_count=14,
            active_generation_id=active_generation_id,
            created_at=now,
            updated_at=now,
        )
        self.records[(owner_session_id, repository_id)] = rec

    def add_record(self, record: RepositoryRecord) -> None:
        self.records[(record.owner_session_id, record.repository_id)] = record

    def create(self, record: RepositoryRecord) -> RepositoryRecord:
        self.records[(record.owner_session_id, record.repository_id)] = record
        return record

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        return self.records.get((owner_session_id, repository_id))

    def update_active_generation(
        self,
        owner_session_id: str,
        repository_id: str,
        active_generation_id: str,
        updated_at: datetime,
    ) -> RepositoryRecord | None:
        rec = self.records.get((owner_session_id, repository_id))
        if rec is not None:
            updated_rec = RepositoryRecord(
                repository_id=rec.repository_id,
                owner_session_id=rec.owner_session_id,
                name=rec.name,
                source_type=rec.source_type,
                status=rec.status,
                file_count=rec.file_count,
                chunk_count=rec.chunk_count,
                active_generation_id=active_generation_id,
                created_at=rec.created_at,
                updated_at=updated_at,
            )
            self.records[(owner_session_id, repository_id)] = updated_rec
            return updated_rec
        return None

    def list_by_owner(self, owner_session_id: str) -> Sequence[RepositoryRecord]:
        return [r for (owner, _), r in self.records.items() if owner == owner_session_id]

    def count_by_owner(self, owner_session_id: str) -> int:
        return len(self.list_by_owner(owner_session_id))

    def update(self, record: RepositoryRecord) -> RepositoryRecord:
        self.records[(record.owner_session_id, record.repository_id)] = record
        return record

    def delete(self, owner_session_id: str, repository_id: str) -> bool:
        return self.records.pop((owner_session_id, repository_id), None) is not None


class FakeEvalCodeChunkRepository(CodeChunkRepository):
    """Offline fake code chunk repo matching production generation filtering semantics."""

    def __init__(self) -> None:
        self._chunks: list[CodeChunk] = []

    def add_chunks(self, chunks: Sequence[CodeChunk]) -> int:
        self._chunks.extend(chunks)
        return len(chunks)

    def get_by_id(
        self, owner_session_id: str, repository_id: str, chunk_id: str
    ) -> CodeChunk | None:
        for c in self._chunks:
            if (
                c.owner_session_id == owner_session_id
                and c.repository_id == repository_id
                and c.chunk_id == chunk_id
            ):
                return c
        return None

    def list_by_repository(
        self,
        owner_session_id: str,
        repository_id: str,
        generation_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[CodeChunk]:
        matched = []
        for c in self._chunks:
            if c.owner_session_id != owner_session_id or c.repository_id != repository_id:
                continue
            if not _matches_generation(c.generation_id, generation_id):
                continue
            matched.append(c)
        return matched[:limit]

    def migrate_legacy_generation(
        self, owner_session_id: str, repository_id: str, target_generation_id: str
    ) -> int:
        count = 0
        new_chunks: list[CodeChunk] = []
        for c in self._chunks:
            if (
                c.owner_session_id == owner_session_id
                and c.repository_id == repository_id
                and c.generation_id is None
            ):
                new_chunks.append(
                    CodeChunk(
                        chunk_id=c.chunk_id,
                        repository_id=c.repository_id,
                        owner_session_id=c.owner_session_id,
                        relative_path=c.relative_path,
                        language=c.language,
                        symbol_name=c.symbol_name,
                        symbol_type=c.symbol_type,
                        start_line=c.start_line,
                        end_line=c.end_line,
                        content=c.content,
                        content_hash=c.content_hash,
                        parser_version=c.parser_version,
                        created_at=c.created_at,
                        generation_id=target_generation_id,
                    )
                )
                count += 1
            else:
                new_chunks.append(c)
        self._chunks = new_chunks
        return count

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        count = len(
            [
                c
                for c in self._chunks
                if c.owner_session_id == owner_session_id and c.repository_id == repository_id
            ]
        )
        self._chunks = [
            c
            for c in self._chunks
            if not (c.owner_session_id == owner_session_id and c.repository_id == repository_id)
        ]
        return count

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
        generation_id: str | None = None,
    ) -> Sequence[RetrievalResult]:
        matched = [
            c
            for c in self._chunks
            if c.owner_session_id == owner_session_id
            and c.repository_id == repository_id
            and _matches_generation(c.generation_id, generation_id)
        ]
        if not matched:
            return ()

        from sourcetrace.storage.mongo_repositories import _ENGLISH_STOP_WORDS, tokenize_identifier

        raw_tokens = tokenize_identifier(query_text)
        query_tokens = set(
            t.lower() for t in raw_tokens if t.lower() not in _ENGLISH_STOP_WORDS and len(t) > 1
        )
        generic_fillers = {
            "repository",
            "codebase",
            "project",
            "system",
            "application",
            "app",
            "file",
            "files",
        }
        specific_tokens = query_tokens - generic_fillers
        if specific_tokens:
            query_tokens = specific_tokens

        if not query_tokens:
            return ()

        scored: list[RetrievalResult] = []

        for chunk in matched:
            text = f"{chunk.relative_path} {chunk.symbol_name} {chunk.content}".lower()
            score = 0.0
            for token in query_tokens:
                if token in text:
                    score += 1.0
            if score > 0:
                scored.append(RetrievalResult(chunk=chunk, score=score))

        scored.sort(
            key=lambda r: (
                -r.score,
                r.chunk.relative_path,
                r.chunk.start_line,
                r.chunk.chunk_id,
            )
        )
        return scored[:limit]

    def search_vectors(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: Sequence[float],
        limit: int = 5,
        generation_id: str | None = None,
    ) -> Sequence[RetrievalResult]:
        matched = [
            c
            for c in self._chunks
            if c.owner_session_id == owner_session_id
            and c.repository_id == repository_id
            and _matches_generation(c.generation_id, generation_id)
        ]
        if not matched:
            return ()
        return ()


@dataclass(frozen=True)
class CitationValidationResult:
    is_valid: bool
    path_exists: bool
    line_range_valid: bool
    scope_owner_valid: bool
    scope_repo_valid: bool
    scope_gen_valid: bool
    failure_reason: str | None = None


def validate_retrieved_citation(
    citation: CitationRecord,
    chunk_id: str,
    file_line_counts: dict[str, int],
    expected_owner_session_id: str,
    expected_repository_id: str,
    expected_generation_id: str | None,
    code_chunk_repo: CodeChunkRepository,
) -> CitationValidationResult:
    """Validate citation file existence, line bounds, and repository/owner/generation scope."""
    path_exists = citation.relative_path in file_line_counts
    max_lines = file_line_counts.get(citation.relative_path, 0)
    line_range_valid = (
        path_exists
        and type(citation.start_line) is int
        and type(citation.end_line) is int
        and 1 <= citation.start_line <= citation.end_line <= max_lines
    )

    raw_chunk: CodeChunk | None = None
    if hasattr(code_chunk_repo, "_chunks"):
        for c in code_chunk_repo._chunks:
            if c.chunk_id == chunk_id:
                raw_chunk = c
                break
    else:
        raw_chunk = code_chunk_repo.get_by_id(
            expected_owner_session_id, expected_repository_id, chunk_id
        )

    if raw_chunk is None:
        return CitationValidationResult(
            is_valid=False,
            path_exists=path_exists,
            line_range_valid=line_range_valid,
            scope_owner_valid=False,
            scope_repo_valid=False,
            scope_gen_valid=False,
            failure_reason=f"Chunk '{chunk_id}' not found in repository",
        )

    scope_owner_valid = raw_chunk.owner_session_id == expected_owner_session_id
    scope_repo_valid = raw_chunk.repository_id == expected_repository_id
    scope_gen_valid = (
        expected_generation_id is None or raw_chunk.generation_id == expected_generation_id
    )

    is_valid = (
        path_exists
        and line_range_valid
        and scope_owner_valid
        and scope_repo_valid
        and scope_gen_valid
    )
    failure_reason = None
    if not is_valid:
        reasons = []
        if not path_exists:
            reasons.append(f"nonexistent path '{citation.relative_path}'")
        if not line_range_valid:
            reasons.append(
                f"invalid line range {citation.start_line}-{citation.end_line} "
                f"(max lines: {max_lines})"
            )
        if not scope_owner_valid:
            reasons.append(
                f"owner mismatch (got '{raw_chunk.owner_session_id}', "
                f"expected '{expected_owner_session_id}')"
            )
        if not scope_repo_valid:
            reasons.append(
                f"repo mismatch (got '{raw_chunk.repository_id}', "
                f"expected '{expected_repository_id}')"
            )
        if not scope_gen_valid:
            reasons.append(
                f"generation mismatch (got '{raw_chunk.generation_id}', "
                f"expected '{expected_generation_id}')"
            )
        failure_reason = "; ".join(reasons)

    return CitationValidationResult(
        is_valid=is_valid,
        path_exists=path_exists,
        line_range_valid=line_range_valid,
        scope_owner_valid=scope_owner_valid,
        scope_repo_valid=scope_repo_valid,
        scope_gen_valid=scope_gen_valid,
        failure_reason=failure_reason,
    )


@dataclass(frozen=True)
class ExpectedEvidenceCase:
    relative_path: str
    symbol_name: str
    symbol_type: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class QuestionCase:
    id: str
    question: str
    category: str
    repository_fixture: str
    expected_insufficient_evidence: bool
    expected_paths: tuple[str, ...]
    expected_symbols: tuple[str, ...]
    expected_evidence: tuple[ExpectedEvidenceCase, ...]


@dataclass
class EvalMetricsReport:
    total_questions: int
    retrieval_recall_at_1: float
    retrieval_recall_at_3: float
    retrieval_recall_at_5: float
    per_question_hit_at_5_count: int
    citation_validity_rate: float
    citation_path_exists_rate: float
    citation_line_range_valid_rate: float
    citation_scope_isolation_rate: float
    unsupported_question_safety_rate: float


def validate_question_set(
    question_set_path: Path, base_dir: Path
) -> tuple[list[QuestionCase], str, str, list[str]]:
    errors: list[str] = []
    cases: list[QuestionCase] = []

    if not question_set_path.exists():
        return [], "", "", [f"Question set file not found: {question_set_path}"]

    try:
        with open(question_set_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as err:
        return [], "", "", [f"Question set JSON invalid: {err}"]

    corpus_ver = data.get("corpus_version", "")
    eval_ver = data.get("evaluation_set_version", "")
    if not corpus_ver or not eval_ver:
        errors.append("corpus_version and evaluation_set_version required")

    questions_raw = data.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        errors.append("questions array required and must not be empty")
        return [], corpus_ver, eval_ver, errors

    seen_ids: set[str] = set()

    for idx, q in enumerate(questions_raw):
        qid = q.get("id")
        if not isinstance(qid, str) or not qid.strip():
            errors.append(f"Question index {idx}: id must be non-empty string")
            continue
        if qid in seen_ids:
            errors.append(f"Question id {qid}: duplicate ID")
        seen_ids.add(qid)

        q_text = q.get("question")
        if not isinstance(q_text, str) or not q_text.strip():
            errors.append(f"Question {qid}: question string required")

        repo_fix = q.get("repository_fixture")
        if not isinstance(repo_fix, str) or not repo_fix.strip():
            errors.append(f"Question {qid}: repository_fixture path string required")
            continue

        fix_dir = base_dir / repo_fix
        if not fix_dir.exists() or not fix_dir.is_dir():
            errors.append(f"Question {qid}: fixture directory {repo_fix} not found")

        exp_insuff = bool(q.get("expected_insufficient_evidence", False))
        exp_paths = tuple(q.get("expected_paths", []))
        exp_symbols = tuple(q.get("expected_symbols", []))

        parsed_ev: list[ExpectedEvidenceCase] = []
        for ev in q.get("expected_evidence", []):
            rel_path = ev.get("relative_path", "")
            file_p = fix_dir / rel_path
            if not file_p.exists() or not file_p.is_file():
                errors.append(f"Question {qid}: evidence file {rel_path} not found")
                continue
            lines = len(file_p.read_text(encoding="utf-8").splitlines())
            s_line = ev.get("start_line", 0)
            e_line = ev.get("end_line", 0)
            if s_line < 1 or e_line < s_line or e_line > lines:
                errors.append(
                    f"Question {qid}: evidence line range {s_line}-{e_line} "
                    f"invalid for {rel_path} ({lines} lines)"
                )
            parsed_ev.append(
                ExpectedEvidenceCase(
                    relative_path=rel_path,
                    symbol_name=ev.get("symbol_name", ""),
                    symbol_type=ev.get("symbol_type", ""),
                    start_line=s_line,
                    end_line=e_line,
                )
            )

        cases.append(
            QuestionCase(
                id=qid,
                question=q_text,
                category=q.get("category", "general"),
                repository_fixture=repo_fix,
                expected_insufficient_evidence=exp_insuff,
                expected_paths=exp_paths,
                expected_symbols=exp_symbols,
                expected_evidence=tuple(parsed_ev),
            )
        )

    return cases, corpus_ver, eval_ver, errors


def run_retrieval_evaluation(
    question_set_path: Path = DEFAULT_QUESTION_SET_PATH,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    base_dir: Path = BACKEND_DIR,
) -> tuple[EvalMetricsReport, list[dict[str, Any]], bool]:
    cases, corpus_ver, eval_ver, errors = validate_question_set(question_set_path, base_dir)
    if errors:
        raise ValueError(f"Question set validation failed: {errors}")

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
            # Active generation chunks
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
            # Tempting chunks under second generation (eval_gen_002) for isolation testing
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
            # Tempting chunks under wrong owner and wrong repository
            c_wrong_owner = CodeChunk(
                chunk_id=f"{p.chunk_id}_wrong_owner",
                repository_id=p.repository_id,
                owner_session_id="eval_session_owner_other",
                relative_path=p.relative_path,
                language=p.language,
                symbol_name=p.symbol_name,
                symbol_type=p.symbol_type,
                start_line=p.start_line,
                end_line=p.end_line,
                content=p.content,
                content_hash=f"{p.content_hash}_wrong_owner",
                parser_version=p.parser_version,
                created_at=now,
                generation_id=active_generation_id,
            )
            c_wrong_repo = CodeChunk(
                chunk_id=f"{p.chunk_id}_wrong_repo",
                repository_id="eval_repo_other",
                owner_session_id=p.owner_session_id,
                relative_path=p.relative_path,
                language=p.language,
                symbol_name=p.symbol_name,
                symbol_type=p.symbol_type,
                start_line=p.start_line,
                end_line=p.end_line,
                content=p.content,
                content_hash=f"{p.content_hash}_wrong_repo",
                parser_version=p.parser_version,
                created_at=now,
                generation_id=active_generation_id,
            )
            chunk_repo.add_chunks((c_active, c_other_gen, c_wrong_owner, c_wrong_repo))

    service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
    )

    per_question: list[dict[str, Any]] = []
    latencies: list[float] = []

    recalls_at_1: list[float] = []
    recalls_at_3: list[float] = []
    recalls_at_5: list[float] = []

    citation_valid_count = 0
    citation_total_count = 0
    citation_path_exists_count = 0
    citation_lines_valid_count = 0
    citation_scope_count = 0

    unsupported_correct_count = 0
    unsupported_total_count = 0
    hit_count = 0

    for case in cases:
        t0 = time.monotonic()
        res: GroundedEvidenceResult = service.retrieve(
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            query=case.question,
            limit=5,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        latencies.append(elapsed_ms)

        retrieved_items = res.items
        retrieved_paths = tuple(item.citation.relative_path for item in retrieved_items)
        retrieved_symbols = tuple(item.citation.symbol_name for item in retrieved_items)

        def calc_recall(
            k: int, q_case: QuestionCase = case, r_items: Sequence[Any] = retrieved_items
        ) -> float:
            if q_case.expected_insufficient_evidence:
                return 1.0 if not r_items else 0.0
            top_items = r_items[:k]
            top_p = set(it.citation.relative_path for it in top_items)
            top_s = set(it.citation.symbol_name for it in top_items)

            expected_targets = set(q_case.expected_paths) | set(q_case.expected_symbols)
            if not expected_targets:
                return 1.0

            found = 0
            for p in q_case.expected_paths:
                if p in top_p:
                    found += 1
            for s in q_case.expected_symbols:
                if s in top_s:
                    found += 1
            return min(1.0, found / len(expected_targets))

        r1 = calc_recall(1)
        r3 = calc_recall(3)
        r5 = calc_recall(5)

        recalls_at_1.append(r1)
        recalls_at_3.append(r3)
        recalls_at_5.append(r5)

        is_hit_at_5 = r5 > 0.0 and not case.expected_insufficient_evidence
        if is_hit_at_5:
            hit_count += 1

        case_citations_valid = True
        for item in retrieved_items:
            citation_total_count += 1
            val_res = validate_retrieved_citation(
                citation=item.citation,
                chunk_id=item.chunk_id,
                file_line_counts=file_line_counts,
                expected_owner_session_id=owner_session_id,
                expected_repository_id=repository_id,
                expected_generation_id=active_generation_id,
                code_chunk_repo=chunk_repo,
            )

            if val_res.path_exists:
                citation_path_exists_count += 1
            if val_res.line_range_valid:
                citation_lines_valid_count += 1
            if val_res.scope_owner_valid and val_res.scope_repo_valid and val_res.scope_gen_valid:
                citation_scope_count += 1

            if val_res.is_valid:
                citation_valid_count += 1
            else:
                case_citations_valid = False

        if case.expected_insufficient_evidence:
            unsupported_total_count += 1
            if len(retrieved_items) == 0:
                unsupported_correct_count += 1

        per_question.append(
            {
                "id": case.id,
                "question": case.question,
                "category": case.category,
                "expected_insufficient_evidence": case.expected_insufficient_evidence,
                "actual_insufficient_evidence": len(retrieved_items) == 0,
                "retrieved_count": len(retrieved_items),
                "retrieved_paths": list(retrieved_paths),
                "retrieved_symbols": list(retrieved_symbols),
                "recall_at_1": round(r1, 4),
                "recall_at_3": round(r3, 4),
                "recall_at_5": round(r5, 4),
                "hit_at_5": is_hit_at_5,
                "citation_valid": case_citations_valid,
            }
        )

    mean_r1 = sum(recalls_at_1) / len(recalls_at_1)
    mean_r3 = sum(recalls_at_3) / len(recalls_at_3)
    mean_r5 = sum(recalls_at_5) / len(recalls_at_5)

    cit_validity_rate = (
        citation_valid_count / citation_total_count if citation_total_count > 0 else 1.0
    )
    cit_path_rate = (
        citation_path_exists_count / citation_total_count if citation_total_count > 0 else 1.0
    )
    cit_lines_rate = (
        citation_lines_valid_count / citation_total_count if citation_total_count > 0 else 1.0
    )
    cit_scope_rate = (
        citation_scope_count / citation_total_count if citation_total_count > 0 else 1.0
    )

    unsupported_safety_rate = (
        unsupported_correct_count / unsupported_total_count if unsupported_total_count > 0 else 1.0
    )

    metrics = EvalMetricsReport(
        total_questions=len(cases),
        retrieval_recall_at_1=round(mean_r1, 4),
        retrieval_recall_at_3=round(mean_r3, 4),
        retrieval_recall_at_5=round(mean_r5, 4),
        per_question_hit_at_5_count=hit_count,
        citation_validity_rate=round(cit_validity_rate, 4),
        citation_path_exists_rate=round(cit_path_rate, 4),
        citation_line_range_valid_rate=round(cit_lines_rate, 4),
        citation_scope_isolation_rate=round(cit_scope_rate, 4),
        unsupported_question_safety_rate=round(unsupported_safety_rate, 4),
    )

    passed = (
        metrics.retrieval_recall_at_5 >= 0.80
        and metrics.citation_validity_rate == 1.0
        and metrics.unsupported_question_safety_rate == 1.0
    )

    os.makedirs(results_dir, exist_ok=True)
    out_payload = {
        "corpus_version": corpus_ver,
        "evaluation_set_version": eval_ver,
        "passed": passed,
        "metrics": asdict(metrics),
        "per_question": per_question,
    }

    with open(results_dir / "retrieval_eval_result.v1.json", "w", encoding="utf-8") as f:
        json.dump(out_payload, f, indent=2)

    s_lat = sorted(latencies)
    telemetry_payload = {
        "corpus_version": corpus_ver,
        "evaluation_set_version": eval_ver,
        "mean_latency_ms": round(sum(s_lat) / len(s_lat), 3),
        "p50_latency_ms": round(s_lat[len(s_lat) // 2], 3),
        "p95_latency_ms": round(s_lat[int(len(s_lat) * 0.95)], 3),
        "max_latency_ms": round(s_lat[-1], 3),
        "per_question_latency_ms": {
            q["id"]: q_lat for q, q_lat in zip(per_question, latencies, strict=True)
        },
    }
    with open(results_dir / "retrieval_eval_telemetry.v1.json", "w", encoding="utf-8") as f:
        json.dump(telemetry_payload, f, indent=2)

    return metrics, per_question, passed


def main() -> int:
    metrics, per_q, passed = run_retrieval_evaluation()
    print("\n--- SOURCETRACE RETRIEVAL & CITATION EVALUATION REPORT ---")
    print(f"Status:               {'PASSED' if passed else 'FAILED'}")
    print(f"Total Questions:      {metrics.total_questions}")
    print(f"Recall@1:             {metrics.retrieval_recall_at_1:.2%}")
    print(f"Recall@3:             {metrics.retrieval_recall_at_3:.2%}")
    print(f"Recall@5:             {metrics.retrieval_recall_at_5:.2%}")
    targeted_q_count = metrics.total_questions - 1
    print(f"Per-Question Hit@5:   {metrics.per_question_hit_at_5_count} / {targeted_q_count}")
    print(f"Citation Validity:    {metrics.citation_validity_rate:.2%}")
    print(f"Unsupported Safety:   {metrics.unsupported_question_safety_rate:.2%}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
