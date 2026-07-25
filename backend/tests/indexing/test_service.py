"""Offline integration and adversarial tests for RepositoryIndexingService."""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import sourcetrace.ingestion.indexing
from sourcetrace.core.exceptions import IndexingError
from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.archive import ExtractionManifest
from sourcetrace.ingestion.indexing import IndexingResult, RepositoryIndexingService
from sourcetrace.models.domain import CodeChunk, ParsedCodeChunk
from sourcetrace.parsers.python_ast import ParseResult


class FakeEmbeddingProvider:

    def __init__(
        self,
        model_identifier: str = "text-embedding-3-small",
        embedding_dimensions: int = 4,
        embedding_vectors: list[tuple[float, ...]] | None = None,
        should_raise: Exception | None = None,
        mutate_count: bool = False,
    ) -> None:
        self.model_identifier = model_identifier
        self.embedding_dimensions = embedding_dimensions
        self.embedding_vectors = embedding_vectors
        self.should_raise = should_raise
        self.mutate_count = mutate_count
        self.received_texts: list[str] = []
        self.embed_calls = 0

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.embed_calls += 1
        if self.should_raise:
            raise self.should_raise
        self.received_texts = list(texts)
        if self.embedding_vectors is not None:
            return self.embedding_vectors

        if self.mutate_count:
            return [tuple(0.1 for _ in range(self.embedding_dimensions))] * (len(texts) + 1)

        result: list[tuple[float, ...]] = []
        for i, _ in enumerate(texts):
            vec = tuple(
                float((i + 1) * 0.1 + j * 0.01)
                for j in range(self.embedding_dimensions)
            )
            result.append(vec)
        return result


class FakeCodeChunkRepository:

    def __init__(
        self,
        save_many_return: Any = "DEFAULT",
        should_raise: Exception | None = None,
    ) -> None:
        self.saved_chunks: list[CodeChunk] = []
        self.save_many_return = save_many_return
        self.should_raise = should_raise
        self.save_many_calls = 0

    def save_many(self, chunks: list[CodeChunk]) -> int:
        self.save_many_calls += 1
        if self.should_raise:
            raise self.should_raise
        self.saved_chunks.extend(chunks)
        if self.save_many_return != "DEFAULT":
            return self.save_many_return
        return len(chunks)

    def list_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> list[CodeChunk]:
        return [
            c for c in self.saved_chunks
            if c.owner_session_id == owner_session_id and c.repository_id == repository_id
        ]

    def search_vectors(
        self,
        owner_session_id: str,
        repository_id: str,
        query_vector: list[float],
        limit: int = 5,
    ) -> list[Any]:
        return []

    def delete_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> int:
        return 0


def _create_acquired_source(
    tmp_dir: Path,
    source_type: str = "zip",
    files: dict[str, str] | None = None,
) -> AcquiredSource:
    if files is not None:
        for rel_path, content in files.items():
            full_path = tmp_dir / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

    manifest = ExtractionManifest(
        file_count=len(files) if files else 0,
        total_extracted_bytes=100,
        relative_paths=tuple(files.keys()) if files else (),
    )
    return AcquiredSource(
        extraction_root=tmp_dir,
        manifest=manifest,
        source_type=source_type,
    )


# ---------------------------------------------------------------------------
# 1. Successful Integration Tests
# ---------------------------------------------------------------------------


def test_successful_zip_indexing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = (
            "@dec1\n"
            "@dec2\n"
            "class SampleClass:\n"
            "    @method_dec\n"
            "    def sample_method(self):\n"
            "        pass\n"
            "\n"
            "def top_func():\n"
            "    def nested_func():\n"
            "        pass\n"
            "    return nested_func\n"
        )
        source = _create_acquired_source(tmp_path, "zip", {"sample.py": code})

        provider = FakeEmbeddingProvider(embedding_dimensions=4)
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        fixed_now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
        result = service.index_acquired_source(
            acquired_source=source,
            owner_session_id="session_abc123",
            repository_id="repo_xyz789",
            now=fixed_now,
        )

        assert isinstance(result, IndexingResult)
        assert result.parsed_file_count == 1
        assert result.chunk_count == 4
        assert result.skipped_file_count == 0

        assert provider.received_texts == [c.content for c in repo.saved_chunks]
        assert repo.save_many_calls == 1
        assert len(repo.saved_chunks) == 4

        symbols = [c.symbol_name for c in repo.saved_chunks]
        assert symbols == [
            "SampleClass",
            "SampleClass.sample_method",
            "top_func",
            "top_func.nested_func",
        ]

        class_chunk = repo.saved_chunks[0]
        assert class_chunk.start_line == 1
        assert class_chunk.end_line == 6
        assert class_chunk.relative_path == "sample.py"
        assert class_chunk.owner_session_id == "session_abc123"
        assert class_chunk.repository_id == "repo_xyz789"
        assert class_chunk.created_at == fixed_now
        assert len(class_chunk.embedding) == 4


def test_successful_github_indexing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = "def github_func():\n    return 42\n"
        source = _create_acquired_source(
            tmp_path, "github", {"owner-repo-commit123/src/module.py": code}
        )

        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        result = service.index_acquired_source(
            acquired_source=source,
            owner_session_id="session_gh",
            repository_id="repo_gh",
        )

        assert result.parsed_file_count == 1
        assert result.chunk_count == 1
        assert repo.saved_chunks[0].relative_path == "src/module.py"
        assert "owner-repo-commit123" not in repo.saved_chunks[0].relative_path


def test_invalid_python_syntax() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        valid_code = "def ok():\n    pass\n"
        invalid_code = "def broken(:\n    pass\n"
        source = _create_acquired_source(
            tmp_path, "zip", {"good.py": valid_code, "bad.py": invalid_code}
        )

        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        result = service.index_acquired_source(
            acquired_source=source,
            owner_session_id="session_123",
            repository_id="repo_123",
        )

        assert result.parsed_file_count == 1
        assert result.skipped_file_count == 1
        assert result.chunk_count == 1
        assert len(repo.saved_chunks) == 1
        assert repo.saved_chunks[0].relative_path == "good.py"


def test_empty_or_no_python_repository() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = _create_acquired_source(tmp_path, "zip", {"README.md": "# Hello"})

        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        result = service.index_acquired_source(
            acquired_source=source,
            owner_session_id="session_123",
            repository_id="repo_123",
        )

        assert result.parsed_file_count == 0
        assert result.chunk_count == 0
        assert result.skipped_file_count == 1
        assert provider.embed_calls == 0
        assert repo.save_many_calls == 0


def test_determinism() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        code = "class Worker:\n    def do_work(self):\n        return True\n"
        source = _create_acquired_source(tmp_path, "zip", {"worker.py": code})

        fixed_now = datetime(2026, 7, 24, 15, 30, 0, tzinfo=UTC)

        provider1 = FakeEmbeddingProvider(embedding_dimensions=4)
        repo1 = FakeCodeChunkRepository()
        service1 = RepositoryIndexingService(provider=provider1, code_chunk_repo=repo1)

        res1 = service1.index_acquired_source(
            acquired_source=source,
            owner_session_id="session_det",
            repository_id="repo_det",
            now=fixed_now,
        )

        provider2 = FakeEmbeddingProvider(embedding_dimensions=4)
        repo2 = FakeCodeChunkRepository()
        service2 = RepositoryIndexingService(provider=provider2, code_chunk_repo=repo2)

        res2 = service2.index_acquired_source(
            acquired_source=source,
            owner_session_id="session_det",
            repository_id="repo_det",
            now=fixed_now,
        )

        assert res1 == res2
        assert len(repo1.saved_chunks) == len(repo2.saved_chunks)
        for c1, c2 in zip(repo1.saved_chunks, repo2.saved_chunks, strict=True):
            assert c1.chunk_id == c2.chunk_id
            assert c1.content_hash == c2.content_hash
            assert c1.embedding == c2.embedding
            assert c1.relative_path == c2.relative_path


# ---------------------------------------------------------------------------
# 2. Invocation Count Assertions
# ---------------------------------------------------------------------------


def test_invocation_counts_non_empty_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source_non_empty = _create_acquired_source(tmp_path, "zip", {"a.py": "def f(): pass\n"})

        parse_calls = 0
        orig_parse = sourcetrace.ingestion.indexing.parse_acquired_source

        def spy_parse(*args: Any, **kwargs: Any) -> Any:
            nonlocal parse_calls
            parse_calls += 1
            return orig_parse(*args, **kwargs)

        monkeypatch.setattr(sourcetrace.ingestion.indexing, "parse_acquired_source", spy_parse)

        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        # Non-empty execution
        res1 = service.index_acquired_source(source_non_empty, "sess", "repo")
        assert parse_calls == 1
        assert provider.embed_calls == 1
        assert repo.save_many_calls == 1
        assert res1.chunk_count == 1

        # Empty execution
        source_empty = _create_acquired_source(tmp_path, "zip", {"a.txt": "text"})
        res2 = service.index_acquired_source(source_empty, "sess", "repo")
        assert parse_calls == 2
        assert provider.embed_calls == 1  # Provider not called for empty input
        assert repo.save_many_calls == 1  # save_many not called for empty input
        assert res2.chunk_count == 0


# ---------------------------------------------------------------------------
# 3. Parse-Result Adversarial Failures
# ---------------------------------------------------------------------------


def test_parse_result_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = _create_acquired_source(tmp_path, "zip", {"app.py": "def main(): pass\n"})
        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        # 1. Parser raises exception containing secret and absolute path
        def raise_secret(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Secret sk-proj-123 failed at /etc/passwd in repo_secret_1")

        monkeypatch.setattr(sourcetrace.ingestion.indexing, "parse_acquired_source", raise_secret)
        with pytest.raises(IndexingError) as exc_info:
            service.index_acquired_source(source, "sess", "repo")
        assert str(exc_info.value) == "Indexing failed safely."
        assert "sk-proj-123" not in str(exc_info.value)
        assert repo.save_many_calls == 0

        # 2. Parser returns None
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing, "parse_acquired_source", lambda *a, **k: None
        )
        with pytest.raises(IndexingError) as exc_info:
            service.index_acquired_source(source, "sess", "repo")
        assert str(exc_info.value) == "Indexing failed safely."

        # 3. Parser returns non-ParseResult (e.g. dict)
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing,
            "parse_acquired_source",
            lambda *a, **k: {"chunks": ()},
        )
        with pytest.raises(IndexingError) as exc_info:
            service.index_acquired_source(source, "sess", "repo")
        assert str(exc_info.value) == "Indexing failed safely."

        # 4. Parser returns list instead of tuple chunks
        good_chunk = ParsedCodeChunk(
            chunk_id="chk_1",
            repository_id="repo",
            owner_session_id="sess",
            relative_path="app.py",
            language="python",
            symbol_name="main",
            symbol_type="function",
            start_line=1,
            end_line=1,
            content="def main(): pass\n",
            content_hash="hash1",
            parser_version="v1",
        )

        bad_res_list_chunks = ParseResult(
            chunks=[good_chunk],  # type: ignore
            parsed_file_count=1,
            skipped=(),
        )
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing,
            "parse_acquired_source",
            lambda *a, **k: bad_res_list_chunks,
        )
        with pytest.raises(IndexingError) as exc_info:
            service.index_acquired_source(source, "sess", "repo")
        assert str(exc_info.value) == "Indexing failed safely."

        # 5. Invalid parsed_file_count (bool, negative, float, string)
        for bad_count in (True, False, -1, 1.5, "1"):
            bad_res_count = ParseResult(
                chunks=(good_chunk,), parsed_file_count=bad_count, skipped=()  # type: ignore
            )

            def mock_count_func(*args: Any, target: Any = bad_res_count, **kwargs: Any) -> Any:
                return target

            monkeypatch.setattr(
                sourcetrace.ingestion.indexing, "parse_acquired_source", mock_count_func
            )
            with pytest.raises(IndexingError):
                service.index_acquired_source(source, "sess", "repo")

        # 6. Wrong owner or repository identity
        wrong_owner_chunk = replace(good_chunk, owner_session_id="other_owner")
        res_wrong_owner = ParseResult(
            chunks=(wrong_owner_chunk,), parsed_file_count=1, skipped=()
        )
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing,
            "parse_acquired_source",
            lambda *a, **k: res_wrong_owner,
        )
        with pytest.raises(IndexingError):
            service.index_acquired_source(source, "sess", "repo")

        # 7. Blank chunk ID or duplicate chunk IDs
        blank_id_chunk = replace(good_chunk, chunk_id="")
        res_blank_id = ParseResult(chunks=(blank_id_chunk,), parsed_file_count=1, skipped=())
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing,
            "parse_acquired_source",
            lambda *a, **k: res_blank_id,
        )
        with pytest.raises(IndexingError):
            service.index_acquired_source(source, "sess", "repo")

        dup_res = ParseResult(chunks=(good_chunk, good_chunk), parsed_file_count=1, skipped=())
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing,
            "parse_acquired_source",
            lambda *a, **k: dup_res,
        )
        with pytest.raises(IndexingError):
            service.index_acquired_source(source, "sess", "repo")

        # 8. Absolute or traversal relative path
        bad_paths = ("/etc/passwd", "C:\\Windows\\file.py", "../app.py", "src/../../etc/passwd")
        for bad_path in bad_paths:
            bad_path_chunk = replace(good_chunk, relative_path=bad_path)
            res_bad_path = ParseResult(chunks=(bad_path_chunk,), parsed_file_count=1, skipped=())

            def mock_path_func(*args: Any, target: Any = res_bad_path, **kwargs: Any) -> Any:
                return target

            monkeypatch.setattr(
                sourcetrace.ingestion.indexing, "parse_acquired_source", mock_path_func
            )
            with pytest.raises(IndexingError):
                service.index_acquired_source(source, "sess", "repo")

        # 9. Invalid line ranges
        line_ranges = ((0, 1), (-1, 5), (10, 5), (True, 5), (1, False))
        for s_line, e_line in line_ranges:
            bad_line_chunk = replace(good_chunk, start_line=s_line, end_line=e_line)  # type: ignore
            res_bad_line = ParseResult(chunks=(bad_line_chunk,), parsed_file_count=1, skipped=())

            def mock_line_func(*args: Any, target: Any = res_bad_line, **kwargs: Any) -> Any:
                return target

            monkeypatch.setattr(
                sourcetrace.ingestion.indexing, "parse_acquired_source", mock_line_func
            )
            with pytest.raises(IndexingError):
                service.index_acquired_source(source, "sess", "repo")


# ---------------------------------------------------------------------------
# 4. Exploding Property Objects Test
# ---------------------------------------------------------------------------


class ExplodingChunk:

    @property
    def owner_session_id(self) -> str:
        raise RuntimeError("Exploding secret API key: sk-proj-123456789")

    @property
    def repository_id(self) -> str:
        raise ValueError("Exploding Mongo URI: mongodb://user:secret@host/db")

    @property
    def chunk_id(self) -> str:
        raise AttributeError("Exploding path: /var/secrets/key.pem")


def test_exploding_property_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = _create_acquired_source(tmp_path, "zip", {"app.py": "def main(): pass\n"})
        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        exploding_res = ParseResult(
            chunks=(ExplodingChunk(),),  # type: ignore
            parsed_file_count=1,
            skipped=(),
        )

        monkeypatch.setattr(
            sourcetrace.ingestion.indexing,
            "parse_acquired_source",
            lambda *a, **k: exploding_res,
        )

        with pytest.raises(IndexingError) as exc_info:
            service.index_acquired_source(source, "sess", "repo")

        err_str = str(exc_info.value)
        assert err_str == "Indexing failed safely."
        assert "sk-proj-123456789" not in err_str
        assert "mongodb://" not in err_str
        assert "/var/secrets" not in err_str
        assert repo.save_many_calls == 0


# ---------------------------------------------------------------------------
# 5. Embedded-Output Adversarial Failures
# ---------------------------------------------------------------------------


def test_embedded_output_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = _create_acquired_source(tmp_path, "zip", {"app.py": "def main(): pass\n"})
        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        good_chunk = CodeChunk(
            chunk_id="chunk_1",
            repository_id="repo",
            owner_session_id="sess",
            relative_path="app.py",
            language="python",
            symbol_name="main",
            symbol_type="function",
            start_line=1,
            end_line=1,
            content="def main(): pass\n",
            content_hash="hash1",
            parser_version="v1",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=4,
            created_at=datetime.now(UTC),
            embedding=(0.1, 0.2, 0.3, 0.4),
        )

        # 1. embed_chunks returns None
        monkeypatch.setattr(sourcetrace.ingestion.indexing, "embed_chunks", lambda *a, **k: None)
        with pytest.raises(IndexingError) as exc_info:
            service.index_acquired_source(source, "sess", "repo")
        assert str(exc_info.value) == "Indexing failed safely."
        assert repo.save_many_calls == 0

        # 2. embed_chunks returns list instead of tuple
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing, "embed_chunks", lambda *a, **k: [good_chunk]
        )
        with pytest.raises(IndexingError):
            service.index_acquired_source(source, "sess", "repo")

        # 3. embed_chunks returns mutated chunk_id or owner or repository
        mutated_chunk = replace(good_chunk, chunk_id="chunk_mutated")
        monkeypatch.setattr(
            sourcetrace.ingestion.indexing, "embed_chunks", lambda *a, **k: (mutated_chunk,)
        )
        with pytest.raises(IndexingError):
            service.index_acquired_source(source, "sess", "repo")

        # 4. Invalid vector values (nan, inf, bool, string)
        bad_vals = (float("nan"), float("inf"), True, "0.1")
        for bad_val in bad_vals:
            bad_vec_chunk = replace(good_chunk, embedding=(0.1, 0.2, 0.3, bad_val))  # type: ignore

            def mock_vec_func(*args: Any, target: Any = bad_vec_chunk, **kwargs: Any) -> Any:
                return (target,)

            monkeypatch.setattr(
                sourcetrace.ingestion.indexing, "embed_chunks", mock_vec_func
            )
            with pytest.raises(IndexingError):
                service.index_acquired_source(source, "sess", "repo")


# ---------------------------------------------------------------------------
# 6. Repository-Result Failures
# ---------------------------------------------------------------------------


def test_repository_result_failures() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = _create_acquired_source(tmp_path, "zip", {"app.py": "def main(): pass\n"})
        provider = FakeEmbeddingProvider()

        # 1. Secret-bearing repo exception
        msg = "Database uri mongo://admin:secret123@db:27017 failed at /data/db"
        secret_exc = RuntimeError(msg)
        failing_repo = FakeCodeChunkRepository(should_raise=secret_exc)
        service1 = RepositoryIndexingService(provider=provider, code_chunk_repo=failing_repo)

        with pytest.raises(IndexingError) as exc_info:
            service1.index_acquired_source(source, "sess", "repo")

        err_str = str(exc_info.value)
        assert err_str == "Indexing failed safely."
        assert "secret123" not in err_str
        assert "mongo://" not in err_str

        # 2. Invalid accepted counts
        for bad_ret in (None, True, False, "1", 1.0, -1, 0, 2):
            bad_repo = FakeCodeChunkRepository(save_many_return=bad_ret)
            service_bad = RepositoryIndexingService(provider=provider, code_chunk_repo=bad_repo)
            with pytest.raises(IndexingError) as exc_info:
                service_bad.index_acquired_source(source, "sess", "repo")
            assert str(exc_info.value) == "Indexing failed safely."


# ---------------------------------------------------------------------------
# 7. Process-Control Exception Passthrough
# ---------------------------------------------------------------------------


def test_process_control_exception_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = _create_acquired_source(tmp_path, "zip", {"app.py": "def main(): pass\n"})
        provider = FakeEmbeddingProvider()
        repo = FakeCodeChunkRepository()
        service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

        # KeyboardInterrupt passthrough
        def raise_kb(*a: Any, **k: Any) -> Any:
            raise KeyboardInterrupt("Interrupted by user")

        monkeypatch.setattr(sourcetrace.ingestion.indexing, "parse_acquired_source", raise_kb)
        with pytest.raises(KeyboardInterrupt):
            service.index_acquired_source(source, "sess", "repo")

        # SystemExit passthrough
        def raise_sysexit(*a: Any, **k: Any) -> Any:
            raise SystemExit(1)

        monkeypatch.setattr(sourcetrace.ingestion.indexing, "parse_acquired_source", raise_sysexit)
        with pytest.raises(SystemExit):
            service.index_acquired_source(source, "sess", "repo")
