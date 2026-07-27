"""Embedding provider contracts, OpenAI-compatible adapter, and chunk embedding service."""

from sourcetrace.embeddings.provider import (
    EmbeddingProvider,
    GeminiEmbeddingAdapter,
    OpenAIEmbeddingAdapter,
)
from sourcetrace.embeddings.service import embed_chunks

__all__ = [
    "EmbeddingProvider",
    "GeminiEmbeddingAdapter",
    "OpenAIEmbeddingAdapter",
    "embed_chunks",
]
