"""Focused mocked unit tests for REPO-001 Phase 3 initial GitHub import metadata capture."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.archive import ExtractionManifest
from sourcetrace.ingestion.github_archive import (
    resolve_github_ref_metadata,
    safe_download_github_archive,
)
from sourcetrace.ingestion.lifecycle import IndexingLifecycleCoordinator
from sourcetrace.models.domain import IndexingJobRecord, RepositoryRecord
from sourcetrace.parsers.flow_evidence import is_flow_evidence_complete


def _safe_resolver(hostname: str) -> list[str]:
    return ["140.82.121.4"]


def test_resolve_github_ref_metadata_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == "https://api.github.com/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if url_str == "https://api.github.com/repos/owner/repo/commits/main":
            return httpx.Response(200, json={"sha": "1234567890abcdef1234567890abcdef12345678"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    meta = resolve_github_ref_metadata("owner", "repo", client=client, resolver=_safe_resolver)

    assert meta.default_branch == "main"
    assert meta.commit_sha == "1234567890abcdef1234567890abcdef12345678"


def test_resolve_github_ref_metadata_rate_limit_or_network_failure_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "API rate limit exceeded"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    meta = resolve_github_ref_metadata("owner", "repo", client=client, resolver=_safe_resolver)

    assert meta.default_branch is None
    assert meta.commit_sha is None


def test_safe_download_github_archive_uses_exact_sha(tmp_path: Path) -> None:
    zip_bytes = b"PK\x05\x06" + b"\x00" * 18  # Minimal valid empty zip header

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == "https://api.github.com/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if url_str == "https://api.github.com/repos/owner/repo/commits/main":
            return httpx.Response(200, json={"sha": "fedcba9876543210fedcba9876543210fedcba98"})
        if "archive/fedcba9876543210fedcba9876543210fedcba98.zip" in url_str:
            return httpx.Response(
                200, content=zip_bytes, headers={"Content-Type": "application/zip"}
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with safe_download_github_archive(
        "https://github.com/owner/repo",
        parent_dir=tmp_path,
        client=client,
        resolver=_safe_resolver,
    ) as result:
        assert result.resolved_branch == "main"
        assert result.resolved_commit_sha == "fedcba9876543210fedcba9876543210fedcba98"
        assert result.archive_path.exists()


def test_safe_download_github_archive_fallback_to_head_on_api_error(tmp_path: Path) -> None:
    zip_bytes = b"PK\x05\x06" + b"\x00" * 18

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.startswith("https://api.github.com/"):
            return httpx.Response(500)
        if url_str == "https://github.com/owner/repo/archive/HEAD.zip":
            return httpx.Response(
                200, content=zip_bytes, headers={"Content-Type": "application/zip"}
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with safe_download_github_archive(
        "https://github.com/owner/repo",
        parent_dir=tmp_path,
        client=client,
        resolver=_safe_resolver,
    ) as result:
        assert result.resolved_branch is None
        assert result.resolved_commit_sha is None
        assert result.archive_path.exists()


@pytest.mark.parametrize(
    ("parser_versions", "expected"),
    [
        (("python-ast-v3",), True),
        (("js-ts-treesitter-v3",), True),
        (("python-ast-v3", "js-ts-treesitter-v3"), True),
        (("python-ast-v2",), True),
        (("js-ts-treesitter-v2",), True),
        (("python-ast-v1",), False),
        (("js-ts-v1",), False),
        (("python-ast-v3", "python-ast-v1"), False),
        (("js-ts-treesitter-v3", "unsupported-parser"), False),
        ((), False),
    ],
)
def test_flow_evidence_complete_derivation_rules(
    parser_versions: tuple[str, ...], expected: bool
) -> None:
    assert is_flow_evidence_complete(parser_versions) is expected


def test_indexing_lifecycle_persists_initial_freshness_metadata(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    mock_repo = MagicMock()
    mock_job_repo = MagicMock()
    mock_indexing_service = MagicMock()

    mock_job_repo.transition_status.return_value = IndexingJobRecord(
        job_id="job_1",
        repository_id="repo_1",
        owner_session_id="sess_1",
        status="storing",
        current_step="Storing repository index",
        created_at=now,
        updated_at=now,
    )
    mock_repo.transition_status.return_value = RepositoryRecord(
        repository_id="repo_1",
        owner_session_id="sess_1",
        name="repo_1",
        source_type="github",
        status="ready",
        created_at=now,
        updated_at=now,
        active_generation_id=None,
        indexed_branch="main",
        indexed_commit_sha="sha_abc_123",
        last_indexed_at=now,
        parser_versions=("python-ast-v3",),
        flow_evidence_complete=True,
        indexed_file_count=3,
        indexed_chunk_count=7,
    )

    coordinator = IndexingLifecycleCoordinator(
        repository_repo=mock_repo,
        job_repo=mock_job_repo,
        indexing_service=mock_indexing_service,
        owner_session_id="sess_1",
        repository_id="repo_1",
        job_id="job_1",
        now=now,
    )

    manifest = ExtractionManifest(
        file_count=1,
        total_extracted_bytes=10,
        relative_paths=("a.py",),
    )
    source = AcquiredSource(
        extraction_root=tmp_path,
        manifest=manifest,
        source_type="github",
        resolved_branch="main",
        resolved_commit_sha="sha_abc_123",
    )

    def fake_index_source(*args, **kwargs):
        observer = kwargs.get("observer")
        if observer:
            observer.parsing_started()
            observer.storing_started()
            res = MagicMock()
            res.parsed_file_count = 3
            res.chunk_count = 7
            res.parser_versions = ("python-ast-v3",)
            observer.completed(res)

    mock_indexing_service.index_acquired_source.side_effect = fake_index_source

    coordinator.consume(source)

    # Verify transition_status call to repository_repo
    transition_call = mock_repo.transition_status.call_args
    assert transition_call is not None
    kwargs = transition_call.kwargs
    assert kwargs["new_status"] == "ready"
    assert kwargs["indexed_branch"] == "main"
    assert kwargs["indexed_commit_sha"] == "sha_abc_123"
    assert kwargs["last_indexed_at"] == now
    assert kwargs["parser_versions"] == ("python-ast-v3",)
    assert kwargs["flow_evidence_complete"] is True
    assert kwargs["indexed_file_count"] == 3
    assert kwargs["indexed_chunk_count"] == 7
