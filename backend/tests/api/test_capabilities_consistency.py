"""Comprehensive capability and provider readiness consistency unit tests."""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import get_session_repository
from sourcetrace.core.capabilities import evaluate_capabilities
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.models.domain import AnonymousSession


def test_caps_static_with_no_keys():
    """Default configuration with no keys yields static index mode only."""
    settings = Settings(
        gemini_api_key=None,
        embedding_api_key=None,
        llm_api_key=None,
        embedding_provider="gemini",
    )
    caps = evaluate_capabilities(settings)
    assert caps.allowed_index_modes == ["static"]
    assert caps.default_index_mode == "static"
    assert caps.semantic_search_available is False
    assert caps.generation_available is False


def test_caps_gemini_provider_with_gemini_key():
    """Gemini embedding provider with Gemini key enables cloud_ai and semantic search."""
    settings = Settings(
        gemini_api_key=SecretStr("AIzaSyFakeGeminiKey123"),
        embedding_api_key=None,
        llm_api_key=None,
        embedding_provider="gemini",
        llm_provider="gemini",
    )
    caps = evaluate_capabilities(settings)
    assert caps.allowed_index_modes == ["static", "cloud_ai"]
    assert caps.semantic_search_available is True
    assert caps.generation_available is True


def test_caps_gemini_provider_with_only_unrelated_openai_key():
    """Gemini embedding provider with only OpenAI keys does NOT enable semantic search."""
    settings = Settings(
        gemini_api_key=None,
        embedding_api_key=SecretStr("sk-proj-FakeOpenAIEmbKey"),
        llm_api_key=SecretStr("sk-proj-FakeOpenAILLMKey"),
        embedding_provider="gemini",
    )
    caps = evaluate_capabilities(settings)
    assert caps.allowed_index_modes == ["static"]
    assert caps.semantic_search_available is False


def test_caps_openai_provider_with_valid_key():
    """OpenAI embedding provider with valid embedding_api_key enables semantic search."""
    settings = Settings(
        gemini_api_key=None,
        embedding_api_key=SecretStr("sk-proj-FakeOpenAIEmbKey"),
        llm_api_key=None,
        embedding_provider="openai",
    )
    caps = evaluate_capabilities(settings)
    assert caps.allowed_index_modes == ["static", "cloud_ai"]
    assert caps.semantic_search_available is True


def test_caps_openai_provider_with_only_unrelated_gemini_key():
    """OpenAI embedding provider with only Gemini key does NOT enable semantic search."""
    settings = Settings(
        gemini_api_key=SecretStr("AIzaSyFakeGeminiKey123"),
        embedding_api_key=None,
        llm_api_key=None,
        embedding_provider="openai",
    )
    caps = evaluate_capabilities(settings)
    assert caps.allowed_index_modes == ["static"]
    assert caps.semantic_search_available is False


def test_caps_unsupported_provider():
    """Unsupported embedding provider disables cloud_ai and semantic search."""
    settings = Settings(
        gemini_api_key=SecretStr("AIzaSyFakeGeminiKey123"),
        embedding_api_key=SecretStr("sk-proj-FakeOpenAIEmbKey"),
        embedding_provider="unsupported_provider",
    )
    caps = evaluate_capabilities(settings)
    assert caps.allowed_index_modes == ["static"]
    assert caps.semantic_search_available is False


def test_caps_operator_disables_cloud_ai():
    """Operator removing cloud_ai from allowed_index_modes restricts output."""
    settings = Settings(
        gemini_api_key=SecretStr("AIzaSyFakeGeminiKey123"),
        embedding_provider="gemini",
        allowed_index_modes=("static",),
    )
    caps = evaluate_capabilities(settings)
    assert caps.allowed_index_modes == ["static"]


def test_route_dependency_override_and_agreement():
    """Capabilities route and POST /repositories agree exactly under dependency override."""
    custom_settings = Settings(
        gemini_api_key=None,
        embedding_api_key=None,
        embedding_provider="gemini",
        allowed_index_modes=("static", "cloud_ai"),
        session_signing_secret=SecretStr(
            "test-only-session-signing-secret-not-for-production"
        ),
    )

    session_repo = MagicMock()
    session_repo.get_by_id.return_value = None
    session_repo.save.side_effect = lambda session: session

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: custom_settings
    app.dependency_overrides[get_session_repository] = lambda: session_repo

    test_client = TestClient(app)

    # 1. Capabilities route
    cap_resp = test_client.get("/api/v1/capabilities")
    assert cap_resp.status_code == 200
    caps_data = cap_resp.json()
    assert caps_data["allowed_index_modes"] == ["static"]
    assert caps_data["semantic_search_available"] is False

    # 2. Repository creation route rejecting cloud_ai with HTTP 422
    repo_resp = test_client.post(
        "/api/v1/repositories",
        json={
            "github_url": "https://github.com/wyze69-sys/FitSync",
            "index_mode": "cloud_ai",
        },
    )
    assert repo_resp.status_code == 422
    assert repo_resp.json()["error"]["code"] == "VALIDATION_ERROR"

    session_repo.save.assert_called_once()
    saved_session = session_repo.save.call_args.args[0]
    assert isinstance(saved_session, AnonymousSession)


def test_no_secret_leakage():
    """Capabilities evaluation and responses never leak API key strings."""
    secret_str = "AIzaSySUPER_SECRET_KEY_9999"
    settings = Settings(
        gemini_api_key=SecretStr(secret_str),
        embedding_provider="gemini",
    )
    caps = evaluate_capabilities(settings)
    caps_repr = repr(caps)
    assert secret_str not in caps_repr
