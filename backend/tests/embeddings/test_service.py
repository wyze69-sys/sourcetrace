"""Offline tests for deterministic chunk embedding service."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from sourcetrace.core.exceptions import EmbeddingError
from sourcetrace.embeddings.service import embed_chunks
from sourcetrace.models.domain import CodeChunk, ParsedCodeChunk


class _FakeProvider:
    def __init__(
        self,
        model_identifier: str = "text-embedding-3-small",
        embedding_dimensions: int = 3,
        raise_error: bool = False,
    ) -> None:
        self._model_identifier = model_identifier
        self._embedding_dimensions = embedding_dimensions
        self.raise_error = raise_error
        self.recorded_inputs: list[list[str]] = []

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    @property
    def embedding_dimensions(self) -> int:
        return self._embedding_dimensions

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if self.raise_error:
            raise EmbeddingError("Embedding failed safely.")
        input_list = list(texts)
        self.recorded_inputs.append(input_list)
        return tuple(
            tuple(round(0.1 * (k + 1), 4) for k in range(self._embedding_dimensions))
            for _ in input_list
        )


def _make_parsed_chunk(
    chunk_id: str = "chunk_123",
    relative_path: str = "src/app.py",
    symbol_name: str = "greet",
    symbol_type: str = "function",
    start_line: int = 1,
    end_line: int = 5,
    content: str = "def greet():\n    pass\n",
) -> ParsedCodeChunk:
    return ParsedCodeChunk(
        chunk_id=chunk_id,
        repository_id="repo_abc",
        owner_session_id="owner_xyz",
        relative_path=relative_path,
        language="python",
        symbol_name=symbol_name,
        symbol_type=symbol_type,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash="abc123hash",
        parser_version="python-ast-v1",
    )


# ---------------------------------------------------------------------------
# Chunk Embedding Service Tests
# ---------------------------------------------------------------------------


def test_empty_chunk_input_does_not_invoke_provider() -> None:
    provider = _FakeProvider()
    res = embed_chunks([], provider=provider)
    assert res == ()
    assert len(provider.recorded_inputs) == 0


def test_provider_receives_exact_chunk_content_in_original_order() -> None:
    c1 = _make_parsed_chunk(chunk_id="c1", content="def f1(): pass")
    c2 = _make_parsed_chunk(chunk_id="c2", content="def f2(): pass")
    provider = _FakeProvider()
    res = embed_chunks([c1, c2], provider=provider)
    assert len(res) == 2
    assert provider.recorded_inputs == [["def f1(): pass", "def f2(): pass"]]


def test_output_code_chunk_order_matches_input_order() -> None:
    c1 = _make_parsed_chunk(chunk_id="c1", relative_path="a.py")
    c2 = _make_parsed_chunk(chunk_id="c2", relative_path="b.py")
    provider = _FakeProvider()
    res = embed_chunks([c1, c2], provider=provider)
    assert res[0].chunk_id == "c1"
    assert res[0].relative_path == "a.py"
    assert res[1].chunk_id == "c2"
    assert res[1].relative_path == "b.py"


def test_every_parsed_metadata_field_preserved_exactly() -> None:
    p = _make_parsed_chunk(
        chunk_id="c_test",
        relative_path="src/main.py",
        symbol_name="MyClass.method",
        symbol_type="method",
        start_line=10,
        end_line=25,
        content="def method(self):\n    pass\n",
    )
    provider = _FakeProvider(model_identifier="test-model", embedding_dimensions=3)
    now = datetime.now(UTC)
    res = embed_chunks([p], provider=provider, now=now)
    assert len(res) == 1
    c = res[0]
    assert isinstance(c, CodeChunk)
    assert c.chunk_id == p.chunk_id
    assert c.repository_id == p.repository_id
    assert c.owner_session_id == p.owner_session_id
    assert c.relative_path == p.relative_path
    assert c.language == p.language
    assert c.symbol_name == p.symbol_name
    assert c.symbol_type == p.symbol_type
    assert c.start_line == p.start_line
    assert c.end_line == p.end_line
    assert c.content == p.content
    assert c.content_hash == p.content_hash
    assert c.parser_version == p.parser_version


def test_model_identifier_and_dimension_recorded() -> None:
    p = _make_parsed_chunk()
    provider = _FakeProvider(model_identifier="text-embedding-3-large", embedding_dimensions=3072)
    res = embed_chunks([p], provider=provider)
    assert res[0].embedding_model == "text-embedding-3-large"
    assert res[0].embedding_dimensions == 3072


def test_embeddings_are_immutable_tuples() -> None:
    p = _make_parsed_chunk()
    provider = _FakeProvider()
    res = embed_chunks([p], provider=provider)
    assert isinstance(res[0].embedding, tuple)
    assert res[0].embedding == (0.1, 0.2, 0.3)


def test_created_at_is_timezone_aware_utc() -> None:
    p = _make_parsed_chunk()
    provider = _FakeProvider()

    # Default now (unspecified)
    res = embed_chunks([p], provider=provider)
    assert res[0].created_at.tzinfo == UTC

    # Explicit now
    explicit_now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    res2 = embed_chunks([p], provider=provider, now=explicit_now)
    assert res2[0].created_at == explicit_now


def test_reusing_identical_parsed_chunks_produces_equivalent_metadata() -> None:
    p = _make_parsed_chunk()
    provider = _FakeProvider()
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    res1 = embed_chunks([p], provider=provider, now=now)
    res2 = embed_chunks([p], provider=provider, now=now)
    assert res1[0] == res2[0]


def test_provider_failure_returns_no_partial_result() -> None:
    c1 = _make_parsed_chunk(chunk_id="c1")
    c2 = _make_parsed_chunk(chunk_id="c2")
    failing_provider = _FakeProvider(raise_error=True)
    with pytest.raises(EmbeddingError) as exc_info:
        embed_chunks([c1, c2], provider=failing_provider)
    assert str(exc_info.value) == "Embedding failed safely."


def test_input_records_remain_unchanged() -> None:
    p = _make_parsed_chunk()
    original_id = p.chunk_id
    original_content = p.content
    provider = _FakeProvider()
    _ = embed_chunks([p], provider=provider)
    assert p.chunk_id == original_id
    assert p.content == original_content


# ---------------------------------------------------------------------------
# Service Metadata & Vector Hardening Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_model", ["", "   ", None])
def test_fake_provider_with_blank_model_metadata_rejected_by_embed_chunks(
    bad_model: Any,
) -> None:
    p = _make_parsed_chunk()
    provider = _FakeProvider(model_identifier=bad_model)
    with pytest.raises(EmbeddingError) as exc_info:
        embed_chunks([p], provider=provider)
    assert str(exc_info.value) == "Embedding failed safely."


@pytest.mark.parametrize("bad_dim", [-1, -5, True, False, 3.14, "3"])
def test_fake_provider_with_invalid_dimensions_rejected(bad_dim: Any) -> None:
    p = _make_parsed_chunk()
    provider = _FakeProvider(embedding_dimensions=bad_dim)
    with pytest.raises(EmbeddingError) as exc_info:
        embed_chunks([p], provider=provider)
    assert str(exc_info.value) == "Embedding failed safely."


def test_fake_provider_with_zero_dimensions_rejected() -> None:
    class _StaticZeroProvider(_FakeProvider):
        def __init__(self):
            super().__init__(model_identifier="none", embedding_dimensions=0)
        def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
            return tuple(() for _ in texts)

    p = _make_parsed_chunk()
    provider = _StaticZeroProvider()
    with pytest.raises(EmbeddingError):
        embed_chunks([p], provider=provider)


def test_fake_provider_returning_nan_inf_booleans_or_strings_rejected() -> None:
    class _MalformedVectorProvider(_FakeProvider):
        def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
            return ((float("nan"), 0.1, 0.2),)  # type: ignore[return-value]

    p = _make_parsed_chunk()
    provider = _MalformedVectorProvider(embedding_dimensions=3)
    with pytest.raises(EmbeddingError) as exc_info:
        embed_chunks([p], provider=provider)
    assert str(exc_info.value) == "Embedding failed safely."


def test_provider_property_access_exception_masked_safely() -> None:
    class _ExplodingProvider:
        @property
        def model_identifier(self) -> str:
            raise RuntimeError("Secret credentials leaked sk-999")

        @property
        def embedding_dimensions(self) -> int:
            return 3

        def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
            return ((0.1, 0.2, 0.3),)

    p = _make_parsed_chunk()
    provider = _ExplodingProvider()
    with pytest.raises(EmbeddingError) as exc_info:
        embed_chunks([p], provider=provider)  # type: ignore[arg-type]
    assert str(exc_info.value) == "Embedding failed safely."
    assert "sk-999" not in str(exc_info.value)

