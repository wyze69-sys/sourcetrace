"""Offline tests for embedding provider contract and OpenAI adapter."""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import EmbeddingError, SourceTraceError
from sourcetrace.embeddings.provider import (
    EmbeddingProvider,
    OpenAIEmbeddingAdapter,
)


class _FakeEmbeddingItem:
    def __init__(self, index: Any, embedding: Any) -> None:
        self.index = index
        self.embedding = embedding


class _FakeEmbeddingData:
    def __init__(self, items: list[Any]) -> None:
        self.data = items


class _FakeClient:
    def __init__(self, response_data_list: list[list[Any]] | None = None) -> None:
        self.call_count = 0
        self.recorded_calls: list[dict[str, Any]] = []
        self._response_data_list = response_data_list or []

    @property
    def embeddings(self) -> _FakeEmbeddingsApi:
        return _FakeEmbeddingsApi(self)


class _FakeEmbeddingsApi:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        self._client.call_count += 1
        self._client.recorded_calls.append(kwargs)
        if self._client._response_data_list:
            items = self._client._response_data_list.pop(0)
            return _FakeEmbeddingData(items)
        # Default: 1 vector per input text
        input_texts = kwargs.get("input", [])
        items = [_FakeEmbeddingItem(i, [0.1, 0.2, 0.3]) for i in range(len(input_texts))]
        return _FakeEmbeddingData(items)


# ---------------------------------------------------------------------------
# Provider Protocol Signature
# ---------------------------------------------------------------------------


def test_provider_protocol_typing() -> None:
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        client=_FakeClient(),
    )
    assert isinstance(adapter, EmbeddingProvider)
    assert adapter.model_identifier == "text-embedding-3-small"
    assert adapter.embedding_dimensions == 3


# ---------------------------------------------------------------------------
# Adapter Inputs & Config Validation
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_tuple_without_sdk_call() -> None:
    client = _FakeClient()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        client=client,
    )
    res = adapter.embed([])
    assert res == ()
    assert client.call_count == 0


@pytest.mark.parametrize("invalid_model", ["", "   ", None])
def test_missing_or_blank_model_fails_safely(invalid_model: Any) -> None:
    with pytest.raises(EmbeddingError) as exc_info:
        OpenAIEmbeddingAdapter(
            model_identifier=invalid_model,
            expected_dimensions=3,
            client=_FakeClient(),
        )
    assert str(exc_info.value) == "Embedding failed safely."


@pytest.mark.parametrize("invalid_dim", [0, -1, -1536])
def test_zero_or_negative_expected_dimensions_fail_before_sdk_call(invalid_dim: int) -> None:
    client = _FakeClient()
    with pytest.raises(EmbeddingError) as exc_info:
        OpenAIEmbeddingAdapter(
            model_identifier="text-embedding-3-small",
            expected_dimensions=invalid_dim,
            client=client,
        )
    assert str(exc_info.value) == "Embedding failed safely."
    assert client.call_count == 0


@pytest.mark.parametrize("invalid_batch", [0, -5])
def test_zero_or_negative_batch_size_fails_before_sdk_call(invalid_batch: int) -> None:
    client = _FakeClient()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        batch_size=invalid_batch,
        client=client,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["text1"])
    assert str(exc_info.value) == "Embedding failed safely."
    assert client.call_count == 0


# ---------------------------------------------------------------------------
# Batching and Input Ordering
# ---------------------------------------------------------------------------


def test_inputs_split_into_deterministic_batches() -> None:
    client = _FakeClient()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        batch_size=2,
        client=client,
    )
    texts = ["a", "b", "c", "d", "e"]
    res = adapter.embed(texts)
    assert len(res) == 5
    assert client.call_count == 3
    assert client.recorded_calls[0]["input"] == ["a", "b"]
    assert client.recorded_calls[1]["input"] == ["c", "d"]
    assert client.recorded_calls[2]["input"] == ["e"]


def test_out_of_order_response_items_restored() -> None:
    # Provider returns index 1 before index 0
    batch_data = [
        [
            _FakeEmbeddingItem(1, [0.4, 0.5, 0.6]),
            _FakeEmbeddingItem(0, [0.1, 0.2, 0.3]),
        ]
    ]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        client=client,
    )
    res = adapter.embed(["first", "second"])
    assert res == ((0.1, 0.2, 0.3), (0.4, 0.5, 0.6))


# ---------------------------------------------------------------------------
# Response Index Validation
# ---------------------------------------------------------------------------


def test_missing_response_index_rejected() -> None:
    batch_data = [[_FakeEmbeddingItem(None, [0.1, 0.2, 0.3])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["text"])
    assert str(exc_info.value) == "Embedding failed safely."


def test_duplicate_response_index_rejected() -> None:
    batch_data = [
        [
            _FakeEmbeddingItem(0, [0.1, 0.2, 0.3]),
            _FakeEmbeddingItem(0, [0.4, 0.5, 0.6]),
        ]
    ]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1", "text2"])


@pytest.mark.parametrize("bad_idx", [-1, 2, 99])
def test_negative_or_out_of_range_index_rejected(bad_idx: int) -> None:
    batch_data = [[_FakeEmbeddingItem(bad_idx, [0.1, 0.2, 0.3])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


@pytest.mark.parametrize("bool_idx", [True, False, "0", 1.0])
def test_boolean_and_non_integer_index_rejected(bool_idx: Any) -> None:
    batch_data = [[_FakeEmbeddingItem(bool_idx, [0.1, 0.2, 0.3])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


def test_response_count_mismatch_rejected() -> None:
    # 2 inputs sent, only 1 item returned
    batch_data = [[_FakeEmbeddingItem(0, [0.1, 0.2, 0.3])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1", "text2"])


# ---------------------------------------------------------------------------
# Vector & Numeric Validation
# ---------------------------------------------------------------------------


def test_empty_vector_rejected() -> None:
    batch_data = [[_FakeEmbeddingItem(0, [])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


def test_wrong_vector_dimension_rejected() -> None:
    # Expected dim 3, returned 2
    batch_data = [[_FakeEmbeddingItem(0, [0.1, 0.2])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


def test_inconsistent_vector_dimensions_rejected() -> None:
    batch_data = [
        [
            _FakeEmbeddingItem(0, [0.1, 0.2, 0.3]),
            _FakeEmbeddingItem(1, [0.1, 0.2]),
        ]
    ]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1", "text2"])


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
def test_invalid_numeric_values_rejected(bad_val: Any) -> None:
    batch_data = [[_FakeEmbeddingItem(0, [0.1, bad_val, 0.3])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    with pytest.raises(EmbeddingError):
        adapter.embed(["text1"])


def test_valid_numeric_values_converted_to_floats() -> None:
    # Integers and floats allowed
    batch_data = [[_FakeEmbeddingItem(0, [1, 2.5, -3])]]
    client = _FakeClient(response_data_list=batch_data)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=client
    )
    res = adapter.embed(["text1"])
    assert res == ((1.0, 2.5, -3.0),)
    assert isinstance(res[0][0], float)


# ---------------------------------------------------------------------------
# SDK Exceptions & Secret Protection
# ---------------------------------------------------------------------------


def test_sdk_exception_becomes_safe_domain_exception() -> None:
    mock_client = MagicMock()
    err_msg = "OpenAI API connection failed secret_key=sk-12345"
    mock_client.embeddings.create.side_effect = RuntimeError(err_msg)
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="test", expected_dimensions=3, client=mock_client
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["text1"])

    assert str(exc_info.value) == "Embedding failed safely."
    assert "sk-12345" not in str(exc_info.value)
    assert "OpenAI API connection" not in str(exc_info.value)
    assert issubclass(EmbeddingError, SourceTraceError)


def test_health_and_settings_functional_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCETRACE_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("SOURCETRACE_LLM_API_KEY", raising=False)
    settings = Settings()
    assert settings.env == "development"


# ---------------------------------------------------------------------------
# Configuration Isolation & Non-Fallback Tests
# ---------------------------------------------------------------------------


def test_default_settings_embedding_dimensions_are_1536() -> None:
    settings = Settings()
    assert settings.embedding_dimensions == 1536


def test_blank_embedding_model_does_not_fall_back_to_llm_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCETRACE_EMBEDDING_MODEL", "")
    monkeypatch.setenv("SOURCETRACE_LLM_MODEL", "gpt-4o")
    settings = Settings()
    with pytest.raises(EmbeddingError) as exc_info:
        OpenAIEmbeddingAdapter(settings=settings, expected_dimensions=3, client=_FakeClient())
    assert str(exc_info.value) == "Embedding failed safely."


def test_missing_embedding_api_key_does_not_fall_back_to_llm_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCETRACE_EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("SOURCETRACE_LLM_API_KEY", "sk-llm-secret-key-12345")
    settings = Settings()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        settings=settings,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["text1"])
    assert str(exc_info.value) == "Embedding failed safely."
    assert "sk-llm-secret-key-12345" not in str(exc_info.value)


def test_missing_embedding_base_url_does_not_silently_use_llm_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCETRACE_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.setenv("SOURCETRACE_LLM_BASE_URL", "https://llm-custom.example.com/v1")
    monkeypatch.setenv("SOURCETRACE_EMBEDDING_API_KEY", "sk-emb-key")
    settings = Settings()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        settings=settings,
    )
    mock_client = MagicMock()
    mock_item = MagicMock()
    mock_item.index = 0
    mock_item.embedding = [0.1, 0.2, 0.3]
    mock_client.embeddings.create.return_value.data = [mock_item]
    patch_target = "sourcetrace.embeddings.provider.openai.OpenAI"
    with patch(patch_target, return_value=mock_client) as mock_openai:
        adapter.embed(["text1"])
        mock_openai.assert_called_once()
        _, kwargs = mock_openai.call_args
        assert kwargs.get("base_url") != "https://llm-custom.example.com/v1"


def test_unconfigured_dimensions_env_var_yields_default_of_1536(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCETRACE_EMBEDDING_DIMENSIONS", raising=False)
    settings = Settings()
    assert settings.embedding_dimensions == 1536
    client = _FakeClient()
    # OpenAI adapter uses settings.embedding_dimensions (now 1536 by default)
    # so it no longer fails — it uses the default dimension of 1536.
    # Constructing the adapter must succeed with the default.
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        settings=settings,
        client=client,
    )
    assert adapter.embedding_dimensions == 1536
    assert client.call_count == 0


# ---------------------------------------------------------------------------
# Scalar & Non-Sequence Input Rejection
# ---------------------------------------------------------------------------


def test_scalar_string_input_fails_safely() -> None:
    client = _FakeClient()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        client=client,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed("source code")  # type: ignore[arg-type]
    assert str(exc_info.value) == "Embedding failed safely."
    assert client.call_count == 0


@pytest.mark.parametrize(
    "invalid_input",
    [
        b"source code",
        bytearray(b"source code"),
        12345,
        3.14,
        True,
        {"key": "val"},
    ],
)
def test_bytes_bytearray_and_non_sequence_inputs_fail_safely(invalid_input: Any) -> None:
    client = _FakeClient()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        client=client,
    )
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(invalid_input)
    assert str(exc_info.value) == "Embedding failed safely."
    assert client.call_count == 0


def test_empty_list_and_empty_tuple_return_empty_tuple_without_client_creation() -> None:
    client = _FakeClient()
    adapter = OpenAIEmbeddingAdapter(
        model_identifier="text-embedding-3-small",
        expected_dimensions=3,
        client=client,
    )
    assert adapter.embed([]) == ()
    assert adapter.embed(()) == ()
    assert client.call_count == 0

