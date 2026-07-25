from sourcetrace.core.config import get_settings
from sourcetrace.embeddings.provider import (
    EmbeddingProvider,
    GeminiEmbeddingAdapter,
    OpenAIEmbeddingAdapter,
)


def get_default_embedding_provider() -> EmbeddingProvider:
    """Build the configured embedding provider for a cloud_ai indexing job."""
    settings = get_settings()
    provider_name = (settings.embedding_provider or "").strip().lower()

    if provider_name == "gemini":
        return GeminiEmbeddingAdapter(settings=settings)
    if provider_name == "openai":
        return OpenAIEmbeddingAdapter(settings=settings)
    raise RuntimeError(f"Unsupported embedding provider: {provider_name!r}")


