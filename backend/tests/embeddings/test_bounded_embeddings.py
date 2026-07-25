"""Offline unit tests for bounded embedding requests and Gemini failure classification."""

from unittest.mock import MagicMock

import pytest

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import EmbeddingError
from sourcetrace.embeddings.provider import (
    MAX_SINGLE_CHUNK_CHARS,
    GeminiEmbeddingAdapter,
    _classify_gemini_error,
)


def test_classify_gemini_errors():
    # 1. Quota Exhaustion / Resource Exhausted
    exc1 = Exception("429 RESOURCE_EXHAUSTED: You exceeded your current quota.")
    msg1, retry1 = _classify_gemini_error(exc1)
    assert msg1 == "Embedding provider quota is exhausted. Try again after the quota resets."
    assert not retry1

    # 2. Rate Limited
    exc2 = Exception("429 Rate limit exceeded. Please try again in 5 seconds.")
    msg2, retry2 = _classify_gemini_error(exc2)
    assert msg2 == "Embedding provider is temporarily rate limited. Retry later."
    assert retry2

    # 3. Invalid Config / API Key
    exc3 = Exception("401 Invalid API key supplied.")
    msg3, retry3 = _classify_gemini_error(exc3)
    assert msg3 == "Embedding provider configuration is invalid."
    assert not retry3

    # 4. Timeout
    exc4 = Exception("504 Gateway Timeout or deadline exceeded.")
    msg4, retry4 = _classify_gemini_error(exc4)
    assert msg4 == "Embedding provider request timed out. Retry later."
    assert retry4


def test_oversized_chunk_input_rejection():
    cfg = Settings(gemini_api_key="mock-key")
    mock_client = MagicMock()
    adapter = GeminiEmbeddingAdapter(client=mock_client, settings=cfg)

    oversized_text = "a" * (MAX_SINGLE_CHUNK_CHARS + 100)
    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed([oversized_text])

    assert "exceeded the embedding input limit" in str(exc_info.value)
    assert mock_client.models.embed_content.call_count == 0


def test_dual_batching_item_and_character_bounds():
    cfg = Settings(gemini_api_key="mock-key")
    mock_client = MagicMock()

    # Mock response factory
    def mock_embed_content(model, contents, config):
        res = MagicMock()
        embeddings = []
        for _ in contents:
            item = MagicMock()
            item.values = [0.1] * 1536
            embeddings.append(item)
        res.embeddings = embeddings
        return res

    mock_client.models.embed_content.side_effect = mock_embed_content
    adapter = GeminiEmbeddingAdapter(batch_size=10, client=mock_client, settings=cfg)

    # 15 items, each 2000 characters (total 30,000 chars)
    # Batch limit is max 10 items AND max 24,000 characters.
    # First batch takes 10 items (20,000 chars).
    # Second batch takes 5 items (10,000 chars).
    texts = ["x" * 2000] * 15
    vectors = adapter.embed(texts)

    assert len(vectors) == 15
    assert mock_client.models.embed_content.call_count == 2


def test_dual_batching_character_limit_flush():
    cfg = Settings(gemini_api_key="mock-key")
    mock_client = MagicMock()

    def mock_embed_content(model, contents, config):
        res = MagicMock()
        embeddings = []
        for _ in contents:
            item = MagicMock()
            item.values = [0.1] * 1536
            embeddings.append(item)
        res.embeddings = embeddings
        return res

    mock_client.models.embed_content.side_effect = mock_embed_content
    adapter = GeminiEmbeddingAdapter(batch_size=100, client=mock_client, settings=cfg)

    # 5 items, each 6000 characters (total 30,000 chars)
    # Batch 1 takes 4 items (24,000 chars).
    # Batch 2 takes 1 item (6,000 chars).
    texts = ["y" * 6000] * 5
    vectors = adapter.embed(texts)

    assert len(vectors) == 5
    assert mock_client.models.embed_content.call_count == 2


def test_transient_429_retry_success():
    cfg = Settings(gemini_api_key="mock-key")
    mock_client = MagicMock()

    res = MagicMock()
    item = MagicMock()
    item.values = [0.2] * 1536
    res.embeddings = [item]

    # First call raises 429 rate limit, second call succeeds
    mock_client.models.embed_content.side_effect = [
        Exception("429 Too many requests, rate limit exceeded"),
        res,
    ]

    adapter = GeminiEmbeddingAdapter(client=mock_client, settings=cfg)
    vectors = adapter.embed(["valid code text"])

    assert len(vectors) == 1
    assert len(vectors[0]) == 1536
    assert mock_client.models.embed_content.call_count == 2


def test_permanent_quota_exhaustion_no_retry():
    cfg = Settings(gemini_api_key="mock-key")
    mock_client = MagicMock()

    mock_client.models.embed_content.side_effect = Exception(
        "429 RESOURCE_EXHAUSTED: You exceeded your current quota."
    )

    adapter = GeminiEmbeddingAdapter(client=mock_client, settings=cfg)

    with pytest.raises(EmbeddingError) as exc_info:
        adapter.embed(["valid code text"])

    assert "quota is exhausted" in str(exc_info.value)
    # Must NOT retry on permanent quota exhaustion
    assert mock_client.models.embed_content.call_count == 1
