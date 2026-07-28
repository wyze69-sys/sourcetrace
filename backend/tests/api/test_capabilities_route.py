"""Offline API route unit tests for GET /api/v1/capabilities."""

from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.core.config import Settings, get_settings


def test_capabilities_endpoint_no_keys():
    """Verify capabilities route when no AI API keys are configured."""
    with patch.dict(
        "os.environ",
        {
            "SOURCETRACE_GEMINI_API_KEY": "",
            "SOURCETRACE_EMBEDDING_API_KEY": "",
            "SOURCETRACE_LLM_API_KEY": "",
        },
        clear=True,
    ):
        test_settings = Settings(_env_file=None)

        app = create_app()
        app.dependency_overrides[get_settings] = lambda: test_settings

        client = TestClient(app)
        response = client.get("/api/v1/capabilities")

        assert response.status_code == 200
        data = response.json()
        assert data["allowed_index_modes"] == ["static"]
        assert data["default_index_mode"] == "static"
        assert data["lexical_search_available"] is True
        assert data["semantic_search_available"] is False
        assert data["generation_available"] is False


def test_capabilities_endpoint_gemini_configured():
    """Verify capabilities route when Gemini API key is configured."""
    test_settings = Settings(
        gemini_api_key=SecretStr("AIzaSyFakeGeminiKey123"),
        embedding_api_key=None,
        llm_api_key=None,
        embedding_provider="gemini",
        llm_provider="gemini",
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings

    client = TestClient(app)
    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    data = response.json()
    assert "static" in data["allowed_index_modes"]
    assert "cloud_ai" in data["allowed_index_modes"]
    assert data["lexical_search_available"] is True
    assert data["semantic_search_available"] is True
    assert data["generation_available"] is True
    # Verify no secret values exposed
    assert "AIzaSyFakeGeminiKey123" not in response.text


def test_capabilities_endpoint_openai_configured():
    """Verify capabilities route when OpenAI-compatible API key is configured."""
    test_settings = Settings(
        gemini_api_key=None,
        embedding_api_key=SecretStr("sk-fake-embedding-key"),
        llm_api_key=SecretStr("sk-fake-llm-key"),
        embedding_provider="openai",
        llm_provider="openai",
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings

    client = TestClient(app)
    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    data = response.json()
    assert "cloud_ai" in data["allowed_index_modes"]
    assert data["semantic_search_available"] is True
    assert data["generation_available"] is True
    assert "sk-fake" not in response.text


def test_capabilities_endpoint_openrouter_llm_only():
    """Verify capabilities route when OpenRouter/OpenAI LLM key is set without embedding key."""
    test_settings = Settings(
        gemini_api_key=None,
        embedding_api_key=None,
        llm_api_key=SecretStr("sk-or-v1-fake-openrouter-key"),
        llm_provider="openai",
        llm_base_url="https://openrouter.ai/api/v1",
        embedding_provider="gemini",
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings

    client = TestClient(app)
    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    data = response.json()
    assert data["allowed_index_modes"] == ["static"]
    assert data["semantic_search_available"] is False
    assert data["generation_available"] is True
    # Verify no secret or internal provider data is exposed
    assert "sk-or-v1" not in response.text
    assert "openrouter.ai" not in response.text
    assert "openai" not in response.text
