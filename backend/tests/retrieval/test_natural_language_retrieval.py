"""Focus integration tests verifying natural-language question retrieval and grounding."""

from datetime import UTC, datetime
from typing import Any

from sourcetrace.generation.client import GenerationMessage
from sourcetrace.generation.service import (
    INSUFFICIENT_EVIDENCE_ANSWER,
    GroundedAnswerService,
)
from sourcetrace.models.domain import (
    CodeChunk,
    RepositoryRecord,
    RetrievalResult,
)
from sourcetrace.retrieval.service import SemanticRetrievalService


class InMemoryFixtureChunkRepository:
    """Production-structured in-memory code chunk repository for offline testing."""

    def __init__(self, chunks: list[CodeChunk]) -> None:
        self.chunks = list(chunks)
        self.search_calls: list[dict[str, Any]] = []

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]:
        from sourcetrace.storage.mongo_repositories import (
            _ENGLISH_STOP_WORDS,
            tokenize_identifier,
        )

        self.search_calls.append(
            {
                "owner_session_id": owner_session_id,
                "repository_id": repository_id,
                "query_text": query_text,
                "limit": limit,
                "generation_id": generation_id,
            }
        )

        clean_query = query_text.strip()
        if not clean_query:
            return []

        raw_tokens = tokenize_identifier(clean_query)
        search_tokens = [t for t in raw_tokens if t.lower() not in _ENGLISH_STOP_WORDS]
        if not search_tokens:
            search_tokens = raw_tokens

        query_set = set(search_tokens)
        results: list[RetrievalResult] = []

        for chunk in self.chunks:
            if chunk.owner_session_id != owner_session_id or chunk.repository_id != repository_id:
                continue
            if generation_id is not None and chunk.generation_id != generation_id:
                continue

            sym_tokens = set(tokenize_identifier(chunk.symbol_name))
            path_tokens = set(tokenize_identifier(chunk.relative_path))
            content_tokens = set(tokenize_identifier(chunk.content[:200]))
            search_terms = set(chunk.search_terms or ()) | sym_tokens | path_tokens

            score = 0.0
            chunk_sym_norm = (chunk.symbol_name_normalized or "").lower()
            chunk_path_norm = (chunk.relative_path_normalized or "").lower()
            q_clean = clean_query.lower()

            is_exact_sym = q_clean == chunk_sym_norm or q_clean == chunk.symbol_name.lower()
            is_exact_path = q_clean == chunk_path_norm or q_clean == chunk.relative_path.lower()

            if chunk_sym_norm and is_exact_sym:
                score = 1.0
            elif chunk_path_norm and is_exact_path:
                score = 0.95
            elif query_set and query_set.issubset(sym_tokens):
                score = 0.85
            elif query_set and query_set.issubset(path_tokens):
                score = 0.80
            elif query_set and query_set.issubset(search_terms):
                score = 0.75
            elif query_set and sym_tokens.intersection(query_set):
                score = 0.65
            elif query_set and path_tokens.intersection(query_set):
                score = 0.55
            elif query_set and (
                search_terms.intersection(query_set) or content_tokens.intersection(query_set)
            ):
                score = 0.45

            if score > 0.0:
                results.append(RetrievalResult(chunk=chunk, score=score))

        results.sort(
            key=lambda r: (
                -r.score,
                r.chunk.relative_path,
                r.chunk.start_line,
                r.chunk.chunk_id,
            )
        )
        return results[:limit]


class InMemoryFixtureRepositoryRepository:
    """Fake repository metadata storage for readiness validation."""

    def __init__(self, repo_record: RepositoryRecord) -> None:
        self.repo_record = repo_record

    def get_by_id(self, owner_session_id: str, repository_id: str) -> RepositoryRecord | None:
        if (
            self.repo_record.owner_session_id == owner_session_id
            and self.repo_record.repository_id == repository_id
        ):
            return self.repo_record
        return None


class FakeGroundedLLMProvider:
    """Fake LLM provider generating citations based on prompt evidence."""

    def __init__(
        self,
        answer_template: str = "Based on retrieved evidence [E1], here is the answer.",
    ) -> None:
        self.answer_template = answer_template
        self.messages_received: list[tuple[GenerationMessage, ...]] = []

    @property
    def model_identifier(self) -> str:
        return "fake-grounded-llm"

    def generate(self, messages: Any) -> str:
        self.messages_received.append(tuple(messages))
        return self.answer_template


def _build_sample_codebase_chunks(
    owner_session_id: str = "user_sess_100",
    repository_id: str = "repo_app_200",
    generation_id: str = "gen_v1",
) -> list[CodeChunk]:
    now = datetime.now(UTC)
    main_code = "def main():\n    app = create_app()\n    app.run(port=8000)\n"
    auth_code = (
        "def login_user(username, password):\n"
        "    user = authenticate(username, password)\n"
        "    return issue_jwt_token(user)\n"
    )
    route_code = (
        "router = APIRouter(prefix='/api/v1')\n\n"
        "@router.get('/health')\n"
        "def health():\n"
        "    return {'status': 'ok'}\n"
    )
    config_code = (
        "class Settings(BaseSettings):\n"
        "    env_name: str = 'production'\n"
        "    mongo_uri: str = 'mongodb://localhost:27017'\n"
    )
    db_code = (
        "class MongoStorageManager:\n"
        "    def connect(self):\n"
        "        self.client = MongoClient(self.settings.mongo_uri)\n"
    )
    # Extra chunk to produce a weak partial match for the word "start"
    random_start_code = "def start_background_timer():\n    print('Timer started')\n"

    return [
        CodeChunk(
            chunk_id="chunk_main",
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            relative_path="src/main.py",
            language="python",
            symbol_name="main",
            symbol_type="function",
            start_line=1,
            end_line=15,
            content=main_code,
            content_hash="h_main",
            parser_version="1.0",
            created_at=now,
            generation_id=generation_id,
            symbol_name_normalized="main",
            relative_path_normalized="src main py",
            search_terms=("main", "app", "server", "create_app", "run", "start"),
        ),
        CodeChunk(
            chunk_id="chunk_auth",
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            relative_path="src/security/auth.py",
            language="python",
            symbol_name="login_user",
            symbol_type="function",
            start_line=10,
            end_line=35,
            content=auth_code,
            content_hash="h_auth",
            parser_version="1.0",
            created_at=now,
            generation_id=generation_id,
            symbol_name_normalized="login user",
            relative_path_normalized="src security auth py",
            search_terms=("login", "user", "auth", "security", "jwt", "token", "password"),
        ),
        CodeChunk(
            chunk_id="chunk_routes",
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            relative_path="src/api/routes.py",
            language="python",
            symbol_name="router",
            symbol_type="variable",
            start_line=1,
            end_line=25,
            content=route_code,
            content_hash="h_routes",
            parser_version="1.0",
            created_at=now,
            generation_id=generation_id,
            symbol_name_normalized="router",
            relative_path_normalized="src api routes py",
            search_terms=("router", "route", "routes", "api", "endpoint", "health"),
        ),
        CodeChunk(
            chunk_id="chunk_config",
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            relative_path="src/core/config.py",
            language="python",
            symbol_name="Settings",
            symbol_type="class",
            start_line=5,
            end_line=30,
            content=config_code,
            content_hash="h_config",
            parser_version="1.0",
            created_at=now,
            generation_id=generation_id,
            symbol_name_normalized="settings",
            relative_path_normalized="src core config py",
            search_terms=("settings", "config", "configuration", "env", "environment"),
        ),
        CodeChunk(
            chunk_id="chunk_db",
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            relative_path="src/storage/database.py",
            language="python",
            symbol_name="MongoStorageManager",
            symbol_type="class",
            start_line=1,
            end_line=40,
            content=db_code,
            content_hash="h_db",
            parser_version="1.0",
            created_at=now,
            generation_id=generation_id,
            symbol_name_normalized="mongo storage manager",
            relative_path_normalized="src storage database py",
            search_terms=("mongo", "storage", "manager", "database", "db", "connection", "connect"),
        ),
        CodeChunk(
            chunk_id="chunk_random_start",
            repository_id=repository_id,
            owner_session_id=owner_session_id,
            relative_path="src/utils/timer.py",
            language="python",
            symbol_name="start_background_timer",
            symbol_type="function",
            start_line=1,
            end_line=10,
            content=random_start_code,
            content_hash="h_timer",
            parser_version="1.0",
            created_at=now,
            generation_id=generation_id,
            symbol_name_normalized="start background timer",
            relative_path_normalized="src utils timer py",
            search_terms=("start", "background", "timer"),
        ),
    ]


def test_natural_language_questions_retrieve_real_fixture_chunks() -> None:
    """Verify natural-language questions retrieve real fixture chunks via retrieval service."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunk_repo = InMemoryFixtureChunkRepository(chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    # 1. Question: "Where does the application start?"
    res_start = service.retrieve(owner_id, repo_id, "Where does the application start?")
    assert res_start.total_retrieved >= 1
    paths_start = [item.citation.relative_path for item in res_start.items]
    assert "src/main.py" in paths_start

    # 2. Question: "How does login work?"
    res_login = service.retrieve(owner_id, repo_id, "How does login work?")
    assert res_login.total_retrieved >= 1
    paths_login = [item.citation.relative_path for item in res_login.items]
    assert "src/security/auth.py" in paths_login

    # 3. Question: "Where are the API routes?"
    res_routes = service.retrieve(owner_id, repo_id, "Where are the API routes?")
    assert res_routes.total_retrieved >= 1
    paths_routes = [item.citation.relative_path for item in res_routes.items]
    assert "src/api/routes.py" in paths_routes

    # 4. Question: "How is configuration loaded?"
    res_config = service.retrieve(owner_id, repo_id, "How is configuration loaded?")
    assert res_config.total_retrieved >= 1
    paths_config = [item.citation.relative_path for item in res_config.items]
    assert "src/core/config.py" in paths_config

    # 5. Question: "How does the database connection work?"
    res_db = service.retrieve(owner_id, repo_id, "How does the database connection work?")
    assert res_db.total_retrieved >= 1
    paths_db = [item.citation.relative_path for item in res_db.items]
    assert "src/storage/database.py" in paths_db


def test_weak_direct_match_triggers_fallback_planner_and_reranks_evidence() -> None:
    """Regression test: Weak direct match triggers fallback planner and selects main.py."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunk_repo = InMemoryFixtureChunkRepository(chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    res = service.retrieve(owner_id, repo_id, "Where does the application start?")

    # 1. Assert search_calls recorded fallback queries ('main', 'create_app', 'server')
    searched_queries = [c["query_text"] for c in chunk_repo.search_calls]
    assert "Where does the application start?" in searched_queries  # Direct query
    assert len(searched_queries) > 1  # Fallback queries DID execute!
    assert any(q in searched_queries for q in ("main", "create_app", "server", "bootstrap"))

    # 2. Assert src/main.py is selected as rank-1 evidence
    assert res.total_retrieved >= 1
    assert res.items[0].citation.relative_path == "src/main.py"
    assert res.items[0].citation.symbol_name == "main"


def test_exact_symbol_retrieval_does_not_trigger_unnecessary_fallback() -> None:
    """Regression test: Exact symbol query returns rank 1 and skips fallback planner."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunk_repo = InMemoryFixtureChunkRepository(chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    res = service.retrieve(owner_id, repo_id, "login_user")

    # 1. Exact symbol query returns exact match as rank 1
    assert res.total_retrieved >= 1
    assert res.items[0].citation.symbol_name == "login_user"

    # 2. Fallback planner was NOT triggered (search_calls count == 1)
    assert len(chunk_repo.search_calls) == 1
    assert chunk_repo.search_calls[0]["query_text"] == "login_user"


def test_whole_token_matching_prevents_substring_false_positives() -> None:
    """Regression test: Upload question does NOT match 'app' startup intent via substring."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunk_repo = InMemoryFixtureChunkRepository(chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    phrases = service._derive_fallback_search_phrases("what happens during upload")

    # 'happens' and 'upload' contain 'app' as a substring, but set-intersection matching ignores it!
    assert "main" not in phrases
    assert "bootstrap" not in phrases
    assert "create_app" not in phrases


def test_grounded_answer_service_with_natural_language_question() -> None:
    """Verify GroundedAnswerService produces grounded citations for natural language questions."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunk_repo = InMemoryFixtureChunkRepository(chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    retrieval_service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    provider = FakeGroundedLLMProvider("Application entrypoint is defined in src/main.py [E1].")
    answer_service = GroundedAnswerService(retrieval_service, provider)

    result = answer_service.generate_answer(
        owner_session_id=owner_id,
        repository_id=repo_id,
        question="Where does the application start?",
    )

    assert result.insufficient_evidence is False
    assert "src/main.py [E1]" in result.answer
    assert len(result.citations) >= 1
    assert result.citations[0].relative_path == "src/main.py"
    assert result.citations[0].symbol_name == "main"


def test_separate_failure_causes_insufficient_retrieval_vs_missing_citations() -> None:
    """Test the 2 distinct causes for insufficient evidence: 0 items vs LLM omitting markers."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunk_repo = InMemoryFixtureChunkRepository(chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    retrieval_service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    # Cause 1: Question matches 0 chunks (empty evidence)
    provider1 = FakeGroundedLLMProvider()
    answer_service1 = GroundedAnswerService(retrieval_service, provider1)
    res1 = answer_service1.generate_answer(
        owner_session_id=owner_id,
        repository_id=repo_id,
        question="xyz123unmatched_question_string_random",
    )
    assert res1.insufficient_evidence is True
    assert res1.answer == INSUFFICIENT_EVIDENCE_ANSWER
    assert res1.citations == ()
    assert len(provider1.messages_received) == 0

    # Cause 2: Evidence IS retrieved (main.py), but LLM generates answer WITHOUT [E1] marker
    provider2 = FakeGroundedLLMProvider("The app starts in main.py without any citation marker.")
    answer_service2 = GroundedAnswerService(retrieval_service, provider2)
    res2 = answer_service2.generate_answer(
        owner_session_id=owner_id,
        repository_id=repo_id,
        question="Where does the application start?",
    )
    assert res2.insufficient_evidence is True
    assert res2.answer == INSUFFICIENT_EVIDENCE_ANSWER
    assert res2.citations == ()
    assert len(provider2.messages_received) == 1


def test_scope_isolation_in_query_planning_fallback() -> None:
    """Verify that fallback retrieval never leaks chunks from another session or generation."""
    now = datetime.now(UTC)
    other_session_chunk = CodeChunk(
        chunk_id="chunk_other_session",
        repository_id="repo_app_200",
        owner_session_id="other_session_999",
        relative_path="src/main.py",
        language="python",
        symbol_name="main",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def main(): pass",
        content_hash="h_other",
        parser_version="1.0",
        created_at=now,
        generation_id="gen_v1",
        search_terms=("main", "app", "server", "start"),
    )

    old_gen_chunk = CodeChunk(
        chunk_id="chunk_old_gen",
        repository_id="repo_app_200",
        owner_session_id="user_sess_100",
        relative_path="src/main.py",
        language="python",
        symbol_name="main",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def main(): pass",
        content_hash="h_old",
        parser_version="1.0",
        created_at=now,
        generation_id="gen_v0_old",
        search_terms=("main", "app", "server", "start"),
    )

    valid_chunk = CodeChunk(
        chunk_id="chunk_valid",
        repository_id="repo_app_200",
        owner_session_id="user_sess_100",
        relative_path="src/main.py",
        language="python",
        symbol_name="main",
        symbol_type="function",
        start_line=1,
        end_line=10,
        content="def main(): pass",
        content_hash="h_valid",
        parser_version="1.0",
        created_at=now,
        generation_id="gen_v1",
        search_terms=("main", "app", "server", "start"),
    )

    chunk_repo = InMemoryFixtureChunkRepository([other_session_chunk, old_gen_chunk, valid_chunk])
    repo_record = RepositoryRecord(
        repository_id="repo_app_200",
        owner_session_id="user_sess_100",
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id="gen_v1",
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    res = service.retrieve("user_sess_100", "repo_app_200", "Where does the application start?")
    assert res.total_retrieved == 1
    assert res.items[0].chunk_id == "chunk_valid"


def test_orientation_question_retrieves_readme_manifest_and_entrypoint() -> None:
    """Verify orientation question retrieves README, main.py, and config."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    now = datetime.now(UTC)
    readme_chunk = CodeChunk(
        chunk_id="chunk_readme",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="README.md",
        language="markdown",
        symbol_name="README",
        symbol_type="file",
        start_line=1,
        end_line=20,
        content="# Sample Application\nThis repository provides background services.",
        content_hash="h_readme",
        parser_version="1.0",
        created_at=now,
        generation_id=gen_id,
        search_terms=("readme", "docs", "sample", "application"),
    )

    unrelated_chunk = CodeChunk(
        chunk_id="chunk_unrelated",
        repository_id=repo_id,
        owner_session_id=owner_id,
        relative_path="api/mekong/station.ts",
        language="typescript",
        symbol_name="readStation",
        symbol_type="function",
        start_line=1,
        end_line=15,
        content="export function readStation() { return cacheFirst(); }",
        content_hash="h_mekong",
        parser_version="1.0",
        created_at=now,
        generation_id=gen_id,
        search_terms=("read", "station", "mekong"),
    )

    sample_chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunks = [readme_chunk, unrelated_chunk] + sample_chunks

    chunk_repo = InMemoryFixtureChunkRepository(chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    res = service.retrieve(owner_id, repo_id, "What should I read first?")

    paths = [item.citation.relative_path for item in res.items]
    assert "README.md" in paths
    assert "src/main.py" in paths
    assert "api/mekong/station.ts" not in paths


def test_orientation_question_with_no_evidence_returns_insufficient_orientation_state() -> None:
    """Verify orientation question on repo with 0 orientation evidence returns structured state."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    chunk_repo = InMemoryFixtureChunkRepository([])
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="empty-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    retrieval_service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )
    provider = FakeGroundedLLMProvider()
    answer_service = GroundedAnswerService(retrieval_service, provider)

    result = answer_service.generate_answer(
        owner_session_id=owner_id,
        repository_id=repo_id,
        question="What should I read first?",
    )

    assert result.insufficient_evidence is True
    assert result.answer_mode == "insufficient_orientation"
    assert "could not verify a clear starting path" in result.answer
    assert result.citations == ()
    assert result.evidence == ()


def test_orientation_provider_failure_returns_static_guidance_not_ai_answer_unavailable() -> None:
    """Verify LLM failure on orientation query returns static guidance reading path."""
    owner_id = "user_sess_100"
    repo_id = "repo_app_200"
    gen_id = "gen_v1"

    sample_chunks = _build_sample_codebase_chunks(owner_id, repo_id, gen_id)
    chunk_repo = InMemoryFixtureChunkRepository(sample_chunks)
    repo_record = RepositoryRecord(
        repository_id=repo_id,
        owner_session_id=owner_id,
        name="sample-app",
        source_type="github",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        active_generation_id=gen_id,
    )
    repo_repo = InMemoryFixtureRepositoryRepository(repo_record)

    retrieval_service = SemanticRetrievalService(
        repository_repo=repo_repo,
        code_chunk_repo=chunk_repo,
        embedding_provider=None,
    )

    class FailingProvider(FakeGroundedLLMProvider):
        def generate(self, messages: Any) -> str:
            raise RuntimeError("Provider connection error")

    answer_service = GroundedAnswerService(retrieval_service, FailingProvider())

    result = answer_service.generate_answer(
        owner_session_id=owner_id,
        repository_id=repo_id,
        question="What should I read first?",
    )

    assert result.insufficient_evidence is False
    assert result.answer_mode == "static_guidance"
    assert "AI answer unavailable" not in result.answer
    assert "Start here to explore this repository:" in result.answer
    assert len(result.citations) >= 1
