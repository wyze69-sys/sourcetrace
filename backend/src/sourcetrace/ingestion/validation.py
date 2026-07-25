"""Pure ingestion security validators.

All functions in this module are pure, offline, and side-effect free.
They perform no network I/O, file-system writes, database calls, or
subprocess execution.

Responsibilities
~~~~~~~~~~~~~~~~
* GitHub URL format and redirect-policy validation.
* IP address public-network policy checking.
* Archive member path traversal/safety validation.
"""

from __future__ import annotations

import ipaddress
import os
import posixpath
import re
from urllib.parse import urlparse

from sourcetrace.core.exceptions import (
    DisallowedRedirectError,
    InvalidRepositoryURLError,
    UnsafeArchiveMemberPathError,
    UnsafeNetworkAddressError,
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Matches canonical ``https://github.com/{owner}/{repo}``
# - owner/repo: 1+ chars of alphanumeric, hyphen, underscore, or dot
# - optional trailing slash only
_GITHUB_REPO_RE: re.Pattern[str] = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9\-_.]+)/"
    r"(?P<repo>[A-Za-z0-9\-_.]+)"
    r"/?$"
)

# Hosts that are acceptable at different stages
_SUBMIT_HOST: str = "github.com"
_REDIRECT_HOST: str = "codeload.github.com"

# ---------------------------------------------------------------------------
# GitHub URL validation
# ---------------------------------------------------------------------------


def validate_github_url(url: str) -> tuple[str, str]:
    """Validate a user-submitted GitHub repository URL.

    Returns
    -------
    tuple[str, str]
        ``(owner, repo)`` extracted from the URL.

    Raises
    ------
    InvalidRepositoryURLError
        When the URL does not match the required format or violates
        any security policy.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidRepositoryURLError(
            "Repository URL must be a non-empty string."
        )

    # Parse the URL
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001 – defensive against malformed input
        raise InvalidRepositoryURLError(
            "Repository URL is malformed and cannot be parsed."
        ) from None

    # --- Scheme ---
    if parsed.scheme != "https":
        raise InvalidRepositoryURLError(
            "Repository URL must use the HTTPS scheme."
        )

    # --- Credentials / userinfo ---
    if parsed.username or parsed.password:
        raise InvalidRepositoryURLError(
            "Repository URL must not contain credentials."
        )

    # --- Port ---
    # parsed.port raises ValueError on malformed ports like ":invalid".
    # Catch it so we never expose a raw ValueError to callers.
    try:
        has_port = parsed.port is not None
    except ValueError:
        raise InvalidRepositoryURLError(
            "Repository URL must not specify a port."
        ) from None
    if has_port:
        raise InvalidRepositoryURLError(
            "Repository URL must not specify a port."
        )

    # --- Host ---
    hostname = parsed.hostname or ""
    if hostname != _SUBMIT_HOST:
        raise InvalidRepositoryURLError(
            "Repository URL host must be github.com."
        )

    # --- Query / fragment ---
    if parsed.query:
        raise InvalidRepositoryURLError(
            "Repository URL must not contain query parameters."
        )
    if parsed.fragment:
        raise InvalidRepositoryURLError(
            "Repository URL must not contain a fragment."
        )

    # --- Path pattern ---
    match = _GITHUB_REPO_RE.match(url)
    if match is None:
        raise InvalidRepositoryURLError(
            "Repository URL must match https://github.com/{owner}/{repo}."
        )

    return match.group("owner"), match.group("repo")


# ---------------------------------------------------------------------------
# Redirect policy validation
# ---------------------------------------------------------------------------


def validate_redirect_url(url: str) -> None:
    """Validate a redirect destination during GitHub archive download.

    Allowed redirect hosts:
    * ``github.com``
    * ``codeload.github.com``

    All redirect URLs must use HTTPS.

    Raises
    ------
    DisallowedRedirectError
        When the redirect URL violates the allowlist.
    """
    if not isinstance(url, str) or not url.strip():
        raise DisallowedRedirectError(
            "Redirect URL must be a non-empty string."
        )

    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        raise DisallowedRedirectError(
            "Redirect URL is malformed and cannot be parsed."
        ) from None

    if parsed.scheme != "https":
        raise DisallowedRedirectError(
            "Redirect URL must use the HTTPS scheme."
        )

    # --- Credentials / userinfo ---
    if parsed.username or parsed.password:
        raise DisallowedRedirectError(
            "Redirect URL must not contain credentials."
        )

    # --- Port ---
    # parsed.port raises ValueError on malformed ports like ":invalid".
    # Catch it so we never expose a raw ValueError to callers.
    try:
        has_port = parsed.port is not None
    except ValueError:
        raise DisallowedRedirectError(
            "Redirect URL must not specify a port."
        ) from None
    if has_port:
        raise DisallowedRedirectError(
            "Redirect URL must not specify a port."
        )

    hostname = parsed.hostname or ""
    if hostname not in {_SUBMIT_HOST, _REDIRECT_HOST}:
        raise DisallowedRedirectError(
            "Redirect target host is not in the allowlist."
        )


# ---------------------------------------------------------------------------
# Network address safety
# ---------------------------------------------------------------------------

# Carrier-grade NAT (RFC 6598): 100.64.0.0/10
_CARRIER_GRADE_NAT = ipaddress.IPv4Network("100.64.0.0/10")


def validate_ip_address(ip_str: str) -> None:
    """Reject non-public IP addresses.

    This function validates an already-resolved IP string.  It does **not**
    perform DNS resolution itself.

    Raises
    ------
    UnsafeNetworkAddressError
        When the address is loopback, private, link-local, multicast,
        reserved, unspecified, or falls into carrier-grade NAT ranges.
    """
    if not isinstance(ip_str, str) or not ip_str.strip():
        raise UnsafeNetworkAddressError(
            "IP address must be a non-empty string."
        )

    try:
        addr = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        raise UnsafeNetworkAddressError(
            "IP address is not a valid IPv4 or IPv6 address."
        ) from None

    if addr.is_loopback:
        raise UnsafeNetworkAddressError(
            "Loopback addresses are not allowed."
        )

    if addr.is_unspecified:
        raise UnsafeNetworkAddressError(
            "Unspecified addresses are not allowed."
        )

    if addr.is_link_local:
        raise UnsafeNetworkAddressError(
            "Link-local addresses are not allowed."
        )

    if addr.is_multicast:
        raise UnsafeNetworkAddressError(
            "Multicast addresses are not allowed."
        )

    if addr.is_reserved:
        raise UnsafeNetworkAddressError(
            "Reserved addresses are not allowed."
        )

    if addr.is_private:
        raise UnsafeNetworkAddressError(
            "Private network addresses are not allowed."
        )

    # Carrier-grade NAT (100.64.0.0/10) — is_private already covers
    # standard RFC 1918 ranges, but carrier-grade NAT is technically
    # "shared address space" and may not be flagged by is_private on
    # older Python versions.  Explicit check here.
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CARRIER_GRADE_NAT:
        raise UnsafeNetworkAddressError(
            "Carrier-grade NAT addresses are not allowed."
        )

    # --- Final public-routability guard ---
    # Reject any address that is not globally routable.  This catches
    # benchmarking ranges (198.18.0.0/15), documentation ranges
    # (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), IPv4-mapped
    # IPv6 addresses wrapping private IPv4, and any other non-global
    # address that individual checks above may have missed.
    if not addr.is_global:
        raise UnsafeNetworkAddressError(
            "Only globally routable addresses are allowed."
        )


# ---------------------------------------------------------------------------
# Archive member path safety
# ---------------------------------------------------------------------------


def validate_archive_member_path(member_path: str) -> str:
    """Validate a path extracted from an archive member.

    Returns
    -------
    str
        The POSIX-normalised, safe, relative path.

    Raises
    ------
    UnsafeArchiveMemberPathError
        When the path contains traversal components, absolute paths,
        Windows drive letters, UNC paths, device paths, NUL bytes, or
        normalises to empty/unsafe values.
    """
    if not isinstance(member_path, str):
        raise UnsafeArchiveMemberPathError(
            "Archive member path must be a string."
        )

    # --- NUL bytes ---
    if "\x00" in member_path:
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not contain NUL bytes."
        )

    # --- Empty ---
    if not member_path.strip():
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not be empty."
        )

    # --- Traversal patterns ---
    # Check both forward and backward slash variants before normalisation
    # so that double-encoding or mixed slashes cannot bypass the check.
    normalised_for_check = member_path.replace("\\", "/")
    parts = normalised_for_check.split("/")

    for part in parts:
        if part == "..":
            raise UnsafeArchiveMemberPathError(
                "Archive member path must not contain directory traversal."
            )

    # --- Windows device paths ---
    # e.g. \\.\COM1, \\?\Volume
    # Must check before UNC because device paths also start with \\\\
    if member_path.startswith("\\\\.") or member_path.startswith("\\\\?"):
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not be a device path."
        )

    # --- UNC paths ---
    if member_path.startswith("\\\\"):
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not be a UNC path."
        )

    # --- Absolute Windows paths (drive letters) ---
    # e.g. C:\foo, C:/foo, c:\foo
    if len(member_path) >= 2 and member_path[1] == ":":
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not contain a drive letter."
        )

    # --- UNC paths with forward slashes ---
    if member_path.startswith("//"):
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not be a UNC path."
        )

    # --- Absolute POSIX paths ---
    if member_path.startswith("/"):
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not be an absolute POSIX path."
        )

    # --- Normalise and final safety check ---
    safe_path = posixpath.normpath(normalised_for_check)

    # normpath("") returns ".", which is not a valid member path
    if safe_path in {".", ""}:
        raise UnsafeArchiveMemberPathError(
            "Archive member path normalises to an empty or root-only value."
        )

    # normpath may collapse "../" into a leading ".." if the traversal
    # was not caught by the earlier split (unlikely but defensive).
    if safe_path.startswith(".."):
        raise UnsafeArchiveMemberPathError(
            "Archive member path must not contain directory traversal."
        )

    # Absolute after normalisation (defensive)
    if os.path.isabs(safe_path):
        raise UnsafeArchiveMemberPathError(
            "Archive member path must be relative."
        )

    return safe_path
