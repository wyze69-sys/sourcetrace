"""Provider-neutral embedding interface and OpenAI-compatible adapter."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import openai

from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import EmbeddingError


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for provider-neutral embedding generation."""

    @property
    def model_identifier(self) -> str:
        """Canonical configured model identifier."""
        ...

    @property
    def embedding_dimensions(self) -> int:
        """Exact expected vector dimension."""
        ...

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed a sequence of input texts in exact input order."""
        ...


# ---------------------------------------------------------------------------
# OpenAI-compatible embedding adapter (kept for optional provider selection)
# ---------------------------------------------------------------------------


class OpenAIEmbeddingAdapter:
    """Injectable OpenAI-compatible embedding provider adapter."""

    def __init__(
        self,
        model_identifier: str | None = None,
        expected_dimensions: int | None = None,
        batch_size: int | None = None,
        client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()

        resolved_model = model_identifier or cfg.embedding_model
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise EmbeddingError("Embedding failed safely.")
        self._model_identifier: str = resolved_model.strip()

        if expected_dimensions is not None:
            resolved_dim = expected_dimensions
        else:
            resolved_dim = cfg.embedding_dimensions

        if not isinstance(resolved_dim, int) or isinstance(resolved_dim, bool) or resolved_dim <= 0:
            raise EmbeddingError("Embedding failed safely.")
        self._expected_dimensions: int = resolved_dim

        if batch_size is not None:
            resolved_batch = batch_size
        else:
            resolved_batch = cfg.embedding_batch_size

        if isinstance(resolved_batch, int) and not isinstance(resolved_batch, bool):
            self._batch_size = resolved_batch
        else:
            self._batch_size = 0

        self._client: Any | None = client
        self._settings: Settings = cfg

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    @property
    def embedding_dimensions(self) -> int:
        return self._expected_dimensions

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        api_key_secret = self._settings.embedding_api_key
        if not api_key_secret:
            raise EmbeddingError("Embedding failed safely.")

        raw_key = api_key_secret.get_secret_value()
        if not raw_key or not raw_key.strip():
            raise EmbeddingError("Embedding failed safely.")

        base_url = self._settings.embedding_base_url
        client_kwargs: dict[str, Any] = {"api_key": raw_key.strip()}
        if isinstance(base_url, str) and base_url.strip():
            client_kwargs["base_url"] = base_url.strip()

        try:
            self._client = openai.OpenAI(**client_kwargs)
        except Exception:
            raise EmbeddingError("Embedding failed safely.") from None

        return self._client

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if isinstance(texts, (str, bytes, bytearray)):
            raise EmbeddingError("Embedding failed safely.")

        if not isinstance(texts, Sequence):
            raise EmbeddingError("Embedding failed safely.")

        if not texts:
            return ()

        # Pre-execution validation
        if (
            not isinstance(self._batch_size, int)
            or isinstance(self._batch_size, bool)
            or self._batch_size <= 0
        ):
            raise EmbeddingError("Embedding failed safely.")

        if (
            not isinstance(self._expected_dimensions, int)
            or isinstance(self._expected_dimensions, bool)
            or self._expected_dimensions <= 0
        ):
            raise EmbeddingError("Embedding failed safely.")

        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise EmbeddingError("Embedding failed safely.")

        client = self._get_client()
        text_list = list(texts)
        n_total = len(text_list)
        all_vectors: list[tuple[float, ...]] = []

        idx = 0
        while idx < n_total:
            batch: list[str] = []
            batch_chars = 0

            while idx < n_total and len(batch) < self._batch_size:
                next_text = text_list[idx]
                if len(next_text) > MAX_SINGLE_CHUNK_CHARS:
                    raise EmbeddingError("A source chunk exceeded the embedding input limit.")

                if batch and (batch_chars + len(next_text) > MAX_EMBEDDING_BATCH_CHARS):
                    break

                batch.append(next_text)
                batch_chars += len(next_text)
                idx += 1

            batch_len = len(batch)

            try:
                response = client.embeddings.create(
                    input=batch,
                    model=self._model_identifier,
                )
            except Exception:
                raise EmbeddingError("Embedding failed safely.") from None

            data_items = getattr(response, "data", None)
            if not isinstance(data_items, (list, tuple)) or len(data_items) != batch_len:
                raise EmbeddingError("Embedding failed safely.")

            batch_vectors: list[tuple[float, ...] | None] = [None] * batch_len
            seen_indexes: set[int] = set()

            for item in data_items:
                b_idx = getattr(item, "index", None)
                if (
                    b_idx is None
                    or not isinstance(b_idx, int)
                    or isinstance(b_idx, bool)
                    or b_idx < 0
                    or b_idx >= batch_len
                    or b_idx in seen_indexes
                ):
                    raise EmbeddingError("Embedding failed safely.")

                seen_indexes.add(b_idx)

                raw_vec = getattr(item, "embedding", None)
                if (
                    not isinstance(raw_vec, (list, tuple))
                    or len(raw_vec) != self._expected_dimensions
                ):
                    raise EmbeddingError("Embedding failed safely.")

                float_vec: list[float] = []
                for val in raw_vec:
                    if (
                        val is None
                        or isinstance(val, bool)
                        or not isinstance(val, (int, float))
                        or math.isnan(val)
                        or math.isinf(val)
                    ):
                        raise EmbeddingError("Embedding failed safely.")
                    float_vec.append(float(val))

                batch_vectors[b_idx] = tuple(float_vec)

            if any(v is None for v in batch_vectors):
                raise EmbeddingError("Embedding failed safely.")

            for v in batch_vectors:
                if v is not None:
                    all_vectors.append(v)

        return tuple(all_vectors)


# ---------------------------------------------------------------------------
# Gemini embedding adapter (google-genai SDK)
# ---------------------------------------------------------------------------

# Valid task type strings accepted by the gemini-embedding-001 model.
_GEMINI_VALID_TASK_TYPES = frozenset(
    {
        "RETRIEVAL_DOCUMENT",
        "RETRIEVAL_QUERY",
        "SEMANTIC_SIMILARITY",
        "CLASSIFICATION",
        "CLUSTERING",
        "QUESTION_ANSWERING",
        "FACT_VERIFICATION",
    }
)


MAX_EMBEDDING_BATCH_CHARS: int = 24000
MAX_SINGLE_CHUNK_CHARS: int = 12000


def _classify_gemini_error(error: Exception) -> tuple[str, bool]:
    """Classify Gemini API errors into a safe actionable error message and retry flag.

    Returns:
        tuple[safe_message, is_retryable]
    """
    status_code: int | None = None
    for attr in ("status_code", "code", "http_status"):
        value = getattr(error, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            status_code = value
            break

    err_parts = [str(error), repr(error)]
    msg_attr = getattr(error, "message", None)
    if isinstance(msg_attr, str):
        err_parts.append(msg_attr)
    args_attr = getattr(error, "args", None)
    if args_attr:
        err_parts.append(str(args_attr))
    text = " ".join(err_parts).casefold()

    # 1. Quota Exhaustion / Resource Exhausted (429 with quota indicator)
    if (
        "resource_exhausted" in text
        or "quota" in text
        or "exceeded your current quota" in text
        or "daily limit" in text
    ):
        return (
            "Embedding provider quota is exhausted. Try again after the quota resets.",
            False,
        )

    # 2. Rate limiting (transient 429 / rate limit)
    if status_code == 429 or "rate limit" in text or "too many requests" in text:
        return ("Embedding provider is temporarily rate limited. Retry later.", True)

    # 3. Invalid credentials / Unauthorized / Forbidden
    if (
        status_code in (401, 403)
        or "api_key" in text
        or "api key" in text
        or "unauthorized" in text
        or ("invalid argument" in text and "key" in text)
        or "permission denied" in text
    ):
        return ("Embedding provider configuration is invalid.", False)

    # 4. Model not found / Unsupported model
    if status_code == 404 or ("model" in text and "not found" in text):
        return ("Embedding provider configuration is invalid.", False)

    # 5. Input length / Oversized input
    if "input limit" in text or "too long" in text or "context length" in text:
        return ("A source chunk exceeded the embedding input limit.", False)

    # 6. Timeout / Deadline exceeded
    if status_code in (408, 504) or "timeout" in text or "timed out" in text or "deadline" in text:
        return ("Embedding provider request timed out. Retry later.", True)

    # 7. Transient Server Errors (500, 502, 503)
    if status_code in (500, 502, 503) or "unavailable" in text or "internal" in text:
        return ("Embedding provider server error. Retry later.", True)

    # Fallback safe error message
    return ("Embedding provider request failed.", False)


class GeminiEmbeddingAdapter:
    """Injectable Gemini embedding provider adapter using the google-genai SDK.

    Lazy client: no network request is made until the first ``embed()`` call.
    Reads ``SOURCETRACE_GEMINI_API_KEY`` exclusively; never reads OpenAI keys.

    ``task_type`` controls the embedding optimization:
    - ``"RETRIEVAL_DOCUMENT"``  — use when embedding source code chunks for indexing.
    - ``"RETRIEVAL_QUERY"``     — use when embedding natural-language query text.

    Both task types produce vectors that are compatible for cosine-similarity
    retrieval with each other when using the same model and output_dimensionality.
    """

    def __init__(
        self,
        model_identifier: str | None = None,
        expected_dimensions: int | None = None,
        batch_size: int | None = None,
        task_type: str = "RETRIEVAL_DOCUMENT",
        client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()

        resolved_model = model_identifier or cfg.gemini_embedding_model
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise EmbeddingError("Embedding failed safely.")
        self._model_identifier: str = resolved_model.strip()

        if expected_dimensions is not None:
            resolved_dim = expected_dimensions
        else:
            resolved_dim = cfg.embedding_dimensions

        if not isinstance(resolved_dim, int) or isinstance(resolved_dim, bool) or resolved_dim <= 0:
            raise EmbeddingError("Embedding failed safely.")
        self._expected_dimensions: int = resolved_dim

        if batch_size is not None:
            resolved_batch = batch_size
        else:
            resolved_batch = cfg.embedding_batch_size

        if isinstance(resolved_batch, int) and not isinstance(resolved_batch, bool):
            self._batch_size = resolved_batch
        else:
            self._batch_size = 0

        if not isinstance(task_type, str) or task_type not in _GEMINI_VALID_TASK_TYPES:
            raise EmbeddingError("Embedding failed safely.")
        self._task_type: str = task_type

        # Injected client for testing; None means lazy construction on first embed().
        self._client: Any | None = client
        self._settings: Settings = cfg

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    @property
    def embedding_dimensions(self) -> int:
        return self._expected_dimensions

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        api_key_secret = self._settings.gemini_api_key
        if not api_key_secret:
            raise EmbeddingError("Embedding provider configuration is invalid.")

        raw_key = api_key_secret.get_secret_value()
        if not raw_key or not raw_key.strip():
            raise EmbeddingError("Embedding provider configuration is invalid.")

        try:
            from google import genai  # type: ignore[import-untyped]

            self._client = genai.Client(api_key=raw_key.strip())
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EmbeddingError("Embedding provider configuration is invalid.") from None

        return self._client

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if isinstance(texts, (str, bytes, bytearray)):
            raise EmbeddingError("Embedding failed safely.")

        if not isinstance(texts, Sequence):
            raise EmbeddingError("Embedding failed safely.")

        if not texts:
            return ()

        # Pre-execution validation
        if (
            not isinstance(self._batch_size, int)
            or isinstance(self._batch_size, bool)
            or self._batch_size <= 0
        ):
            raise EmbeddingError("Embedding failed safely.")

        if (
            not isinstance(self._expected_dimensions, int)
            or isinstance(self._expected_dimensions, bool)
            or self._expected_dimensions <= 0
        ):
            raise EmbeddingError("Embedding failed safely.")

        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise EmbeddingError("Embedding failed safely.")

        client = self._get_client()
        text_list = list(texts)
        n_total = len(text_list)
        all_vectors: list[tuple[float, ...]] = []

        try:
            from google.genai import types as genai_types  # type: ignore[import-untyped]
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise EmbeddingError("Embedding provider configuration is invalid.") from None

        idx = 0
        while idx < n_total:
            batch: list[str] = []
            batch_chars = 0

            while idx < n_total and len(batch) < self._batch_size:
                next_text = text_list[idx]
                if len(next_text) > MAX_SINGLE_CHUNK_CHARS:
                    raise EmbeddingError("A source chunk exceeded the embedding input limit.")

                if batch and (batch_chars + len(next_text) > MAX_EMBEDDING_BATCH_CHARS):
                    break

                batch.append(next_text)
                batch_chars += len(next_text)
                idx += 1

            batch_len = len(batch)
            response = None
            last_error_msg = "Embedding provider request failed."

            for attempt in range(3):
                try:
                    response = client.models.embed_content(
                        model=self._model_identifier,
                        contents=batch,
                        config=genai_types.EmbedContentConfig(
                            task_type=self._task_type,
                            output_dimensionality=self._expected_dimensions,
                        ),
                    )
                    break
                except (KeyboardInterrupt, SystemExit):
                    raise
                except EmbeddingError:
                    raise
                except Exception as error:
                    safe_msg, is_retryable = _classify_gemini_error(error)
                    last_error_msg = safe_msg
                    if attempt == 2 or not is_retryable:
                        raise EmbeddingError(safe_msg) from None
                    time.sleep(2**attempt)

            if response is None:
                raise EmbeddingError(last_error_msg)

            # Gemini returns result.embeddings: list of ContentEmbedding objects
            # each with a .values: list[float] attribute
            raw_embeddings = getattr(response, "embeddings", None)
            if not isinstance(raw_embeddings, (list, tuple)) or len(raw_embeddings) != batch_len:
                raise EmbeddingError("Embedding provider returned an invalid vector response.")

            for emb in raw_embeddings:
                raw_vec = getattr(emb, "values", None)
                if (
                    not isinstance(raw_vec, (list, tuple))
                    or len(raw_vec) != self._expected_dimensions
                ):
                    raise EmbeddingError("Embedding provider returned an invalid vector response.")

                float_vec: list[float] = []
                for val in raw_vec:
                    if (
                        val is None
                        or isinstance(val, bool)
                        or not isinstance(val, (int, float))
                        or math.isnan(val)
                        or math.isinf(val)
                    ):
                        raise EmbeddingError(
                            "Embedding provider returned an invalid vector response."
                        )
                    float_vec.append(float(val))

                all_vectors.append(tuple(float_vec))

        if len(all_vectors) != n_total:
            raise EmbeddingError("Embedding provider returned an invalid vector response.")

        return tuple(all_vectors)
