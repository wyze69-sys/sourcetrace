"""Synchronous provider-neutral, storage-neutral core indexing service."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sourcetrace.core.exceptions import IndexingError, ProviderError
from sourcetrace.embeddings.provider import EmbeddingProvider
from sourcetrace.embeddings.service import embed_chunks
from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.scanner import SkippedFile
from sourcetrace.models.domain import CodeChunk, ParsedCodeChunk
from sourcetrace.parsers.python_ast import ParseResult, parse_acquired_source
from sourcetrace.storage.mongo_repositories import derive_chunk_search_metadata
from sourcetrace.storage.repositories import CodeChunkRepository


@dataclass(frozen=True, slots=True)
class IndexingResult:
    """Immutable result of indexing an acquired source code tree."""

    parsed_file_count: int
    chunk_count: int
    skipped_file_count: int
    skipped: tuple[SkippedFile, ...] = ()


class IndexingLifecycleObserver(Protocol):
    """Protocol for synchronous lifecycle event notifications during indexing."""

    def parsing_started(self) -> None: ...
    def embedding_started(self) -> None: ...
    def storing_started(self) -> None: ...
    def completed(self, result: IndexingResult) -> None: ...


class _NoOpIndexingLifecycleObserver:
    """Default no-op implementation of IndexingLifecycleObserver."""

    def parsing_started(self) -> None: pass
    def embedding_started(self) -> None: pass
    def storing_started(self) -> None: pass
    def completed(self, result: IndexingResult) -> None: pass


class RepositoryIndexingService:
    """Synchronous orchestrator indexing acquired source code trees."""

    def __init__(
        self,
        provider: EmbeddingProvider | None = None,
        code_chunk_repo: CodeChunkRepository | None = None,
        observer: IndexingLifecycleObserver | None = None,
    ) -> None:
        if code_chunk_repo is None:
            raise IndexingError("Indexing failed safely.")
        self._provider = provider
        self._code_chunk_repo = code_chunk_repo
        self._observer: IndexingLifecycleObserver = (
            observer if observer is not None else _NoOpIndexingLifecycleObserver()
        )

    def index_acquired_source(
        self,
        acquired_source: AcquiredSource,
        owner_session_id: str,
        repository_id: str,
        index_mode: str | None = None,
        now: datetime | None = None,
        observer: IndexingLifecycleObserver | None = None,
    ) -> IndexingResult:
        """Process acquired source through scanner, parser, optional embedder, and storage."""
        active_observer = observer if observer is not None else self._observer

        effective_mode = (
            index_mode
            if index_mode in ("static", "cloud_ai")
            else ("cloud_ai" if self._provider is not None else "static")
        )

        try:
            # 1. Validate service inputs safely
            if (
                type(acquired_source) is not AcquiredSource
                or type(owner_session_id) is not str
                or not owner_session_id.strip()
                or type(repository_id) is not str
                or not repository_id.strip()
            ):
                raise IndexingError("Indexing failed safely.")

            if now is None:
                now_dt = datetime.now(UTC)
            elif type(now) is not datetime or isinstance(now, bool):
                raise IndexingError("Indexing failed safely.")
            elif now.tzinfo is None:
                now_dt = now.replace(tzinfo=UTC)
            else:
                now_dt = now.astimezone(UTC)

            # 2. Lifecycle event: parsing_started
            active_observer.parsing_started()

            # Parse acquired source exactly once
            parse_result = parse_acquired_source(
                acquired_source=acquired_source,
                repository_id=repository_id,
                owner_session_id=owner_session_id,
            )

            # Validate ParseResult structure strictly
            if type(parse_result) is not ParseResult:
                raise IndexingError("Indexing failed safely.")

            parsed_file_count = parse_result.parsed_file_count
            if (
                type(parsed_file_count) is not int
                or isinstance(parsed_file_count, bool)
                or parsed_file_count < 0
            ):
                raise IndexingError("Indexing failed safely.")

            parsed_chunks = parse_result.chunks
            if type(parsed_chunks) is not tuple:
                raise IndexingError("Indexing failed safely.")

            skipped = parse_result.skipped
            if type(skipped) is not tuple:
                raise IndexingError("Indexing failed safely.")

            for s in skipped:
                if type(s) is not SkippedFile:
                    raise IndexingError("Indexing failed safely.")

            skipped_file_count = len(skipped)

            # Validate parsed chunks strictly
            seen_parsed_chunk_ids: set[str] = set()
            for chunk in parsed_chunks:
                if type(chunk) is not ParsedCodeChunk:
                    raise IndexingError("Indexing failed safely.")

                # Scope and ID matching
                if (
                    type(chunk.chunk_id) is not str
                    or not chunk.chunk_id.strip()
                    or chunk.repository_id != repository_id
                    or chunk.owner_session_id != owner_session_id
                    or chunk.chunk_id in seen_parsed_chunk_ids
                ):
                    raise IndexingError("Indexing failed safely.")
                seen_parsed_chunk_ids.add(chunk.chunk_id)

                # Path safety
                if (
                    type(chunk.relative_path) is not str
                    or not chunk.relative_path.strip()
                ):
                    raise IndexingError("Indexing failed safely.")

                rel_p = Path(chunk.relative_path)
                if (
                    rel_p.is_absolute()
                    or ".." in rel_p.parts
                    or chunk.relative_path.startswith("/")
                    or chunk.relative_path.startswith("\\")
                    or ":" in chunk.relative_path
                ):
                    raise IndexingError("Indexing failed safely.")

                # Line numbers
                if (
                    type(chunk.start_line) is not int
                    or type(chunk.end_line) is not int
                    or isinstance(chunk.start_line, bool)
                    or isinstance(chunk.end_line, bool)
                    or chunk.start_line < 1
                    or chunk.end_line < chunk.start_line
                ):
                    raise IndexingError("Indexing failed safely.")

                # Content and metadata non-empty strings
                if (
                    type(chunk.content) is not str
                    or not chunk.content
                    or type(chunk.content_hash) is not str
                    or not chunk.content_hash.strip()
                    or type(chunk.parser_version) is not str
                    or not chunk.parser_version.strip()
                    or type(chunk.symbol_name) is not str
                    or not chunk.symbol_name.strip()
                    or type(chunk.symbol_type) is not str
                    or not chunk.symbol_type.strip()
                    or type(chunk.language) is not str
                    or not chunk.language.strip()
                ):
                    raise IndexingError("Indexing failed safely.")

            chunks_to_store: list[CodeChunk] = []

            if effective_mode == "static":
                # Static mode: bypass embedding phase entirely
                for p in parsed_chunks:
                    derived = derive_chunk_search_metadata(
                        p.symbol_name, p.relative_path, p.content
                    )
                    chunks_to_store.append(
                        CodeChunk(
                            chunk_id=p.chunk_id,
                            repository_id=p.repository_id,
                            owner_session_id=p.owner_session_id,
                            relative_path=p.relative_path,
                            language=p.language,
                            symbol_name=p.symbol_name,
                            symbol_type=p.symbol_type,
                            start_line=p.start_line,
                            end_line=p.end_line,
                            content=p.content,
                            content_hash=p.content_hash,
                            parser_version=p.parser_version,
                            created_at=now_dt,
                            embedding_model=None,
                            embedding_dimensions=None,
                            embedding=None,
                            symbol_name_normalized=derived["symbol_name_normalized"],
                            relative_path_normalized=derived["relative_path_normalized"],
                            search_terms=derived["search_terms"],
                            search_text=derived["search_text"],
                        )
                    )

                active_observer.storing_started()

                if len(parsed_chunks) == 0:
                    result = IndexingResult(
                        parsed_file_count=parsed_file_count,
                        chunk_count=0,
                        skipped_file_count=skipped_file_count,
                        skipped=skipped,
                    )
                    active_observer.completed(result)
                    return result

                accepted_count = self._code_chunk_repo.save_many(chunks_to_store)
                if (
                    type(accepted_count) is not int
                    or isinstance(accepted_count, bool)
                    or accepted_count != len(chunks_to_store)
                ):
                    raise IndexingError("Indexing failed safely.")

                result = IndexingResult(
                    parsed_file_count=parsed_file_count,
                    chunk_count=accepted_count,
                    skipped_file_count=skipped_file_count,
                    skipped=skipped,
                )
                active_observer.completed(result)
                return result

            # Cloud AI mode: execute embedding pipeline
            if self._provider is None:
                raise IndexingError("Indexing failed safely.")

            active_observer.embedding_started()

            embedded_chunks = embed_chunks(
                chunks=parsed_chunks,
                provider=self._provider,
                now=now_dt,
            )

            if (
                type(embedded_chunks) is not tuple
                or len(embedded_chunks) != len(parsed_chunks)
            ):
                raise IndexingError("Indexing failed safely.")

            active_observer.storing_started()

            if len(parsed_chunks) == 0:
                if embedded_chunks != ():
                    raise IndexingError("Indexing failed safely.")
                result = IndexingResult(
                    parsed_file_count=parsed_file_count,
                    chunk_count=0,
                    skipped_file_count=skipped_file_count,
                    skipped=skipped,
                )
                active_observer.completed(result)
                return result

            for parsed, embedded in zip(parsed_chunks, embedded_chunks, strict=True):
                if type(embedded) is not CodeChunk:
                    raise IndexingError("Indexing failed safely.")

                if (
                    embedded.chunk_id != parsed.chunk_id
                    or embedded.repository_id != repository_id
                    or embedded.owner_session_id != owner_session_id
                    or embedded.relative_path != parsed.relative_path
                    or embedded.language != parsed.language
                    or embedded.symbol_name != parsed.symbol_name
                    or embedded.symbol_type != parsed.symbol_type
                    or embedded.start_line != parsed.start_line
                    or embedded.end_line != parsed.end_line
                    or embedded.content != parsed.content
                    or embedded.content_hash != parsed.content_hash
                    or embedded.parser_version != parsed.parser_version
                ):
                    raise IndexingError("Indexing failed safely.")

                if (
                    type(embedded.embedding_model) is not str
                    or not embedded.embedding_model.strip()
                    or type(embedded.embedding_dimensions) is not int
                    or isinstance(embedded.embedding_dimensions, bool)
                    or embedded.embedding_dimensions <= 0
                    or type(embedded.embedding) is not tuple
                    or len(embedded.embedding) != embedded.embedding_dimensions
                ):
                    raise IndexingError("Indexing failed safely.")

                for val in embedded.embedding:
                    if (
                        type(val) not in (int, float)
                        or isinstance(val, bool)
                        or not math.isfinite(val)
                    ):
                        raise IndexingError("Indexing failed safely.")

                derived = derive_chunk_search_metadata(
                    embedded.symbol_name, embedded.relative_path, embedded.content
                )
                chunks_to_store.append(
                    CodeChunk(
                        chunk_id=embedded.chunk_id,
                        repository_id=embedded.repository_id,
                        owner_session_id=embedded.owner_session_id,
                        relative_path=embedded.relative_path,
                        language=embedded.language,
                        symbol_name=embedded.symbol_name,
                        symbol_type=embedded.symbol_type,
                        start_line=embedded.start_line,
                        end_line=embedded.end_line,
                        content=embedded.content,
                        content_hash=embedded.content_hash,
                        parser_version=embedded.parser_version,
                        created_at=embedded.created_at,
                        embedding_model=embedded.embedding_model,
                        embedding_dimensions=embedded.embedding_dimensions,
                        embedding=embedded.embedding,
                        symbol_name_normalized=derived["symbol_name_normalized"],
                        relative_path_normalized=derived["relative_path_normalized"],
                        search_terms=derived["search_terms"],
                        search_text=derived["search_text"],
                    )
                )

            accepted_count = self._code_chunk_repo.save_many(chunks_to_store)

            if (
                type(accepted_count) is not int
                or isinstance(accepted_count, bool)
                or accepted_count != len(chunks_to_store)
            ):
                raise IndexingError("Indexing failed safely.")

            result = IndexingResult(
                parsed_file_count=parsed_file_count,
                chunk_count=accepted_count,
                skipped_file_count=skipped_file_count,
                skipped=skipped,
            )
            active_observer.completed(result)
            return result

        except (KeyboardInterrupt, SystemExit):
            raise
        except (IndexingError, ProviderError):
            raise
        except Exception:
            raise IndexingError("Indexing failed safely.") from None


def index_acquired_source(
    acquired_source: AcquiredSource,
    owner_session_id: str,
    repository_id: str,
    provider: EmbeddingProvider,
    code_chunk_repo: CodeChunkRepository,
    now: datetime | None = None,
    observer: IndexingLifecycleObserver | None = None,
) -> IndexingResult:
    """Convenience functional entry point for indexing acquired source code."""
    service = RepositoryIndexingService(
        provider=provider, code_chunk_repo=code_chunk_repo, observer=observer
    )
    return service.index_acquired_source(
        acquired_source=acquired_source,
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        now=now,
        observer=observer,
    )
