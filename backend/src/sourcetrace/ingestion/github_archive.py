"""Safe public GitHub archive download module.

Responsibilities
----------------
- Validate submitted GitHub repository URLs (scheme, host, format, credentials, ports).
- Construct canonical archive target URLs (https://github.com/{owner}/{repo}/archive/HEAD.zip).
- Enforce pre-request DNS resolution & SSRF IP safety (globally routable addresses only).
- Enforce strict host redirect policy (github.com submit host, codeload.github.com redirect target).
- Enforce manual HTTP redirect handling (max 5 redirects, HTTPS only, no credentials/ports).
- Enforce download timeout (DOWNLOAD_TIMEOUT_SECONDS) and response size limit
  (MAX_GITHUB_ARCHIVE_BYTES).
- Stream response body via httpx.Client.stream() with Content-Type and byte-limit checks.
- Manage isolated temporary directory lifecycle (sourcetrace_github_*) with guaranteed cleanup.
- Ensure error messages are safe and never leak internal paths, URLs, IPs, or secrets.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from sourcetrace.core.exceptions import (
    ArchiveDownloadError,
    DisallowedRedirectError,
    IngestionLimitError,
    InvalidRepositoryURLError,
    UnsafeNetworkAddressError,
)
from sourcetrace.ingestion.limits import (
    DOWNLOAD_TIMEOUT_SECONDS,
    MAX_GITHUB_ARCHIVE_BYTES,
)
from sourcetrace.ingestion.validation import (
    validate_github_url,
    validate_ip_address,
    validate_redirect_url,
)

# Stream read chunk size (64 KiB)
_STREAM_CHUNK_SIZE: int = 64 * 1024

# Allowed Content-Type media types for ZIP archives
_ALLOWED_MEDIA_TYPES: set[str] = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
    "binary/octet-stream",
}


@dataclass(frozen=True)
class DownloadedArchiveResult:
    """Internal download metadata yielded inside the context manager."""

    archive_path: Path
    content_length: int
    owner: str
    repo: str


def _default_dns_resolver(hostname: str) -> list[str]:
    """Resolve hostname to a list of IP address strings."""
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ips = list({info[4][0] for info in infos if info[4]})
        if not ips:
            raise UnsafeNetworkAddressError("Network address is disallowed.")
        return ips
    except UnsafeNetworkAddressError:
        raise
    except Exception as err:
        raise UnsafeNetworkAddressError("Network address is disallowed.") from err


def _validate_target_url_and_ip(
    url: str,
    is_redirect: bool,
    resolver: Callable[[str], list[str]],
) -> str:
    """Validate URL scheme, host allowlist, credentials, ports, and resolved IP addresses.

    Returns
    -------
    str
        The validated hostname.
    """
    try:
        parsed = urlparse(url)
    except Exception as err:
        if is_redirect:
            raise DisallowedRedirectError("Redirect target is disallowed.") from err
        raise InvalidRepositoryURLError("Repository URL is invalid.") from err

    if parsed.scheme != "https":
        if is_redirect:
            raise DisallowedRedirectError("Redirect target is disallowed.")
        raise InvalidRepositoryURLError("Repository URL is invalid.")

    if parsed.username or parsed.password:
        if is_redirect:
            raise DisallowedRedirectError("Redirect target is disallowed.")
        raise InvalidRepositoryURLError("Repository URL is invalid.")

    try:
        has_port = parsed.port is not None
    except ValueError as err:
        if is_redirect:
            raise DisallowedRedirectError("Redirect target is disallowed.") from err
        raise InvalidRepositoryURLError("Repository URL is invalid.") from err

    if has_port:
        if is_redirect:
            raise DisallowedRedirectError("Redirect target is disallowed.")
        raise InvalidRepositoryURLError("Repository URL is invalid.")

    hostname = parsed.hostname or ""
    if not hostname:
        if is_redirect:
            raise DisallowedRedirectError("Redirect target is disallowed.")
        raise InvalidRepositoryURLError("Repository URL is invalid.")

    if not is_redirect:
        if hostname != "github.com":
            raise InvalidRepositoryURLError("Repository URL is invalid.")
    else:
        if hostname not in {"github.com", "codeload.github.com"}:
            raise DisallowedRedirectError("Redirect target is disallowed.")

    # DNS Resolution and SSRF IP check
    ips = resolver(hostname)
    if not ips:
        raise UnsafeNetworkAddressError("Network address is disallowed.")

    for ip_str in ips:
        validate_ip_address(ip_str)

    return hostname


@contextmanager
def safe_download_github_archive(
    url: str,
    parent_dir: str | Path | None = None,
    client: httpx.Client | None = None,
    resolver: Callable[[str], list[str]] | None = None,
) -> Iterator[DownloadedArchiveResult]:
    """Managed context manager for safe public GitHub repository archive downloads.

    Validates URL, resolves DNS, checks SSRF IP rules, enforces host allowlists
    and manual redirect limits, streams response chunks under byte size limits via
    httpx.Client.stream(), yields internal download metadata, and guarantees cleanup.

    Parameters
    ----------
    url : str
        User-submitted public GitHub repository URL.
    parent_dir : str | Path | None
        Optional parent directory under which the unique extraction folder is created.
    client : httpx.Client | None
        Optional HTTP client (used for dependency injection in offline unit tests).
    resolver : Callable[[str], list[str]] | None
        Optional DNS resolver (used for dependency injection in offline unit tests).

    Yields
    ------
    DownloadedArchiveResult
        Internal result object containing archive_path, content_length, owner, repo.
    """
    dns_resolver = resolver or _default_dns_resolver

    # 1. Validate initial repository URL format & extract owner/repo
    owner, repo = validate_github_url(url)

    # 2. Construct canonical archive URL
    canonical_url = f"https://github.com/{owner}/{repo}/archive/HEAD.zip"

    # 3. Create isolated unique temporary directory
    if parent_dir is not None:
        parent_path = Path(parent_dir)
        if not parent_path.exists() or not parent_path.is_dir():
            raise ArchiveDownloadError("Archive download failed safely.")
        temp_dir_str = tempfile.mkdtemp(prefix="sourcetrace_github_", dir=str(parent_path))
    else:
        temp_dir_str = tempfile.mkdtemp(prefix="sourcetrace_github_")

    target_root = Path(temp_dir_str).resolve()
    archive_file_path = (target_root / "source.zip").resolve()

    own_client = client is None
    http_client = client or httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS),
    )

    try:
        downloaded_bytes = 0
        current_url = canonical_url
        redirect_count = 0
        seen_urls = {current_url}

        while True:
            # Validate target host policy & resolved IP addresses before request
            _validate_target_url_and_ip(
                current_url,
                is_redirect=(redirect_count > 0),
                resolver=dns_resolver,
            )

            try:
                with http_client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": "SourceTrace/1.0"},
                    follow_redirects=False,
                ) as response:
                    status_code = response.status_code

                    # Handle redirects
                    if status_code in (301, 302, 303, 307, 308):
                        redirect_count += 1
                        if redirect_count > 5:
                            raise DisallowedRedirectError("Redirect target is disallowed.")

                        location = response.headers.get("Location")
                        if not location or not location.strip():
                            raise DisallowedRedirectError("Redirect target is disallowed.")

                        next_url = urljoin(current_url, location.strip())
                        validate_redirect_url(next_url)

                        if next_url in seen_urls:
                            raise DisallowedRedirectError("Redirect target is disallowed.")
                        seen_urls.add(next_url)

                        current_url = next_url
                        # Exiting 'with response' automatically closes the redirect response
                        continue

                    elif status_code == 200:
                        # Validate Content-Type if present
                        content_type_hdr = response.headers.get("Content-Type", "").strip()
                        if content_type_hdr:
                            media_type = content_type_hdr.split(";")[0].strip().lower()
                            if media_type not in _ALLOWED_MEDIA_TYPES:
                                raise ArchiveDownloadError("Archive download failed safely.")

                        # Validate Content-Length if present
                        content_len_hdr = response.headers.get("Content-Length", "").strip()
                        if content_len_hdr:
                            try:
                                cl_val = int(content_len_hdr)
                            except ValueError as err:
                                raise ArchiveDownloadError(
                                    "Archive download failed safely."
                                ) from err

                            if cl_val < 0:
                                raise ArchiveDownloadError("Archive download failed safely.")
                            if cl_val > MAX_GITHUB_ARCHIVE_BYTES:
                                raise IngestionLimitError(
                                    "Archive member exceeds an ingestion limit."
                                )

                        # Stream response body to target file with limit checks
                        try:
                            with open(archive_file_path, "wb") as dst:
                                for chunk in response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
                                    if not chunk:
                                        continue
                                    chunk_len = len(chunk)
                                    downloaded_bytes += chunk_len

                                    if downloaded_bytes > MAX_GITHUB_ARCHIVE_BYTES:
                                        raise IngestionLimitError(
                                            "Archive member exceeds an ingestion limit."
                                        )
                                    dst.write(chunk)
                        except (IngestionLimitError, ArchiveDownloadError):
                            raise
                        except Exception as err:
                            raise ArchiveDownloadError("Archive download failed safely.") from err

                        if downloaded_bytes == 0:
                            raise ArchiveDownloadError("Archive download failed safely.")

                        break

                    else:
                        # Non-200, non-redirect status code
                        raise ArchiveDownloadError("Archive download failed safely.")

            except (
                DisallowedRedirectError,
                InvalidRepositoryURLError,
                UnsafeNetworkAddressError,
                IngestionLimitError,
                ArchiveDownloadError,
            ):
                raise
            except (httpx.HTTPError, OSError, ValueError) as err:
                raise ArchiveDownloadError("Archive download failed safely.") from err
            except Exception as err:
                raise ArchiveDownloadError("Archive download failed safely.") from err

        yield DownloadedArchiveResult(
            archive_path=archive_file_path,
            content_length=downloaded_bytes,
            owner=owner,
            repo=repo,
        )

    finally:
        if own_client:
            try:
                http_client.close()
            except Exception:  # noqa: BLE001
                pass
        # Guaranteed cleanup of isolated directory
        shutil.rmtree(target_root, ignore_errors=True)
