"""Deterministic chunk embedding service converting ParsedCodeChunk records to CodeChunk records."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime

from sourcetrace.core.exceptions import EmbeddingError
from sourcetrace.embeddings.provider import EmbeddingProvider
from sourcetrace.models.domain import CodeChunk, ParsedCodeChunk


def embed_chunks(
    chunks: Sequence[ParsedCodeChunk],
    provider: EmbeddingProvider,
    now: datetime | None = None,
) -> tuple[CodeChunk, ...]:
    """Embed a sequence of ParsedCodeChunk records into validated CodeChunk records.

    Preserves exact input chunk order, metadata, content hash, and line ranges.
    Returns an empty tuple without invoking provider when chunks sequence is empty.
    """
    if isinstance(chunks, (str, bytes, bytearray)) or not isinstance(chunks, Sequence):
        raise EmbeddingError("Embedding failed safely.")

    if not chunks:
        return ()

    try:
        raw_model = provider.model_identifier
        raw_dim = provider.embedding_dimensions
    except EmbeddingError:
        raise
    except Exception:
        raise EmbeddingError("Embedding failed safely.") from None

    if not isinstance(raw_model, str) or not raw_model.strip():
        raise EmbeddingError("Embedding failed safely.")
    model_id = raw_model.strip()

    if (
        not isinstance(raw_dim, int)
        or isinstance(raw_dim, bool)
        or raw_dim <= 0
    ):
        raise EmbeddingError("Embedding failed safely.")
    expected_dim = raw_dim

    if now is None:
        now_dt = datetime.now(UTC)
    elif now.tzinfo is None:
        now_dt = now.replace(tzinfo=UTC)
    else:
        now_dt = now.astimezone(UTC)

    texts = [c.content for c in chunks]

    try:
        vectors = provider.embed(texts)
    except EmbeddingError:
        raise
    except Exception:
        raise EmbeddingError("Embedding failed safely.") from None

    if not isinstance(vectors, (tuple, list)) or len(vectors) != len(chunks):
        raise EmbeddingError("Embedding failed safely.")

    out: list[CodeChunk] = []
    for parsed, vec in zip(chunks, vectors, strict=True):
        if not isinstance(vec, tuple) or len(vec) != expected_dim:
            raise EmbeddingError("Embedding failed safely.")

        for val in vec:
            if (
                val is None
                or isinstance(val, bool)
                or not isinstance(val, (int, float))
                or math.isnan(val)
                or math.isinf(val)
            ):
                raise EmbeddingError("Embedding failed safely.")

        out.append(
            CodeChunk(
                chunk_id=parsed.chunk_id,
                repository_id=parsed.repository_id,
                owner_session_id=parsed.owner_session_id,
                relative_path=parsed.relative_path,
                language=parsed.language,
                symbol_name=parsed.symbol_name,
                symbol_type=parsed.symbol_type,
                start_line=parsed.start_line,
                end_line=parsed.end_line,
                content=parsed.content,
                content_hash=parsed.content_hash,
                parser_version=parsed.parser_version,
                embedding_model=model_id,
                embedding_dimensions=expected_dim,
                created_at=now_dt,
                embedding=vec,
                references=parsed.references,
                imports=parsed.imports,
                endpoints=parsed.endpoints,
                extraction_truncated=parsed.extraction_truncated,
            )
        )

    return tuple(out)
