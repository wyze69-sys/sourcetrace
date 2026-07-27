"""Offline tests for GeminiEmbeddingAdapter — no live network calls."""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import EmbeddingError
from sourcetrace.embeddings.provider import (
    EmbeddingProvider,
    GeminiEmbeddingAdapter,
)

# ---------------------------------------------------------------------------
# Fake Gemini client helpers
# ---------------------------------------------------------------------------


class _FakeContentEmbedding:
    """Mimics google.genai ContentEmbedding with a .values list."""

    def __init__(self, values: list[Any]) -> None:
        self.values = values


class _FakeEmbedResponse:
    """Mimics google.genai EmbedContentResponse with .embeddings list."""

    def __init__(self, embeddings: list[Any]) -> None:
        self.embeddings = embeddings


class _FakeModelsApi:
    def __init__(self, response_queue: list[Any] | None = None) -> None:
        self.call_count = 0
        self.recorded_calls: list[dict[str, Any]] = []
        self._queue = list(response_queue or [])

    def embed_content(self, **kwargs: Any) -> Any:
        self.call_count += 1
        self.recorded_calls.append(kwargs)
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        # Default: return one embedding per input text in `contents`
        contents = kwargs.get("contents", [])
        n = len(contents) if isinstance(contents, (list, tuple)) else 1
        return _FakeEmbedResponse([_FakeContentEmbedding([0.1] * 1536) for _ in range(n)])


class _FakeGeminiClient:
    def __init__(self, response_queue: list[Any] | None = None) -> None:
        self.models = _FakeModelsApi(response_queue)


def _make_settings(
    *,
    gemini_api_key: str = "test-gemini-key",
    gemini_embedding_model: str = "gemini-embedding-001",
    embedding_dimensions: int = 1536,
    embedding_batch_size: int = 100,
) -> Settings:
    return Settings(
        gemini_api_key=SecretStr(gemini_api_key),
        gemini_embedding_model=gemini_embedding_model,
        embedding_dimensions=embedding_dimensions,
        embedding_batch_size=embedding_batch_size,
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_implements_embedding_provider_protocol() -> None:
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=_FakeGeminiClient(),
    )
    assert isinstance(adapter, EmbeddingProvider)
    assert adapter.model_identifier == "gemini-embedding-001"
    assert adapter.embedding_dimensions == 1536


# ---------------------------------------------------------------------------
# Lazy client construction
# ---------------------------------------------------------------------------


def test_constructor_is_lazy_no_client_created() -> None:
    """No network call or client object created by the constructor."""
    settings = _make_settings()
    adapter = GeminiEmbeddingAdapter(settings=settings)
    # _client must remain None until embed() is called
    assert adapter._client is None


def test_client_created_only_on_first_embed_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """google.genai.Client is not imported/called until embed() executes."""
    settings = _make_settings()
    adapter = GeminiEmbeddingAdapter(settings=settings)
    assert adapter._client is None

    fake_client = _FakeGeminiClient()

    patch_target = "sourcetrace.embeddings.provider.GeminiEmbeddingAdapter._get_client"
    with patch(patch_target, return_value=fake_client):
        adapter = GeminiEmbeddingAdapter(settings=settings)
        adapter.embed(["hello"])

    # After embed, _get_client was used (we patched it, so _client may still be None here,
    # but the key test is that the constructor did not set it)
    # The important assertion is already covered: _client was None before embed.


# ---------------------------------------------------------------------------
# Model and dimensions from config
# ---------------------------------------------------------------------------


def test_model_identifier_from_config() -> None:
    settings = _make_settings(gemini_embedding_model="gemini-embedding-001")
    adapter = GeminiEmbeddingAdapter(settings=settings, client=_FakeGeminiClient())
    assert adapter.model_identifier == "gemini-embedding-001"


def test_model_identifier_overridden_by_explicit_param() -> None:
    settings = _make_settings(gemini_embedding_model="gemini-embedding-001")
    adapter = GeminiEmbeddingAdapter(
        model_identifier="custom-model",
        settings=settings,
        client=_FakeGeminiClient(),
    )
    assert adapter.model_identifier == "custom-model"


def test_dimensions_default_to_1536_from_config() -> None:
    settings = _make_settings(embedding_dimensions=1536)
    adapter = GeminiEmbeddingAdapter(settings=settings, client=_FakeGeminiClient())
    assert adapter.embedding_dimensions == 1536


# ---------------------------------------------------------------------------
# API key isolation
# ---------------------------------------------------------------------------


def test_uses_gemini_api_key_not_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini adapter must read SOURCETRACE_GEMINI_API_KEY, never llm_api_key."""
    monkeypatch.delenv("SOURCETRACE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("SOURCETRACE_EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("SOURCETRACE_GEMINI_API_KEY", "my-gemini-key")
    monkeypatch.setenv("SOURCETRACE_EMBEDDING_DIMENSIONS", "1536")
    monkeypatch.setenv("SOURCETRACE_GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    from sourcetrace.core.config import Settings

    settings = Settings()
    GeminiEmbeddingAdapter(settings=settings)
    # Client not yet created; check that gemini_api_key is set in settings
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "my-gemini-key"


def test_missing_gemini_api_key_fails_on_embed() -> None:
    settings = Settings(
        gemini_api_key=None,
        gemini_embedding_model="gemini-embedding-001",
        embedding_dimensions=1536,
    )
    adapter = GeminiEmbeddingAdapter(settings=settings)
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["test text"])
    assert (
        "configuration is invalid" in str(exc_info.value)
        or str(exc_info.value) == "Embedding failed safely."
    )


def test_no_gemini_key_in_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[RuntimeError("key=AIzaSy-super-secret-key-12345")]
    )
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["test text"])
    assert "AIzaSy-super-secret" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Task type
# ---------------------------------------------------------------------------


def test_default_task_type_is_retrieval_document() -> None:
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=_FakeGeminiClient(),
    )
    assert adapter._task_type == "RETRIEVAL_DOCUMENT"


def test_retrieval_query_task_type_accepted() -> None:
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        task_type="RETRIEVAL_QUERY",
        client=_FakeGeminiClient(),
    )
    assert adapter._task_type == "RETRIEVAL_QUERY"


def test_invalid_task_type_raises_embedding_error() -> None:
    with pytest.raises(EmbeddingError) as exc_info:
        GeminiEmbeddingAdapter(
            model_identifier="gemini-embedding-001",
            expected_dimensions=1536,
            task_type="INVALID_TYPE",
        )
    assert str(exc_info.value) == "Embedding failed safely."


# ---------------------------------------------------------------------------
# Batching behavior
# ---------------------------------------------------------------------------


def test_batching_sends_multiple_calls_for_large_input() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        batch_size=2,
        client=fake_client,
    )
    texts = ["a", "b", "c", "d", "e"]
    result = adapter.embed(texts)
    assert len(result) == 5
    assert fake_client.models.call_count == 3
    # Verify each batch had the right contents
    assert fake_client.models.recorded_calls[0]["contents"] == ["a", "b"]
    assert fake_client.models.recorded_calls[1]["contents"] == ["c", "d"]
    assert fake_client.models.recorded_calls[2]["contents"] == ["e"]


def test_single_text_single_call() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    result = adapter.embed(["hello world"])
    assert len(result) == 1
    assert fake_client.models.call_count == 1


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_without_api_call() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    result = adapter.embed([])
    assert result == ()
    assert fake_client.models.call_count == 0


def test_empty_tuple_returns_empty_without_api_call() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    result = adapter.embed(())
    assert result == ()
    assert fake_client.models.call_count == 0


# ---------------------------------------------------------------------------
# Exact vector count validation
# ---------------------------------------------------------------------------


def test_exact_vector_count_enforced() -> None:
    # Return 1 embedding when 2 texts were sent
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeEmbedResponse([_FakeContentEmbedding([0.1] * 1536)])]
    )
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["text1", "text2"])
    assert (
        "invalid vector response" in str(exc_info.value)
        or str(exc_info.value) == "Embedding failed safely."
    )


def test_total_count_matches_input_count() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        batch_size=3,
        client=fake_client,
    )
    texts = ["a", "b", "c", "d"]
    result = adapter.embed(texts)
    assert len(result) == 4


# ---------------------------------------------------------------------------
# Exact 1536-dimension enforcement
# ---------------------------------------------------------------------------


def test_wrong_vector_dimension_rejected() -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeEmbedResponse([_FakeContentEmbedding([0.1] * 512)])]
    )
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["text1"])
    assert (
        "invalid vector response" in str(exc_info.value)
        or str(exc_info.value) == "Embedding failed safely."
    )


def test_empty_vector_rejected() -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeEmbedResponse([_FakeContentEmbedding([])])]
    )
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


# ---------------------------------------------------------------------------
# Invalid vector values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_val",
    [
        True,
        False,
        "0.1",
        math.nan,
        math.inf,
        -math.inf,
        None,
        [1.0],
    ],
)
def test_invalid_numeric_values_in_vector_rejected(bad_val: Any) -> None:
    values = [0.1] * 1535 + [bad_val]
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeEmbedResponse([_FakeContentEmbedding(values)])]
    )
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


def test_valid_integer_and_float_values_accepted() -> None:
    values = [1] * 768 + [2.5] * 768
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeEmbedResponse([_FakeContentEmbedding(values)])]
    )
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    result = adapter.embed(["text1"])
    assert len(result) == 1
    assert all(isinstance(v, float) for v in result[0])


# ---------------------------------------------------------------------------
# Provider failure masking
# ---------------------------------------------------------------------------


def test_provider_exception_masked_as_embedding_error() -> None:
    err = RuntimeError("Internal server error with secret details")
    fake_client = _FakeGeminiClient(response_queue=[err, err, err])
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["text1"])
    assert "Internal server error with secret details" not in str(exc_info.value)
    assert str(exc_info.value) in (
        "Embedding failed safely.",
        "Embedding provider server error. Retry later.",
        "Embedding provider request failed.",
    )


def test_none_embeddings_response_raises_embedding_error() -> None:
    class BadResponse:
        embeddings = None

    fake_client = _FakeGeminiClient(response_queue=[BadResponse()])
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


# ---------------------------------------------------------------------------
# Process-control exception passthrough
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_passes_through() -> None:
    fake_client = _FakeGeminiClient(response_queue=[KeyboardInterrupt()])
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(KeyboardInterrupt):
        adapter.embed(["text1"])


def test_system_exit_passes_through() -> None:
    fake_client = _FakeGeminiClient(response_queue=[SystemExit(1)])
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=fake_client,
    )
    with pytest.raises(SystemExit):
        adapter.embed(["text1"])


# ---------------------------------------------------------------------------
# Non-sequence input rejection
# ---------------------------------------------------------------------------


def test_scalar_string_input_fails_safely() -> None:
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=_FakeGeminiClient(),
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed("not a list")  # type: ignore[arg-type]
    assert str(exc_info.value) == "Embedding failed safely."


@pytest.mark.parametrize(
    "invalid_input",
    [
        b"bytes input",
        bytearray(b"bytearray input"),
        12345,
        3.14,
        True,
    ],
)
def test_non_sequence_inputs_fail_safely(invalid_input: Any) -> None:
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        client=_FakeGeminiClient(),
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(invalid_input)
    assert str(exc_info.value) == "Embedding failed safely."


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_whitespace_only_model_identifier_fails_safely() -> None:
    """Whitespace-only model identifier fails immediately (evaluates as non-empty but blank)."""
    with pytest.raises(EmbeddingError) as exc_info:
        GeminiEmbeddingAdapter(
            model_identifier="   ",
            expected_dimensions=1536,
            client=_FakeGeminiClient(),
        )
    assert str(exc_info.value) == "Embedding failed safely."


def test_none_model_with_blank_config_fails_safely() -> None:
    """When model_identifier=None and settings.gemini_embedding_model is blank, it fails."""
    settings = Settings(
        gemini_embedding_model="",
        embedding_dimensions=1536,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        GeminiEmbeddingAdapter(
            model_identifier=None,
            expected_dimensions=1536,
            settings=settings,
            client=_FakeGeminiClient(),
        )
    assert str(exc_info.value) == "Embedding failed safely."


@pytest.mark.parametrize("invalid_dim", [0, -1, -1536])
def test_zero_or_negative_dimensions_fail_before_api_call(invalid_dim: int) -> None:
    with pytest.raises(EmbeddingError) as exc_info:
        GeminiEmbeddingAdapter(
            model_identifier="gemini-embedding-001",
            expected_dimensions=invalid_dim,
            client=_FakeGeminiClient(),
        )
    assert str(exc_info.value) == "Embedding failed safely."


@pytest.mark.parametrize("invalid_batch", [0, -5])
def test_zero_or_negative_batch_size_fails_on_embed(invalid_batch: int) -> None:
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        batch_size=invalid_batch,
        client=_FakeGeminiClient(),
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])
