"""Offline adversarial tests for public GitHub archive downloads."""

from __future__ import annotations

import io
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from sourcetrace.core.exceptions import (
    ArchiveDownloadError,
    DisallowedRedirectError,
    IngestionLimitError,
    InvalidRepositoryURLError,
    UnsafeNetworkAddressError,
)
from sourcetrace.ingestion.github_archive import (
    DownloadedArchiveResult,
    safe_download_github_archive,
)
from sourcetrace.ingestion.limits import MAX_GITHUB_ARCHIVE_BYTES

# Public IPv4 address for mocking DNS resolution (github.com)
_SAFE_PUBLIC_IP: str = "140.82.121.4"


def _safe_resolver(hostname: str) -> list[str]:
    """Offline DNS resolver returning a valid public IP."""
    return [_SAFE_PUBLIC_IP]


def _make_sample_zip() -> bytes:
    """Helper to create a small valid zip bytes stream."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.md", "# Test Repo")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Valid GitHub archive download with redirect
# ---------------------------------------------------------------------------


def test_valid_github_archive_download_with_redirect(tmp_path: Path) -> None:
    """Proves valid download following github.com -> codeload.github.com redirect."""
    zip_bytes = _make_sample_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == "https://github.com/owner/repo/archive/HEAD.zip":
            return httpx.Response(
                302,
                headers={"Location": "https://codeload.github.com/owner/repo/zip/refs/heads/main"},
            )
        elif url_str == "https://codeload.github.com/owner/repo/zip/refs/heads/main":
            return httpx.Response(
                200,
                content=zip_bytes,
                headers={"Content-Type": "application/zip"},
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with safe_download_github_archive(
        "https://github.com/owner/repo",
        parent_dir=tmp_path,
        client=client,
        resolver=_safe_resolver,
    ) as res:
        assert isinstance(res, DownloadedArchiveResult)
        assert res.owner == "owner"
        assert res.repo == "repo"
        assert res.content_length == len(zip_bytes)
        assert res.archive_path.exists()
        assert res.archive_path.read_bytes() == zip_bytes
        # Path is inside parent_dir
        assert res.archive_path.parent.parent.resolve() == tmp_path.resolve()

    # Cleanup after exit
    assert not res.archive_path.exists()


# ---------------------------------------------------------------------------
# 2. Zero HTTP calls on URL or SSRF validation failure
# ---------------------------------------------------------------------------


def test_invalid_source_url_makes_zero_http_calls() -> None:
    """Proves malformed or non-HTTPS URL raises error with zero HTTP requests."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(InvalidRepositoryURLError):
        with safe_download_github_archive(
            "http://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass

    assert not called


def test_private_address_makes_zero_http_calls() -> None:
    """Proves private IP raises UnsafeNetworkAddressError with zero HTTP calls."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def private_resolver(hostname: str) -> list[str]:
        return ["127.0.0.1", "192.168.1.1"]

    with pytest.raises(UnsafeNetworkAddressError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=private_resolver,
        ):
            pass

    assert not called


# ---------------------------------------------------------------------------
# 3. Redirect Controls
# ---------------------------------------------------------------------------


def test_disallowed_redirect_makes_no_second_http_call() -> None:
    """Proves non-allowlisted redirect target halts with zero additional HTTP calls."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(302, headers={"Location": "https://evil.com/malicious.zip"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(DisallowedRedirectError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass

    assert request_count == 1


def test_relative_redirect_handling() -> None:
    """Proves relative Location headers are resolved against current URL."""
    zip_bytes = _make_sample_zip()
    urls_visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        urls_visited.append(url_str)
        if url_str == "https://github.com/owner/repo/archive/HEAD.zip":
            return httpx.Response(302, headers={"Location": "/owner/repo/archive/v1.0.zip"})
        elif url_str == "https://github.com/owner/repo/archive/v1.0.zip":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with safe_download_github_archive(
        "https://github.com/owner/repo",
        client=client,
        resolver=_safe_resolver,
    ) as res:
        assert res.content_length == len(zip_bytes)

    assert urls_visited == [
        "https://github.com/owner/repo/archive/HEAD.zip",
        "https://github.com/owner/repo/archive/v1.0.zip",
    ]


def test_missing_location_header() -> None:
    """Proves 302 response missing Location header raises DisallowedRedirectError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(DisallowedRedirectError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass


def test_redirect_loop_and_limit_rejection() -> None:
    """Proves redirect loop or > 5 redirects raises DisallowedRedirectError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://github.com/owner/repo/archive/HEAD.zip"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(DisallowedRedirectError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass


# ---------------------------------------------------------------------------
# 4. Response & Streaming Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_cl", ["invalid", "-500"])
def test_malformed_and_negative_content_length(bad_cl: str) -> None:
    """Proves malformed or negative Content-Length header raises ArchiveDownloadError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data", headers={"Content-Length": bad_cl})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArchiveDownloadError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass


def test_oversized_content_length_header() -> None:
    """Proves Content-Length header exceeding limit raises IngestionLimitError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(MAX_GITHUB_ARCHIVE_BYTES + 100)},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass


def test_streaming_body_exceeds_limit_despite_small_header() -> None:
    """Proves stream exceeding MAX_GITHUB_ARCHIVE_BYTES raises limit error and cleans up."""
    def handler(request: httpx.Request) -> httpx.Response:
        huge_data = b"X" * (MAX_GITHUB_ARCHIVE_BYTES + 500)
        return httpx.Response(200, content=huge_data, headers={"Content-Length": "100"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    temp_dirs_before = set(Path(tempfile.gettempdir()).glob("sourcetrace_github_*"))

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass

    temp_dirs_after = set(Path(tempfile.gettempdir()).glob("sourcetrace_github_*"))
    assert temp_dirs_after.issubset(temp_dirs_before)


def test_unexpected_content_type() -> None:
    """Proves non-ZIP Content-Type raises ArchiveDownloadError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>error</html>",
            headers={"Content-Type": "text/html"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArchiveDownloadError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass


def test_timeout_and_transport_error() -> None:
    """Proves httpx timeout/transport exception raises safe ArchiveDownloadError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Network timed out")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArchiveDownloadError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass


def test_non_200_response() -> None:
    """Proves HTTP 404/500 response raises safe ArchiveDownloadError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"Not Found")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArchiveDownloadError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass


# ---------------------------------------------------------------------------
# 5. Cleanup Invariants & Error Safety
# ---------------------------------------------------------------------------


def test_cleanup_after_caller_exception() -> None:
    """Proves temporary download directory is removed if caller raises inside context."""
    zip_bytes = _make_sample_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=zip_bytes)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloaded_path: Path | None = None

    class CustomCallerError(Exception):
        pass

    with pytest.raises(CustomCallerError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ) as res:
            downloaded_path = res.archive_path
            assert downloaded_path.exists()
            raise CustomCallerError("Caller code failed inside context")

    assert downloaded_path is not None
    assert not downloaded_path.exists()


def test_no_raw_leaks_in_domain_errors() -> None:
    """Proves error messages do not leak internal URLs, IPs, local paths, headers, or bodies."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"Internal Secret Stack Trace Server Error")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ArchiveDownloadError) as exc_info:
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=client,
            resolver=_safe_resolver,
        ):
            pass

    err_msg = str(exc_info.value)
    assert err_msg == "Archive download failed safely."
    assert "https://" not in err_msg
    assert "github.com" not in err_msg
    assert "140.82.121.4" not in err_msg
    assert "Internal Secret" not in err_msg
    assert "sourcetrace_github_" not in err_msg


# ---------------------------------------------------------------------------
# 6. Streaming Method & Response-Close Regression Tests
# ---------------------------------------------------------------------------


def test_downloader_uses_client_stream_not_get() -> None:
    """Proves safe_download_github_archive calls client.stream and not client.get."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/zip", "Content-Length": "10"}
    mock_response.iter_bytes.return_value = [b"0123456789"]

    mock_client.stream.return_value.__enter__.return_value = mock_response

    with safe_download_github_archive(
        "https://github.com/owner/repo",
        client=mock_client,
        resolver=_safe_resolver,
    ) as res:
        assert res.content_length == 10

    mock_client.get.assert_not_called()
    mock_client.stream.assert_called()


def test_incremental_streaming_and_early_abort_on_limit() -> None:
    """Proves chunks are consumed incrementally and reading stops when limit is crossed."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {}

    chunk1 = b"A" * (MAX_GITHUB_ARCHIVE_BYTES - 100)
    chunk2 = b"B" * 500  # Crosses limit
    chunk3 = b"C" * 100  # Should never be read

    chunks_yielded: list[bytes] = []

    def chunk_generator() -> Iterator[bytes]:
        for c in [chunk1, chunk2, chunk3]:
            chunks_yielded.append(c)
            yield c

    mock_response.iter_bytes.return_value = chunk_generator()
    mock_client.stream.return_value.__enter__.return_value = mock_response

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=mock_client,
            resolver=_safe_resolver,
        ):
            pass

    assert chunk1 in chunks_yielded
    assert chunk2 in chunks_yielded
    assert chunk3 not in chunks_yielded


def test_redirect_and_final_responses_are_closed() -> None:
    """Proves response context enter/exit is called for redirects and final responses."""
    redirect_response = MagicMock(spec=httpx.Response)
    redirect_response.status_code = 302
    redirect_response.headers = {
        "Location": "https://codeload.github.com/owner/repo/zip/refs/heads/main"
    }

    success_response = MagicMock(spec=httpx.Response)
    success_response.status_code = 200
    success_response.headers = {"Content-Type": "application/zip"}
    success_response.iter_bytes.return_value = [b"zipdata"]

    ctx1 = MagicMock()
    ctx1.__enter__.return_value = redirect_response
    ctx1.__exit__.return_value = None

    ctx2 = MagicMock()
    ctx2.__enter__.return_value = success_response
    ctx2.__exit__.return_value = None

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.stream.side_effect = [ctx1, ctx2]

    with safe_download_github_archive(
        "https://github.com/owner/repo",
        client=mock_client,
        resolver=_safe_resolver,
    ) as res:
        assert res.content_length == 7

    ctx1.__exit__.assert_called_once()
    ctx2.__exit__.assert_called_once()


def test_final_response_closed_on_ingestion_limit_error() -> None:
    """Proves response context exit (__exit__) is called when IngestionLimitError occurs."""
    error_response = MagicMock(spec=httpx.Response)
    error_response.status_code = 200
    error_response.headers = {"Content-Length": str(MAX_GITHUB_ARCHIVE_BYTES + 1000)}

    ctx = MagicMock()
    ctx.__enter__.return_value = error_response
    ctx.__exit__.return_value = None

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.stream.return_value = ctx

    with pytest.raises(IngestionLimitError):
        with safe_download_github_archive(
            "https://github.com/owner/repo",
            client=mock_client,
            resolver=_safe_resolver,
        ):
            pass

    ctx.__exit__.assert_called_once()
