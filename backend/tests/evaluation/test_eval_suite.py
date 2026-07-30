"""Offline unit & integration tests for evaluation benchmark suite (EVAL-001 Parts 1, 2 & 3)."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
EVAL_DIR = Path(__file__).resolve().parent
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import pytest
from run_grounded_eval import (
    DEFAULT_SCENARIO_SET_PATH,
    DeterministicFakeGenerationProvider,
    run_grounded_evaluation,
    validate_scenario_set,
)
from run_retrieval_eval import (
    DEFAULT_QUESTION_SET_PATH,
    FakeEvalCodeChunkRepository,
    FakeEvalRepositoryRepository,
    run_retrieval_evaluation,
    validate_question_set,
    validate_retrieved_citation,
)
from run_unified_eval import (
    DEFAULT_THRESHOLDS_PATH,
    run_unified_evaluation,
)

from sourcetrace.core.exceptions import RetrievalError
from sourcetrace.generation.service import GroundedAnswerService
from sourcetrace.models.domain import CitationRecord, CodeChunk, RepositoryRecord
from sourcetrace.retrieval.service import SemanticRetrievalService

# ---------------------------------------------------------------------------
# Part 1: Retrieval & Citation Evaluation Tests
# ---------------------------------------------------------------------------


def test_question_set_schema_and_corpus_validation():
    """Verify question_set.v1.json passes schema and fixture line count validation."""
    cases, corpus_ver, eval_ver, errors = validate_question_set(
        DEFAULT_QUESTION_SET_PATH, BACKEND_DIR
    )
    assert not errors, f"Question set validation failed with errors: {errors}"
    assert corpus_ver == "v1.0.0"
    assert eval_ver == "v1.0.0"
    assert len(cases) == 6

    categories = {c.category for c in cases}
    assert "entrypoint" in categories
    assert "authentication" in categories
    assert "routing" in categories
    assert "configuration" in categories
    assert "orientation" in categories
    assert "unsupported" in categories


def test_result_json_is_byte_for_byte_deterministic(tmp_path: Path):
    """Verify running evaluation twice yields 100% byte-for-byte identical result JSON files."""
    res_dir1 = tmp_path / "res1"
    res_dir2 = tmp_path / "res2"

    metrics1, per_q1, passed1 = run_retrieval_evaluation(
        DEFAULT_QUESTION_SET_PATH, res_dir1, BACKEND_DIR
    )
    metrics2, per_q2, passed2 = run_retrieval_evaluation(
        DEFAULT_QUESTION_SET_PATH, res_dir2, BACKEND_DIR
    )

    assert passed1 is True
    assert passed2 is True

    json_file1 = res_dir1 / "retrieval_eval_result.v1.json"
    json_file2 = res_dir2 / "retrieval_eval_result.v1.json"

    assert json_file1.exists()
    assert json_file2.exists()

    content1 = json_file1.read_text(encoding="utf-8")
    content2 = json_file2.read_text(encoding="utf-8")

    msg = "Primary evaluation result JSON is not byte-for-byte deterministic!"
    assert content1 == content2, msg


def test_generation_filtering_exact_semantics():
    """Verify FakeEvalCodeChunkRepository matches production generation filtering semantics."""
    repo = FakeEvalCodeChunkRepository()
    now = datetime.now(UTC)

    c_active = CodeChunk(
        chunk_id="chunk_active",
        repository_id="repo_001",
        owner_session_id="owner_001",
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass",
        content_hash="h1",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    c_stale = CodeChunk(
        chunk_id="chunk_stale",
        repository_id="repo_001",
        owner_session_id="owner_001",
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass # OLD STALE VERSION",
        content_hash="h2",
        parser_version="v1",
        created_at=now,
        generation_id="gen_002",
    )

    c_legacy = CodeChunk(
        chunk_id="chunk_legacy",
        repository_id="repo_001",
        owner_session_id="owner_001",
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass # LEGACY UNLINKED",
        content_hash="h3",
        parser_version="v1",
        created_at=now,
        generation_id=None,
    )

    repo.add_chunks((c_active, c_stale, c_legacy))

    # 1. Querying gen_001 returns ONLY gen_001 chunk
    res_gen1 = repo.search_lexical("owner_001", "repo_001", "generate_session_token", 10, "gen_001")
    assert len(res_gen1) == 1
    assert res_gen1[0].chunk.chunk_id == "chunk_active"

    # 2. Querying gen_002 directly returns ONLY gen_002 chunk
    res_gen2 = repo.search_lexical("owner_001", "repo_001", "generate_session_token", 10, "gen_002")
    assert len(res_gen2) == 1
    assert res_gen2[0].chunk.chunk_id == "chunk_stale"

    # 3. Querying generation_id=None returns ONLY legacy chunk
    res_legacy = repo.search_lexical("owner_001", "repo_001", "generate_session_token", 10, None)
    assert len(res_legacy) == 1
    assert res_legacy[0].chunk.chunk_id == "chunk_legacy"

    # 4. Querying sentinel '__ALL_GENERATIONS__' returns all 3 chunks
    res_all = repo.search_lexical(
        "owner_001", "repo_001", "generate_session_token", 10, "__ALL_GENERATIONS__"
    )
    assert len(res_all) == 3


def test_service_level_active_generation_isolation():
    """Verify SemanticRetrievalService active generation resolution excludes stale chunks."""
    owner_id = "owner_001"
    repo_id = "repo_001"

    repo_repo = FakeEvalRepositoryRepository(owner_id, repo_id, active_generation_id="gen_001")
    chunk_repo = FakeEvalCodeChunkRepository()
    now = datetime.now(UTC)

    c_active = CodeChunk(
        chunk_id="chunk_gen1_auth",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass # ACTIVE GEN 001",
        content_hash="h1",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    c_stale = CodeChunk(
        chunk_id="chunk_gen2_auth",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass # STALE GEN 002",
        content_hash="h2",
        parser_version="v1",
        created_at=now,
        generation_id="gen_002",
    )

    chunk_repo.add_chunks((c_active, c_stale))

    service = SemanticRetrievalService(repository_repo=repo_repo, code_chunk_repo=chunk_repo)

    res = service.retrieve(owner_id, repo_id, "authentication generate_session_token", limit=5)
    assert len(res.items) > 0
    for item in res.items:
        assert item.chunk_id == "chunk_gen1_auth"
        assert item.citation.relative_path == "auth.py"
        c_obj = chunk_repo.get_by_id(owner_id, repo_id, item.chunk_id)
        assert c_obj is not None
        assert c_obj.generation_id == "gen_001"
        assert "STALE GEN 002" not in c_obj.content


def test_service_level_owner_and_repository_isolation():
    """Verify SemanticRetrievalService.retrieve enforces strict owner and repository isolation."""
    owner_1 = "owner_001"
    repo_1 = "repo_001"
    owner_2 = "owner_002"
    repo_2 = "repo_002"

    repo_repo = FakeEvalRepositoryRepository(owner_1, repo_1, active_generation_id="gen_001")
    repo_repo.add_record(
        RepositoryRecord(
            repository_id=repo_2,
            owner_session_id=owner_1,
            name="repo_2",
            source_type="github",
            status="ready",
            file_count=1,
            chunk_count=1,
            active_generation_id="gen_001",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )

    chunk_repo = FakeEvalCodeChunkRepository()
    now = datetime.now(UTC)

    c_valid = CodeChunk(
        chunk_id="chunk_valid",
        repository_id=repo_1,
        owner_session_id=owner_1,
        relative_path="auth.py",
        language="python",
        symbol_name="auth",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def auth(): pass",
        content_hash="h1",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    c_other_owner = CodeChunk(
        chunk_id="chunk_other_owner",
        repository_id=repo_1,
        owner_session_id=owner_2,
        relative_path="auth.py",
        language="python",
        symbol_name="auth",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def auth(): pass # OTHER OWNER",
        content_hash="h2",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    c_other_repo = CodeChunk(
        chunk_id="chunk_other_repo",
        repository_id=repo_2,
        owner_session_id=owner_1,
        relative_path="auth.py",
        language="python",
        symbol_name="auth",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def auth(): pass # OTHER REPO",
        content_hash="h3",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    chunk_repo.add_chunks((c_valid, c_other_owner, c_other_repo))
    service = SemanticRetrievalService(repository_repo=repo_repo, code_chunk_repo=chunk_repo)

    res = service.retrieve(owner_1, repo_1, "auth", limit=5)
    assert len(res.items) == 1
    assert res.items[0].chunk_id == "chunk_valid"

    # Querying repo_2 for owner_1 returns ONLY repo_2 chunk
    res_repo2 = service.retrieve(owner_1, repo_2, "auth", limit=5)
    assert len(res_repo2.items) == 1
    assert res_repo2.items[0].chunk_id == "chunk_other_repo"

    # Querying repo_1 with wrong owner raises RetrievalError
    with pytest.raises(RetrievalError):
        service.retrieve(owner_2, repo_1, "auth", limit=5)


def test_service_level_legacy_generation_isolation_and_migration():
    """Verify SemanticRetrievalService auto-migrates legacy chunks and excludes non-null gens."""
    owner_id = "owner_legacy"
    repo_id = "repo_legacy"

    repo_repo = FakeEvalRepositoryRepository(owner_id, repo_id, active_generation_id=None)
    chunk_repo = FakeEvalCodeChunkRepository()
    now = datetime.now(UTC)

    c_legacy = CodeChunk(
        chunk_id="chunk_legacy_auth",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass # LEGACY UNLINKED",
        content_hash="h1",
        parser_version="v1",
        created_at=now,
        generation_id=None,
    )

    c_stale_modern = CodeChunk(
        chunk_id="chunk_modern_stale",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass # MODERN STALE GEN 002",
        content_hash="h2",
        parser_version="v1",
        created_at=now,
        generation_id="gen_002",
    )

    chunk_repo.add_chunks((c_legacy, c_stale_modern))
    service = SemanticRetrievalService(repository_repo=repo_repo, code_chunk_repo=chunk_repo)

    res = service.retrieve(owner_id, repo_id, "authentication generate_session_token", limit=5)
    assert len(res.items) > 0

    repo_rec = repo_repo.get_by_id(owner_id, repo_id)
    assert repo_rec is not None
    assert repo_rec.active_generation_id == f"job_ref_legacy_{repo_id}"

    for item in res.items:
        assert item.chunk_id == "chunk_legacy_auth"
        c_obj = chunk_repo.get_by_id(owner_id, repo_id, item.chunk_id)
        assert c_obj is not None
        assert c_obj.generation_id == f"job_ref_legacy_{repo_id}"
        assert "MODERN STALE GEN 002" not in c_obj.content


def test_citation_validator_detects_all_failure_modes():
    """Verify validate_retrieved_citation detects bad path, bad lines, and scope mismatch."""
    file_line_counts = {"main.py": 20, "auth.py": 15}
    repo = FakeEvalCodeChunkRepository()
    now = datetime.now(UTC)

    c_valid = CodeChunk(
        chunk_id="chunk_valid",
        repository_id="repo_001",
        owner_session_id="owner_001",
        relative_path="auth.py",
        language="python",
        symbol_name="auth",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def auth(): pass",
        content_hash="h1",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    c_wrong_owner = CodeChunk(
        chunk_id="chunk_wrong_owner",
        repository_id="repo_001",
        owner_session_id="owner_other",
        relative_path="auth.py",
        language="python",
        symbol_name="auth",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def auth(): pass",
        content_hash="h2",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    c_wrong_repo = CodeChunk(
        chunk_id="chunk_wrong_repo",
        repository_id="repo_other",
        owner_session_id="owner_001",
        relative_path="auth.py",
        language="python",
        symbol_name="auth",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def auth(): pass",
        content_hash="h3",
        parser_version="v1",
        created_at=now,
        generation_id="gen_001",
    )

    c_wrong_gen = CodeChunk(
        chunk_id="chunk_wrong_gen",
        repository_id="repo_001",
        owner_session_id="owner_001",
        relative_path="auth.py",
        language="python",
        symbol_name="auth",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def auth(): pass",
        content_hash="h4",
        parser_version="v1",
        created_at=now,
        generation_id="gen_stale",
    )

    repo.add_chunks((c_valid, c_wrong_owner, c_wrong_repo, c_wrong_gen))

    # 1. Valid citation passes
    cit_valid = CitationRecord(
        relative_path="auth.py",
        start_line=1,
        end_line=10,
        symbol_name="auth",
        symbol_type="function",
    )
    res_valid = validate_retrieved_citation(
        citation=cit_valid,
        chunk_id="chunk_valid",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_valid.is_valid is True
    assert res_valid.failure_reason is None

    # 2. Nonexistent path fails
    cit_bad_path = CitationRecord(
        relative_path="ghost.py",
        start_line=1,
        end_line=5,
        symbol_name="auth",
        symbol_type="function",
    )
    res_bad_path = validate_retrieved_citation(
        citation=cit_bad_path,
        chunk_id="chunk_valid",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_bad_path.is_valid is False
    assert res_bad_path.path_exists is False

    # 3. Invalid line range (start_line < 1) fails
    cit_bad_line1 = CitationRecord(
        relative_path="auth.py",
        start_line=0,
        end_line=5,
        symbol_name="auth",
        symbol_type="function",
    )
    res_bad_line1 = validate_retrieved_citation(
        citation=cit_bad_line1,
        chunk_id="chunk_valid",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_bad_line1.is_valid is False
    assert res_bad_line1.line_range_valid is False

    # 4. Invalid line range (end_line < start_line) fails
    cit_bad_line2 = CitationRecord(
        relative_path="auth.py",
        start_line=10,
        end_line=5,
        symbol_name="auth",
        symbol_type="function",
    )
    res_bad_line2 = validate_retrieved_citation(
        citation=cit_bad_line2,
        chunk_id="chunk_valid",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_bad_line2.is_valid is False
    assert res_bad_line2.line_range_valid is False

    # 5. Invalid line range (end_line > max_lines) fails
    cit_bad_line3 = CitationRecord(
        relative_path="auth.py",
        start_line=1,
        end_line=999,
        symbol_name="auth",
        symbol_type="function",
    )
    res_bad_line3 = validate_retrieved_citation(
        citation=cit_bad_line3,
        chunk_id="chunk_valid",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_bad_line3.is_valid is False
    assert res_bad_line3.line_range_valid is False

    # 6. Wrong owner session ID fails scope validation
    res_wrong_owner = validate_retrieved_citation(
        citation=cit_valid,
        chunk_id="chunk_wrong_owner",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_wrong_owner.is_valid is False
    assert res_wrong_owner.scope_owner_valid is False

    # 7. Wrong repository ID fails scope validation
    res_wrong_repo = validate_retrieved_citation(
        citation=cit_valid,
        chunk_id="chunk_wrong_repo",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_wrong_repo.is_valid is False
    assert res_wrong_repo.scope_repo_valid is False

    # 8. Wrong generation ID fails scope validation
    res_wrong_gen = validate_retrieved_citation(
        citation=cit_valid,
        chunk_id="chunk_wrong_gen",
        file_line_counts=file_line_counts,
        expected_owner_session_id="owner_001",
        expected_repository_id="repo_001",
        expected_generation_id="gen_001",
        code_chunk_repo=repo,
    )
    assert res_wrong_gen.is_valid is False
    assert res_wrong_gen.scope_gen_valid is False


def test_correct_recall_and_hit_metrics():
    """Verify recall@1, recall@3, recall@5 hit metrics meet thresholds."""
    metrics, per_q, passed = run_retrieval_evaluation(
        DEFAULT_QUESTION_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    assert passed is True
    assert metrics.retrieval_recall_at_5 >= 0.80
    assert metrics.per_question_hit_at_5_count >= 5
    assert metrics.citation_validity_rate == 1.0


def test_targeted_questions_exclude_unrelated_fixture_module():
    """Verify targeted questions for entrypoint/auth/routes/config do not return analytics.py."""
    metrics, per_q, passed = run_retrieval_evaluation(
        DEFAULT_QUESTION_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    assert passed is True

    for q in per_q:
        if q["category"] in ("entrypoint", "authentication", "routing", "configuration"):
            assert "analytics.py" not in q["retrieved_paths"], (
                f"Question {q['id']} ({q['category']}) incorrectly retrieved analytics.py!"
            )


def test_unsupported_question_remains_safe():
    """Verify unsupported out-of-domain question returns no evidence and remains safe."""
    metrics, per_q, passed = run_retrieval_evaluation(
        DEFAULT_QUESTION_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    assert metrics.unsupported_question_safety_rate == 1.0

    unsupported_q = next(q for q in per_q if q["category"] == "unsupported")
    assert unsupported_q["retrieved_count"] == 0
    assert unsupported_q["expected_insufficient_evidence"] is True
    assert unsupported_q["actual_insufficient_evidence"] is True


# ---------------------------------------------------------------------------
# Part 2: Grounded-Answer Quality Evaluation Tests
# ---------------------------------------------------------------------------


def test_scenario_set_schema_and_fixture_validation():
    """Verify scenario_set.v1.json passes schema validation."""
    cases, corpus_ver, scenario_ver, errors = validate_scenario_set(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR
    )
    assert not errors, f"Scenario set validation failed with errors: {errors}"
    assert corpus_ver == "v1.0.0"
    assert scenario_ver == "v1.0.0"
    assert len(cases) == 7

    categories = {sc.category for sc in cases}
    assert "valid_grounded" in categories
    assert "multiple_citations" in categories
    assert "uncited_provider" in categories
    assert "invalid_marker" in categories
    assert "provider_failure" in categories
    assert "no_evidence" in categories
    assert "orientation" in categories


def test_grounded_eval_result_json_is_byte_for_byte_deterministic(tmp_path: Path):
    """Verify running grounded evaluation twice yields 100% byte-for-byte identical result JSON."""
    res_dir1 = tmp_path / "g_res1"
    res_dir2 = tmp_path / "g_res2"

    metrics1, per_sc1, passed1 = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, res_dir1, BACKEND_DIR
    )
    metrics2, per_sc2, passed2 = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, res_dir2, BACKEND_DIR
    )

    assert passed1 is True
    assert passed2 is True

    json_file1 = res_dir1 / "grounded_eval_result.v1.json"
    json_file2 = res_dir2 / "grounded_eval_result.v1.json"

    assert json_file1.exists()
    assert json_file2.exists()

    content1 = json_file1.read_text(encoding="utf-8")
    content2 = json_file2.read_text(encoding="utf-8")

    msg = "Primary grounded evaluation result JSON is not byte-for-byte deterministic!"
    assert content1 == content2, msg


def test_valid_grounded_answer_scenario():
    """Verify Scenario 01 (valid grounded answer) returns normal mode and valid citations."""
    metrics, per_sc, passed = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    assert passed is True

    sc01 = next(sc for sc in per_sc if sc["id"] == "scenario-01")
    assert sc01["passed"] is True
    assert sc01["answer_mode"] == "normal"
    assert sc01["insufficient_evidence"] is False
    assert sc01["citation_count"] == 1
    assert sc01["citation_valid"] is True
    assert sc01["reason_code"] == "VALID_GROUNDED_ANSWER"


def test_multiple_valid_citations_scenario():
    """Verify Scenario 02 (multiple valid citations) maps all cited evidence markers."""
    metrics, per_sc, passed = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    sc02 = next(sc for sc in per_sc if sc["id"] == "scenario-02")
    assert sc02["passed"] is True
    assert sc02["answer_mode"] == "normal"
    assert sc02["citation_count"] == 2
    assert sc02["citation_valid"] is True
    assert sc02["reason_code"] == "VALID_MULTIPLE_CITATIONS"


def test_uncited_provider_answer_safe_fallback():
    """Verify Scenario 03 (uncited provider answer) safely falls back to insufficient_evidence."""
    metrics, per_sc, passed = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    sc03 = next(sc for sc in per_sc if sc["id"] == "scenario-03")
    assert sc03["passed"] is True
    assert sc03["answer_mode"] == "insufficient_evidence"
    assert sc03["insufficient_evidence"] is True
    assert sc03["citation_count"] == 0
    assert sc03["reason_code"] == "UNCITED_PROVIDER_FALLBACK"


def test_invalid_citation_marker_safe_downgrade():
    """Verify Scenario 04 (invalid marker [E99]) safely downgrades without trusting [E99]."""
    metrics, per_sc, passed = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    sc04 = next(sc for sc in per_sc if sc["id"] == "scenario-04")
    assert sc04["passed"] is True
    assert sc04["answer_mode"] == "insufficient_evidence"
    assert sc04["insufficient_evidence"] is True
    assert sc04["citation_count"] == 0
    assert sc04["reason_code"] == "INVALID_MARKER_FALLBACK"


def test_provider_failure_safe_fallback():
    """Verify Scenario 05 (provider exception) converts to static_guidance without errors."""
    metrics, per_sc, passed = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    sc05 = next(sc for sc in per_sc if sc["id"] == "scenario-05")
    assert sc05["passed"] is True
    assert sc05["answer_mode"] == "static_guidance"
    assert sc05["insufficient_evidence"] is False
    assert sc05["citation_count"] == 2
    assert sc05["reason_code"] == "PROVIDER_FAILURE_FALLBACK"


def test_no_retrieval_evidence_skips_provider():
    """Verify Scenario 06 (unsupported question) skips LLM provider call entirely."""
    metrics, per_sc, passed = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    sc06 = next(sc for sc in per_sc if sc["id"] == "scenario-06")
    assert sc06["passed"] is True
    assert sc06["provider_called"] is False
    assert sc06["answer_mode"] == "insufficient_evidence"
    assert sc06["insufficient_evidence"] is True
    assert sc06["citation_count"] == 0
    assert sc06["reason_code"] == "NO_EVIDENCE_SAFEGUARD"


def test_orientation_question_grounded_answer():
    """Verify Scenario 07 (orientation question) returns orientation mode with valid citations."""
    metrics, per_sc, passed = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, BACKEND_DIR / "tests" / "evaluation" / "results", BACKEND_DIR
    )
    sc07 = next(sc for sc in per_sc if sc["id"] == "scenario-07")
    assert sc07["passed"] is True
    assert sc07["answer_mode"] == "orientation"
    assert sc07["insufficient_evidence"] is False
    assert sc07["citation_count"] == 2
    assert sc07["reason_code"] == "VALID_ORIENTATION_ANSWER"


def test_citations_scope_isolation_under_grounded_answer_service():
    """Verify citations under GroundedAnswerService cannot reference outside scope."""
    owner_id = "owner_scope_01"
    repo_id = "repo_scope_01"

    repo_repo = FakeEvalRepositoryRepository(owner_id, repo_id, active_generation_id="gen_active")
    chunk_repo = FakeEvalCodeChunkRepository()
    now = datetime.now(UTC)

    c_active = CodeChunk(
        chunk_id="chunk_active_auth",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass",
        content_hash="h1",
        parser_version="v1",
        created_at=now,
        generation_id="gen_active",
    )

    c_stale = CodeChunk(
        chunk_id="chunk_stale_auth",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="auth.py",
        language="python",
        symbol_name="generate_session_token",
        symbol_type="function",
        start_line=1,
        end_line=5,
        content="def generate_session_token(): pass # STALE GENERATION",
        content_hash="h2",
        parser_version="v1",
        created_at=now,
        generation_id="gen_stale",
    )

    chunk_repo.add_chunks((c_active, c_stale))
    retrieval_service = SemanticRetrievalService(
        repository_repo=repo_repo, code_chunk_repo=chunk_repo
    )

    provider = DeterministicFakeGenerationProvider(
        behavior="text",
        output_text="Authentication token generation is in auth.py [E1].",
    )
    answer_service = GroundedAnswerService(
        retrieval_service=retrieval_service, generation_provider=provider
    )

    res = answer_service.generate_answer(
        owner_id, repo_id, "How does authentication work?", limit=5
    )
    assert len(res.citations) == 1
    val_res = validate_retrieved_citation(
        citation=res.citations[0],
        chunk_id="chunk_active_auth",
        file_line_counts={"auth.py": 10},
        expected_owner_session_id=owner_id,
        expected_repository_id=repo_id,
        expected_generation_id="gen_active",
        code_chunk_repo=chunk_repo,
    )
    assert val_res.is_valid is True
    assert val_res.scope_gen_valid is True


# ---------------------------------------------------------------------------
# Part 3: Unified Evaluation Reporting and Offline CI Gate Tests
# ---------------------------------------------------------------------------


def test_unified_eval_success_with_current_corpus(tmp_path: Path):
    """Verify run_unified_evaluation passes 100% of quality gates with current corpus."""
    res_dir = tmp_path / "u_res"
    report, passed = run_unified_evaluation(
        thresholds_path=DEFAULT_THRESHOLDS_PATH,
        question_set_path=DEFAULT_QUESTION_SET_PATH,
        scenario_set_path=DEFAULT_SCENARIO_SET_PATH,
        results_dir=res_dir,
        base_dir=BACKEND_DIR,
    )
    assert passed is True
    assert report["passed"] is True
    assert report["status"] == "PASSED"
    assert report["reason_code"] == "PASS"
    assert report["failure_reason"] is None
    assert len(report["gate_checks"]) == 8
    assert all(c["passed"] for c in report["gate_checks"])


def test_unified_eval_json_is_byte_for_byte_deterministic(tmp_path: Path):
    """Verify running unified evaluation twice yields 100% byte-for-byte identical result JSON."""
    res_dir1 = tmp_path / "u_res1"
    res_dir2 = tmp_path / "u_res2"

    report1, passed1 = run_unified_evaluation(
        DEFAULT_THRESHOLDS_PATH,
        DEFAULT_QUESTION_SET_PATH,
        DEFAULT_SCENARIO_SET_PATH,
        res_dir1,
        BACKEND_DIR,
    )
    report2, passed2 = run_unified_evaluation(
        DEFAULT_THRESHOLDS_PATH,
        DEFAULT_QUESTION_SET_PATH,
        DEFAULT_SCENARIO_SET_PATH,
        res_dir2,
        BACKEND_DIR,
    )

    assert passed1 is True
    assert passed2 is True

    json1 = (res_dir1 / "unified_eval_result.v1.json").read_text(encoding="utf-8")
    json2 = (res_dir2 / "unified_eval_result.v1.json").read_text(encoding="utf-8")

    assert json1 == json2, "Primary unified eval result JSON is not byte-for-byte deterministic!"


def test_stricter_threshold_causes_gate_failure_and_non_zero_exit(tmp_path: Path):
    """Verify a stricter threshold causes gate failure with THRESHOLD_VIOLATION."""
    temp_cfg = tmp_path / "stricter_thresholds.json"
    temp_res = tmp_path / "stricter_res"

    cfg_data = {
        "thresholds_version": "v1.0.0",
        "corpus_version": "v1.0.0",
        "evaluation_set_version": "v1.0.0",
        "scenario_set_version": "v1.0.0",
        "thresholds": {
            "retrieval_recall_at_5": 0.99,  # Unreachable threshold (actual is 0.8889)
            "retrieval_citation_validity_rate": 1.00,
            "retrieval_unsupported_question_safety_rate": 1.00,
            "grounded_answer_pass_rate": 1.00,
            "valid_citation_marker_rate": 1.00,
            "citation_coverage_rate": 1.00,
            "uncited_provider_safe_fallback_rate": 1.00,
            "provider_failure_safe_fallback_rate": 1.00,
        },
    }
    temp_cfg.write_text(json.dumps(cfg_data), encoding="utf-8")

    report, passed = run_unified_evaluation(
        temp_cfg, DEFAULT_QUESTION_SET_PATH, DEFAULT_SCENARIO_SET_PATH, temp_res, BACKEND_DIR
    )

    assert passed is False
    assert report["passed"] is False
    assert report["status"] == "FAILED"
    assert report["reason_code"] == "THRESHOLD_VIOLATION"
    assert "THRESHOLD_VIOLATION" in report["failure_reason"]


def test_malformed_or_missing_retrieval_result_fails_closed(tmp_path: Path):
    """Verify nonexistent question set file fails closed with RETRIEVAL_EVAL_FAILED."""
    res_dir = tmp_path / "bad_q_res"
    bad_q_path = tmp_path / "nonexistent_q_set.json"

    report, passed = run_unified_evaluation(
        DEFAULT_THRESHOLDS_PATH, bad_q_path, DEFAULT_SCENARIO_SET_PATH, res_dir, BACKEND_DIR
    )
    assert passed is False
    assert report["passed"] is False
    assert report["reason_code"] in ("RETRIEVAL_EVAL_FAILED", "MISSING_RESULT_DATA")


def test_malformed_or_missing_grounded_result_fails_closed(tmp_path: Path):
    """Verify nonexistent scenario set file fails closed with GROUNDED_EVAL_FAILED."""
    res_dir = tmp_path / "bad_sc_res"
    bad_sc_path = tmp_path / "nonexistent_sc_set.json"

    report, passed = run_unified_evaluation(
        DEFAULT_THRESHOLDS_PATH, DEFAULT_QUESTION_SET_PATH, bad_sc_path, res_dir, BACKEND_DIR
    )
    assert passed is False
    assert report["passed"] is False
    assert report["reason_code"] in ("GROUNDED_EVAL_FAILED", "MISSING_RESULT_DATA")


def test_version_mismatch_fails_closed(tmp_path: Path):
    """Verify thresholds file with mismatched version fails closed with VERSION_MISMATCH."""
    temp_cfg = tmp_path / "mismatched_cfg.json"
    res_dir = tmp_path / "mismatched_res"

    cfg_data = {
        "thresholds_version": "v1.0.0",
        "corpus_version": "v9.9.9",  # Mismatched corpus version
        "evaluation_set_version": "v1.0.0",
        "scenario_set_version": "v1.0.0",
        "thresholds": {
            "retrieval_recall_at_5": 0.80,
            "retrieval_citation_validity_rate": 1.00,
            "retrieval_unsupported_question_safety_rate": 1.00,
            "grounded_answer_pass_rate": 1.00,
            "valid_citation_marker_rate": 1.00,
            "citation_coverage_rate": 1.00,
            "uncited_provider_safe_fallback_rate": 1.00,
            "provider_failure_safe_fallback_rate": 1.00,
        },
    }
    temp_cfg.write_text(json.dumps(cfg_data), encoding="utf-8")

    report, passed = run_unified_evaluation(
        temp_cfg, DEFAULT_QUESTION_SET_PATH, DEFAULT_SCENARIO_SET_PATH, res_dir, BACKEND_DIR
    )
    assert passed is False
    assert report["passed"] is False
    assert report["reason_code"] == "VERSION_MISMATCH"
    assert "VERSION_MISMATCH" in report["failure_reason"]


def test_telemetry_does_not_affect_gate_output(tmp_path: Path):
    """Verify primary result JSON contains zero telemetry fields and timing does not affect gate."""
    res_dir = tmp_path / "telem_res"
    report, passed = run_unified_evaluation(
        DEFAULT_THRESHOLDS_PATH,
        DEFAULT_QUESTION_SET_PATH,
        DEFAULT_SCENARIO_SET_PATH,
        res_dir,
        BACKEND_DIR,
    )
    assert passed is True

    json_text = (res_dir / "unified_eval_result.v1.json").read_text(encoding="utf-8")
    assert "latency" not in json_text.lower()
    assert "execution_ms" not in json_text.lower()

    # Telemetry file retains execution timing separately
    telemetry_text = (res_dir / "unified_eval_telemetry.v1.json").read_text(encoding="utf-8")
    assert "total_execution_ms" in telemetry_text


def test_failure_output_contains_no_raw_paths_or_parser_errors(tmp_path: Path):
    """Verify missing/malformed config reports contain zero raw file paths or JSON parser errors."""
    res_dir = tmp_path / "sanitized_res"

    # 1. Missing config file
    missing_cfg = tmp_path / "nonexistent_config.json"
    report_missing, passed_missing = run_unified_evaluation(
        missing_cfg, DEFAULT_QUESTION_SET_PATH, DEFAULT_SCENARIO_SET_PATH, res_dir, BACKEND_DIR
    )
    assert passed_missing is False
    assert report_missing["reason_code"] == "CONFIG_NOT_FOUND"
    assert report_missing["failure_reason"] == "Evaluation threshold configuration is unavailable."
    report_missing_str = json.dumps(report_missing)
    assert "nonexistent_config.json" not in report_missing_str
    assert "C:\\" not in report_missing_str and "D:\\" not in report_missing_str

    # 2. Malformed JSON config file
    malformed_cfg = tmp_path / "malformed_config.json"
    malformed_cfg.write_text("{ invalid json syntax }", encoding="utf-8")
    report_malformed, passed_malformed = run_unified_evaluation(
        malformed_cfg, DEFAULT_QUESTION_SET_PATH, DEFAULT_SCENARIO_SET_PATH, res_dir, BACKEND_DIR
    )
    assert passed_malformed is False
    assert report_malformed["reason_code"] == "CONFIG_INVALID"
    assert report_malformed["failure_reason"] == "Evaluation threshold configuration is invalid."
    report_malformed_str = json.dumps(report_malformed)
    assert "Expecting property name enclosed in double quotes" not in report_malformed_str
    assert "malformed_config.json" not in report_malformed_str

    # 3. CLI execution output sanitization check
    cmd = [
        sys.executable,
        str(EVAL_DIR / "run_unified_eval.py"),
        "--thresholds-file",
        str(malformed_cfg),
        "--results-dir",
        str(res_dir),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "CONFIG_INVALID" in proc.stdout
    assert "Evaluation threshold configuration is invalid." in proc.stdout
    assert "malformed_config.json" not in proc.stdout
    assert "Expecting property name" not in proc.stdout


def test_cli_exit_code_behavior(tmp_path: Path):
    """Verify run_unified_eval.py CLI exits 0 on success and non-zero on threshold failure."""
    res_dir = tmp_path / "cli_res"

    cmd_pass = [
        sys.executable,
        str(EVAL_DIR / "run_unified_eval.py"),
        "--results-dir",
        str(res_dir),
    ]
    proc_pass = subprocess.run(cmd_pass, capture_output=True, text=True)
    msg = f"Expected exit 0, got {proc_pass.returncode}: {proc_pass.stdout}"
    assert proc_pass.returncode == 0, msg

    # Stricter config causes non-zero exit code
    temp_cfg = tmp_path / "strict_cli_cfg.json"
    cfg_data = {
        "thresholds_version": "v1.0.0",
        "corpus_version": "v1.0.0",
        "evaluation_set_version": "v1.0.0",
        "scenario_set_version": "v1.0.0",
        "thresholds": {"retrieval_recall_at_5": 0.99},
    }
    temp_cfg.write_text(json.dumps(cfg_data), encoding="utf-8")

    cmd_fail = [
        sys.executable,
        str(EVAL_DIR / "run_unified_eval.py"),
        "--thresholds-file",
        str(temp_cfg),
        "--results-dir",
        str(res_dir),
    ]
    proc_fail = subprocess.run(cmd_fail, capture_output=True, text=True)
    assert proc_fail.returncode != 0, "Expected non-zero exit code on threshold failure"


def test_retrieval_and_grounded_runners_independently_runnable(tmp_path: Path):
    """Verify run_retrieval_eval.py and run_grounded_eval.py remain independently runnable."""
    r_dir = tmp_path / "ind_r"
    g_dir = tmp_path / "ind_g"

    ret_metrics, ret_pq, ret_pass = run_retrieval_evaluation(
        DEFAULT_QUESTION_SET_PATH, r_dir, BACKEND_DIR
    )
    assert ret_pass is True
    assert (r_dir / "retrieval_eval_result.v1.json").exists()

    gnd_metrics, gnd_psc, gnd_pass = run_grounded_evaluation(
        DEFAULT_SCENARIO_SET_PATH, g_dir, BACKEND_DIR
    )
    assert gnd_pass is True
    assert (g_dir / "grounded_eval_result.v1.json").exists()
