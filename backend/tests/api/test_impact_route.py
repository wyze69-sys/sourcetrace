"""Offline API route tests for POST /api/v1/repositories/{id}/impact (IMPACT-001)."""

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

_OWNER = "owner_impact_route"
_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _repo_record(status: str = "ready") -> RepositoryRecord:
    return RepositoryRecord(
        repository_id="repo_i1",
        owner_session_id=_OWNER,
        name="impact-repo",
        source_type="github",
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
        index_mode="static",
    )


def _chunk(chunk_id: str, symbol_name: str, references=()) -> CodeChunk:
    return CodeChunk(
        chunk_id=chunk_id,
        repository_id="repo_i1",
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
    return app, mock_chunk_repo


def test_impact_requires_bearer_authentication() -> None:
    app, _ = _app(authed=False)
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_i1/impact", json={"symbol": "compute"}
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_impact_missing_or_cross_owner_repository_returns_uniform_404() -> None:
    # get_by_id is owner-scoped: a repo owned by another session resolves to
    # None exactly like a nonexistent one, so both produce the same 404.
    app, _ = _app(repo_record=None)
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_of_someone_else/impact", json={"symbol": "compute"}
    )
    assert response.status_code == 404


def test_impact_not_ready_repository_returns_400() -> None:
    app, _ = _app(repo_record=_repo_record(status="processing"))
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_i1/impact", json={"symbol": "compute"}
    )
    assert response.status_code == 400


def test_impact_empty_symbol_is_rejected() -> None:
    app, _ = _app(repo_record=_repo_record())
    client = TestClient(app)
    response = client.post("/api/v1/repositories/repo_i1/impact", json={"symbol": ""})
    assert response.status_code == 422


def test_impact_success_returns_upstream_citations_and_risk() -> None:
    target = _chunk("c_target", "compute")
    caller = _chunk("c_caller", "handler", references=[ReferenceEvidence("compute", "call", 4, 4)])
    app, _ = _app(
        repo_record=_repo_record(), chunks=[target, caller], lexical_hits=[target]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact", json={"symbol": "compute"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["repository_id"] == "repo_i1"
    assert data["target"]["resolved_node_id"] == "c_target"
    assert [item["node_id"] for item in data["upstream"]] == ["c_caller"]
    item = data["upstream"][0]
    assert item["distance"] == 1
    assert item["evidence_node_id"] == "c_caller"
    assert item["evidence_label"] == "compute"
    assert item["evidence_line_start"] == 4
    assert data["downstream"] == []
    assert data["risk_level"] in ("low", "medium", "high")
    assert isinstance(data["risk_factors"], list)
    # No test file references the target in this fixture.
    kinds = {f["kind"] for f in data["risk_factors"]}
    assert "no_test_coverage" in kinds
    assert data["affected_endpoints"] == []
    assert data["affected_components"] == []
    assert data["affected_tests"] == []


def test_impact_unresolved_symbol_returns_200_with_unknown_risk() -> None:
    app, _ = _app(
        repo_record=_repo_record(), chunks=[_chunk("c_x", "unrelated")], lexical_hits=[]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact", json={"symbol": "does_not_exist"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["target"]["resolved_node_id"] is None
    assert data["upstream"] == [] and data["downstream"] == []
    assert data["risk_level"] == "unknown"
    assert [g["kind"] for g in data["gaps"]] == ["entry_unresolved"]


def test_impact_explain_mode_without_llm_capability_returns_422() -> None:
    from sourcetrace.core.config import Settings, get_settings

    app, _ = _app(repo_record=_repo_record())
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
        "/api/v1/repositories/repo_i1/impact",
        json={"symbol": "compute", "mode": "explain"},
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
    from sourcetrace.api.dependencies import get_impact_explainer_factory
    from sourcetrace.core.config import Settings, get_settings

    app, _ = _app(repo_record=_repo_record(), chunks=chunks, lexical_hits=lexical_hits)
    explainer = _FakeExplainer(outcome)
    app.dependency_overrides[get_impact_explainer_factory] = lambda: (lambda: explainer)
    app.dependency_overrides[get_settings] = lambda: Settings(
        llm_provider="gemini", gemini_api_key="test-gemini-key"
    )
    return app, explainer


def _impact_fixture():
    target = _chunk("c_target", "compute")
    caller = _chunk(
        "c_caller", "handler", references=[ReferenceEvidence("compute", "call", 4, 4)]
    )
    return target, caller


def test_impact_explain_mode_returns_marker_validated_explanation() -> None:
    target, caller = _impact_fixture()
    app, explainer = _explain_app(
        ("Changing this may break [S1].", (1,)), [target, caller], [target]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact",
        json={"symbol": "compute", "mode": "explain"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["explanation"] == {
        "text": "Changing this may break [S1].",
        "cited_steps": [1],
    }
    # The static preview itself is unchanged by explanation.
    assert [item["node_id"] for item in data["upstream"]] == ["c_caller"]
    assert "explanation_failed" not in {g["kind"] for g in data["gaps"]}
    assert len(explainer.received) == 1


def test_impact_explain_failure_degrades_to_static_preview_with_gap() -> None:
    target, caller = _impact_fixture()
    app, _ = _explain_app(None, [target, caller], [target])
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact",
        json={"symbol": "compute", "mode": "explain"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["explanation"] is None
    assert [item["node_id"] for item in data["upstream"]] == ["c_caller"]
    assert "explanation_failed" in {g["kind"] for g in data["gaps"]}


def test_impact_static_mode_never_touches_the_explainer() -> None:
    target, caller = _impact_fixture()
    app, explainer = _explain_app(("[S1]", (1,)), [target, caller], [target])
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact",
        json={"symbol": "compute", "mode": "static"},
    )

    assert response.status_code == 200
    assert response.json()["explanation"] is None
    assert explainer.received == []


def test_diff_impact_explain_mode_works_end_to_end() -> None:
    target, caller = _impact_fixture()
    app, explainer = _explain_app(
        ("The diff changes [S1] and [S2]; [S1] has a dependent.", (1, 2)),
        [target, caller],
        [target],
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact/diff",
        json={"diff": _VALID_DIFF, "mode": "explain"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["explanation"]["cited_steps"] == [1, 2]
    assert len(explainer.received) == 1


def test_diff_impact_explain_without_llm_capability_returns_422() -> None:
    from sourcetrace.core.config import Settings, get_settings

    app, _ = _app(repo_record=_repo_record())
    app.dependency_overrides[get_settings] = lambda: Settings(
        llm_provider="gemini",
        gemini_api_key=None,
        llm_api_key=None,
        embedding_api_key=None,
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_i1/impact/diff",
        json={"diff": _VALID_DIFF, "mode": "explain"},
    )
    assert response.status_code == 422


_VALID_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -2,1 +2,1 @@\n"
    "-old\n"
    "+new\n"
)


def test_diff_impact_requires_bearer_authentication() -> None:
    app, _ = _app(authed=False)
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_i1/impact/diff", json={"diff": _VALID_DIFF}
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_diff_impact_missing_or_cross_owner_repository_returns_uniform_404() -> None:
    app, _ = _app(repo_record=None)
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_of_someone_else/impact/diff",
        json={"diff": _VALID_DIFF},
    )
    assert response.status_code == 404


def test_diff_impact_not_ready_repository_returns_400() -> None:
    app, _ = _app(repo_record=_repo_record(status="processing"))
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_i1/impact/diff", json={"diff": _VALID_DIFF}
    )
    assert response.status_code == 400


def test_diff_impact_rejects_non_diff_input_with_422() -> None:
    app, _ = _app(repo_record=_repo_record(), chunks=[_chunk("c_x", "main")])
    client = TestClient(app)
    response = client.post(
        "/api/v1/repositories/repo_i1/impact/diff",
        json={"diff": "this is not a unified diff"},
    )
    # DiffParseError maps to the platform's standard 422 validation envelope
    # (details are masked by the global error handler by design).
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_diff_impact_success_returns_targets_and_aggregated_impact() -> None:
    target = _chunk("c_target", "compute")
    caller = _chunk(
        "c_caller", "handler", references=[ReferenceEvidence("compute", "call", 4, 4)]
    )
    app, mock_chunk_repo = _app(repo_record=_repo_record(), chunks=[target, caller])
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact/diff", json={"diff": _VALID_DIFF}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["repository_id"] == "repo_i1"
    assert [t["node_id"] for t in data["targets"]] == ["c_target", "c_caller"]
    assert all("changed_lines" in t for t in data["targets"])
    # Both chunks in src/app.py span line 2, so both are targets and the
    # caller's reference to compute is a seed-to-seed edge (excluded).
    assert data["upstream"] == []
    assert data["risk_level"] in ("low", "medium", "high", "unknown")
    # Zero-token: the diff preview needs only the chunk listing.
    called = {c[0] for c in mock_chunk_repo.method_calls}
    assert called == {"list_by_repository"}
    assert not mock_chunk_repo.search_vectors.called


def test_impact_makes_no_llm_or_provider_calls() -> None:
    target = _chunk("c_target", "compute")
    app, mock_chunk_repo = _app(
        repo_record=_repo_record(), chunks=[target], lexical_hits=[target]
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/repositories/repo_i1/impact", json={"symbol": "compute"}
    )

    assert response.status_code == 200
    # The chunk repository is the ONLY collaborator, and only its two
    # read methods are used — no vector search, no embedding, no generation.
    called = {c[0] for c in mock_chunk_repo.method_calls}
    assert called == {"list_by_repository", "search_lexical"}
    assert not mock_chunk_repo.search_vectors.called
