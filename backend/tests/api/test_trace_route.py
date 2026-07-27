"""Offline API route tests for POST /api/v1/repositories/{id}/trace (TRACE-003)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_code_chunk_repository,
    get_current_owner_id,
    get_repository_repository,
)
from sourcetrace.models.domain import (
    CodeChunk,
    ReferenceEvidence,
    RepositoryRecord,
    RetrievalResult,
)

_OWNER = "owner_trace_route"
_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _repo_record(status: str = "ready") -> RepositoryRecord:
    return RepositoryRecord(
        repository_id="repo_t3",
        owner_session_id=_OWNER,
        name="trace-repo",
        source_type="github",
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
        index_mode="static",
    )


def _chunk(chunk_id: str, symbol_name: str, references=()) -> CodeChunk:
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id="repo_t3",
        owner_session_id=_OWNER,
        relative_path="src/app.py",
        language="python",
        symbol_name=symbol_name,
        symbol_type="function",
        start_line=1,
        end_line=8,
        content=f"def {symbol_name}(): ...",
        content_hash=f"hash_{chunk_id}",
        parser_version="python-ast-v2",
        created_at=_NOW,
        references=tuple(references),
    )


def _app(repo_record=None, chunks=None, lexical_hits=None, authed: bool = True):
    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = repo_record

    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = list(chunks or [])
    mock_chunk_repo.search_lexical.return_value = [
        RetrievalResult(chunk=c, score=1.0) for c in (lexical_hits or [])
    ]

    app = create_app()
    if authed:
        app.dependency_overrides[get_current_owner_id] = lambda: _OWNER
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo
    return app


def test_trace_requires_bearer_authentication() -> None:
    client = TestClient(_app(authed=False))
    response = client.post("/api/v1/repositories/repo_t3/trace", json={"entry": "main"})
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_trace_missing_or_cross_owner_repository_returns_uniform_404() -> None:
    # get_by_id is owner-scoped: a repo owned by another session resolves to
    # None exactly like a nonexistent one, so both produce the same 404.
    client = TestClient(_app(repo_record=None))
    response = client.post(
        "/api/v1/repositories/repo_of_someone_else/trace", json={"entry": "main"}
    )
    assert response.status_code == 404


def test_trace_not_ready_repository_returns_400() -> None:
    client = TestClient(_app(repo_record=_repo_record(status="processing")))
    response = client.post("/api/v1/repositories/repo_t3/trace", json={"entry": "main"})
    assert response.status_code == 400


def test_trace_explain_mode_without_llm_capability_returns_422() -> None:
    from sourcetrace.core.config import Settings, get_settings

    app = _app(repo_record=_repo_record())
    # Explicit keyless settings: generation must be unavailable regardless of
    # any developer-local .env contents.
    app.dependency_overrides[get_settings] = lambda: Settings(
        llm_provider="gemini",
        gemini_api_key=None,
        llm_api_key=None,
        embedding_api_key=None,
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_t3/trace",
        json={"entry": "main", "mode": "explain"},
    )
    assert response.status_code == 422


class _FakeExplainer:
    def __init__(self, outcome):
        self._outcome = outcome
        self.received = []

    def explain(self, result):
        self.received.append(result)
        return self._outcome


def _explain_app(outcome, chunks, lexical_hits):
    from sourcetrace.api.dependencies import get_trace_explainer_factory
    from sourcetrace.core.config import Settings, get_settings

    app = _app(repo_record=_repo_record(), chunks=chunks, lexical_hits=lexical_hits)
    explainer = _FakeExplainer(outcome)
    app.dependency_overrides[get_trace_explainer_factory] = lambda: lambda: explainer
    app.dependency_overrides[get_settings] = lambda: Settings(
        llm_provider="gemini", gemini_api_key="test-gemini-key"
    )
    return app, explainer


def test_trace_explain_mode_returns_marker_validated_explanation() -> None:
    main = _chunk("c_main", "main", references=[ReferenceEvidence("helper", "call", 4, 4)])
    helper = _chunk("c_helper", "helper")
    app, explainer = _explain_app(
        ("The flow starts at [S1] and calls [S2].", (1, 2)), [main, helper], [main]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_t3/trace",
        json={"entry": "main", "mode": "explain"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["explanation"] == {
        "text": "The flow starts at [S1] and calls [S2].",
        "cited_steps": [1, 2],
    }
    # The static trace itself is unchanged by explanation.
    assert data["steps"] == ["c_main", "c_helper"]
    assert [g["kind"] for g in data["gaps"]] == []
    assert len(explainer.received) == 1


def test_trace_explain_failure_degrades_to_static_trace_with_gap() -> None:
    main = _chunk("c_main", "main", references=[ReferenceEvidence("helper", "call", 4, 4)])
    helper = _chunk("c_helper", "helper")
    app, _ = _explain_app(None, [main, helper], [main])
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_t3/trace",
        json={"entry": "main", "mode": "explain"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["explanation"] is None
    assert data["steps"] == ["c_main", "c_helper"]
    assert [g["kind"] for g in data["gaps"]] == ["explanation_failed"]


def test_trace_static_mode_never_touches_the_explainer() -> None:
    main = _chunk("c_main", "main")
    app, explainer = _explain_app(("[S1]", (1,)), [main], [main])
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_t3/trace", json={"entry": "main", "mode": "static"}
    )

    assert response.status_code == 200
    assert response.json()["explanation"] is None
    assert explainer.received == []


def test_trace_success_returns_nodes_edges_steps_and_null_explanation() -> None:
    main = _chunk("c_main", "main", references=[ReferenceEvidence("helper", "call", 4, 4)])
    helper = _chunk("c_helper", "helper")
    client = TestClient(
        _app(repo_record=_repo_record(), chunks=[main, helper], lexical_hits=[main])
    )

    response = client.post("/api/v1/repositories/repo_t3/trace", json={"entry": "main"})

    assert response.status_code == 200
    data = response.json()
    assert data["repository_id"] == "repo_t3"
    assert data["entry"]["resolved_node_id"] == "c_main"
    assert [n["node_id"] for n in data["nodes"]] == ["c_main", "c_helper"]
    assert data["steps"] == ["c_main", "c_helper"]
    assert len(data["edges"]) == 1
    edge = data["edges"][0]
    assert edge["from_node_id"] == "c_main"
    assert edge["to_node_id"] == "c_helper"
    assert edge["kind"] == "call"
    assert edge["evidence_line_start"] == 4
    assert data["explanation"] is None


def test_trace_owned_ready_repo_with_no_match_returns_200_entry_unresolved() -> None:
    client = TestClient(
        _app(repo_record=_repo_record(), chunks=[_chunk("c_x", "unrelated")], lexical_hits=[])
    )

    response = client.post("/api/v1/repositories/repo_t3/trace", json={"entry": "does_not_exist"})

    assert response.status_code == 200
    data = response.json()
    assert data["entry"]["resolved_node_id"] is None
    assert data["nodes"] == []
    assert data["edges"] == []
    assert [g["kind"] for g in data["gaps"]] == ["entry_unresolved"]


def test_trace_static_mode_makes_no_llm_or_provider_calls() -> None:
    main = _chunk("c_main", "main")
    mock_repo_repo = MagicMock()
    mock_repo_repo.get_by_id.return_value = _repo_record()
    mock_chunk_repo = MagicMock()
    mock_chunk_repo.list_by_repository.return_value = [main]
    mock_chunk_repo.search_lexical.return_value = [RetrievalResult(chunk=main, score=1.0)]

    app = create_app()
    app.dependency_overrides[get_current_owner_id] = lambda: _OWNER
    app.dependency_overrides[get_repository_repository] = lambda: mock_repo_repo
    app.dependency_overrides[get_code_chunk_repository] = lambda: mock_chunk_repo
    client = TestClient(app)

    response = client.post("/api/v1/repositories/repo_t3/trace", json={"entry": "main"})

    assert response.status_code == 200
    # The chunk repository is the ONLY collaborator, and only its two
    # read methods are used — no vector search, no embedding, no generation.
    called = {c[0] for c in mock_chunk_repo.method_calls}
    assert called == {"list_by_repository", "search_lexical"}
    assert not mock_chunk_repo.search_vectors.called
