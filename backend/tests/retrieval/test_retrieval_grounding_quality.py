"""Regression tests for question answering quality and grounding."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sourcetrace.generation.client import GenerationMessage, GenerationProvider
from sourcetrace.generation.service import GroundedAnswerService
from sourcetrace.models.domain import (
    CodeChunk,
    RepositoryRecord,
    RetrievalResult,
)
from sourcetrace.retrieval.service import SemanticRetrievalService


class MockGenerationProvider(GenerationProvider):
    """Mock LLM provider returning configurable answers."""

    def __init__(self, answer: str = "") -> None:
        self.answer = answer
        self.received_messages: list[GenerationMessage] = []

    @property
    def model_identifier(self) -> str:
        return "mock-provider"

    def generate(self, messages: tuple[GenerationMessage, ...] | list[GenerationMessage]) -> str:
        self.received_messages = list(messages)
        return self.answer


def create_dummy_chunk(
    owner_session_id: str = "sess_test_owner",
    repository_id: str = "repo_test_01",
    generation_id: str = "job_ref_01",
    chunk_id: str = "chunk_01",
    relative_path: str = "src/app.py",
    symbol_name: str = "main",
    symbol_type: str = "function",
    start_line: int = 1,
    end_line: int = 10,
    content: str = "def main():\n    print('Hello World')\n",
) -> CodeChunk:
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        generation_id=generation_id,
        relative_path=relative_path,
        language="python",
        symbol_name=symbol_name,
        symbol_type=symbol_type,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash="hash_01",
        parser_version="v1",
        created_at=datetime.now(UTC),
    )


def test_auth_query_with_only_startup_candidates_returns_insufficient_evidence():
    """Auth query with only startup candidates returns insufficient_evidence with 0 citations."""
    repo_record = RepositoryRecord(
        repository_id="repo_test_01",
        owner_session_id="sess_test_owner",
        name="TestRepo",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id="job_ref_01",
    )
    repo_mock = MagicMock()
    repo_mock.get_by_id.return_value = repo_record

    startup_chunk_1 = create_dummy_chunk(
        chunk_id="chunk_main",
        relative_path="bottle.py",
        symbol_name="main",
        symbol_type="function",
        content="def main():\n    optparser = OptionParser()\n    optparser.add_option('--bind')\n",
    )
    startup_chunk_2 = create_dummy_chunk(
        chunk_id="chunk_submain",
        relative_path="bottle.py",
        symbol_name="_main",
        symbol_type="function",
        content="def _main():\n    main()\n",
    )

    code_chunk_mock = MagicMock()
    code_chunk_mock.list_by_repository.return_value = [startup_chunk_1, startup_chunk_2]
    code_chunk_mock.search_lexical.return_value = [
        RetrievalResult(chunk=startup_chunk_1, score=0.8),
        RetrievalResult(chunk=startup_chunk_2, score=0.75),
    ]

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_mock,
        code_chunk_repo=code_chunk_mock,
        embedding_provider=None,
    )

    provider = MockGenerationProvider(
        answer="The provided snippets [E1][E2] show entry points main and _main."
    )

    answer_svc = GroundedAnswerService(
        retrieval_service=retrieval_svc,
        generation_provider=provider,
    )

    result = answer_svc.generate_answer(
        owner_session_id="sess_test_owner",
        repository_id="repo_test_01",
        question="How does authentication work?",
    )

    assert result.insufficient_evidence
    assert result.answer_mode == "insufficient_evidence"
    assert len(result.citations) == 0
    expected_ans = (
        "I do not have enough retrieved evidence from the indexed repository "
        "to answer this question."
    )
    assert result.answer == expected_ans


def test_auth_query_with_startup_and_auth_candidates_selects_auth_evidence():
    """Auth query with both startup and auth candidates selects auth evidence."""
    repo_record = RepositoryRecord(
        repository_id="repo_test_01",
        owner_session_id="sess_test_owner",
        name="TestRepo",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id="job_ref_01",
    )
    repo_mock = MagicMock()
    repo_mock.get_by_id.return_value = repo_record

    startup_chunk = create_dummy_chunk(
        chunk_id="chunk_main",
        relative_path="bottle.py",
        symbol_name="main",
        symbol_type="function",
        content="def main():\n    pass\n",
    )
    auth_chunk = create_dummy_chunk(
        chunk_id="chunk_auth",
        relative_path="bottle.py",
        symbol_name="parse_auth",
        symbol_type="function",
        content=(
            "def parse_auth(header):\n"
            "    # Parse Basic/Bearer authentication header\n"
            "    return header.split()\n"
        ),
    )

    code_chunk_mock = MagicMock()
    code_chunk_mock.list_by_repository.return_value = [startup_chunk, auth_chunk]

    def mock_search_lexical(
        owner_session_id, repository_id, query_text, limit=5, generation_id=None
    ):
        if "auth" in query_text.lower():
            return [
                RetrievalResult(chunk=auth_chunk, score=0.85),
                RetrievalResult(chunk=startup_chunk, score=0.50),
            ]
        return [RetrievalResult(chunk=startup_chunk, score=0.60)]

    code_chunk_mock.search_lexical.side_effect = mock_search_lexical

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_mock,
        code_chunk_repo=code_chunk_mock,
        embedding_provider=None,
    )

    provider = MockGenerationProvider(
        answer=(
            "Authentication works by parsing headers in `parse_auth` [E1]. "
            "Entry main [E2] is unused."
        )
    )

    answer_svc = GroundedAnswerService(
        retrieval_service=retrieval_svc,
        generation_provider=provider,
    )

    result = answer_svc.generate_answer(
        owner_session_id="sess_test_owner",
        repository_id="repo_test_01",
        question="How does authentication work?",
    )

    assert result.answer_mode == "normal"
    assert not result.insufficient_evidence
    assert len(result.citations) == 1
    assert result.citations[0].relative_path == "bottle.py"
    assert result.citations[0].symbol_name == "parse_auth"


def test_orientation_query_without_readme_ranks_entrypoints_and_manifests():
    """Orientation query with no README but server.js/app.js/App.jsx returns reading guide."""
    repo_record = RepositoryRecord(
        repository_id="repo_test_fitsync",
        owner_session_id="sess_test_owner",
        name="FitSync",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id="job_ref_01",
    )
    repo_mock = MagicMock()
    repo_mock.get_by_id.return_value = repo_record

    server_chunk = create_dummy_chunk(
        chunk_id="c_server",
        repository_id="repo_test_fitsync",
        relative_path="backend/server.js",
        symbol_name="listen",
        symbol_type="function",
        content="const app = require('./app');\napp.listen(5000);\n",
    )
    app_chunk = create_dummy_chunk(
        chunk_id="c_app",
        repository_id="repo_test_fitsync",
        relative_path="backend/app.js",
        symbol_name="expressApp",
        symbol_type="module",
        content=(
            "const express = require('express');\n"
            "const app = express();\n"
            "module.exports = app;\n"
        ),
    )
    react_chunk = create_dummy_chunk(
        chunk_id="c_react",
        repository_id="repo_test_fitsync",
        relative_path="client/src/App.jsx",
        symbol_name="App",
        symbol_type="component",
        content="export default function App() { return <div>FitSync App</div>; }\n",
    )

    code_chunk_mock = MagicMock()
    code_chunk_mock.list_by_repository.return_value = [server_chunk, app_chunk, react_chunk]

    def mock_search_lexical(
        owner_session_id, repository_id, query_text, limit=5, generation_id=None
    ):
        q = query_text.lower()
        results = []
        if "server" in q or "app" in q or "main" in q or "index" in q:
            results = [
                RetrievalResult(chunk=server_chunk, score=0.90),
                RetrievalResult(chunk=app_chunk, score=0.90),
                RetrievalResult(chunk=react_chunk, score=0.90),
            ]
        return results

    code_chunk_mock.search_lexical.side_effect = mock_search_lexical

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_mock,
        code_chunk_repo=code_chunk_mock,
        embedding_provider=None,
    )

    provider = MockGenerationProvider(
        answer=(
            "SourceTrace verified that this is a full-stack FitSync repository.\n"
            "1. Read `backend/server.js` [E1]: Server listener.\n"
            "2. Read `backend/app.js` [E2]: Express application configuration.\n"
            "3. Read `client/src/App.jsx` [E3]: React main UI component.\n"
            "Next action: Inspect `backend/server.js` [E1]."
        )
    )

    answer_svc = GroundedAnswerService(
        retrieval_service=retrieval_svc,
        generation_provider=provider,
    )

    result = answer_svc.generate_answer(
        owner_session_id="sess_test_owner",
        repository_id="repo_test_fitsync",
        question="What should I read first?",
    )

    assert result.answer_mode == "orientation"
    assert not result.insufficient_evidence
    assert len(result.citations) == 3
    citation_paths = [c.relative_path for c in result.citations]
    assert "backend/server.js" in citation_paths
    assert "backend/app.js" in citation_paths
    assert "client/src/App.jsx" in citation_paths


def test_orientation_query_with_only_unrelated_files_returns_insufficient_orientation():
    """4. Orientation query with only unrelated files returns truthful insufficient orientation."""
    repo_record = RepositoryRecord(
        repository_id="repo_test_01",
        owner_session_id="sess_test_owner",
        name="TestRepo",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id="job_ref_01",
    )
    repo_mock = MagicMock()
    repo_mock.get_by_id.return_value = repo_record

    unrelated_chunk = create_dummy_chunk(
        relative_path="utils/math.py",
        symbol_name="add",
        symbol_type="function",
        content="def add(a, b): return a + b\n",
    )

    code_chunk_mock = MagicMock()
    code_chunk_mock.list_by_repository.return_value = [unrelated_chunk]
    code_chunk_mock.search_lexical.return_value = [
        RetrievalResult(chunk=unrelated_chunk, score=0.30)
    ]

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_mock,
        code_chunk_repo=code_chunk_mock,
        embedding_provider=None,
    )

    provider = MockGenerationProvider(answer="Read math.py [E1].")

    answer_svc = GroundedAnswerService(
        retrieval_service=retrieval_svc,
        generation_provider=provider,
    )

    result = answer_svc.generate_answer(
        owner_session_id="sess_test_owner",
        repository_id="repo_test_01",
        question="What should I read first?",
    )

    assert result.insufficient_evidence
    assert result.answer_mode == "insufficient_orientation"
    assert len(result.citations) == 0
    exp_orient_msg = "SourceTrace could not verify a clear starting path from indexed source files."
    assert result.answer == exp_orient_msg


def test_owner_repository_active_generation_isolation():
    """5. Verify retrieval operations strictly scope queries to owner/repo/gen."""
    repo_record = RepositoryRecord(
        repository_id="repo_target",
        owner_session_id="sess_target_owner",
        name="TargetRepo",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id="job_ref_target",
    )
    repo_mock = MagicMock()
    repo_mock.get_by_id.return_value = repo_record

    code_chunk_mock = MagicMock()
    code_chunk_mock.list_by_repository.return_value = []
    code_chunk_mock.search_lexical.return_value = []

    retrieval_svc = SemanticRetrievalService(
        repository_repo=repo_mock,
        code_chunk_repo=code_chunk_mock,
        embedding_provider=None,
    )

    retrieval_svc.retrieve(
        owner_session_id="sess_target_owner",
        repository_id="repo_target",
        query="test query",
    )

    repo_mock.get_by_id.assert_called_with("sess_target_owner", "repo_target")
