"""Focused offline tests for ingestion security primitives.

These tests cover:
  1. Immutable ingestion limit constants
  2. GitHub URL validation (format, scheme, host, path, credentials, etc.)
  3. Redirect policy validation
  4. IP address safety (SSRF protection)
  5. Archive member path safety (traversal, absolute, UNC, device, NUL, etc.)
  6. Typed ingestion security exception hierarchy

No network calls, file I/O, database connections, or subprocesses.
"""

from __future__ import annotations

import pytest

from sourcetrace.core.exceptions import (
    DisallowedRedirectError,
    IngestionLimitError,
    InvalidRepositoryURLError,
    SourceTraceError,
    UnsafeArchiveMemberPathError,
    UnsafeNetworkAddressError,
)
from sourcetrace.ingestion.limits import (
    DOWNLOAD_TIMEOUT_SECONDS,
    MAX_COMPRESSED_ZIP_BYTES,
    MAX_COMPRESSION_RATIO,
    MAX_EXTRACTED_ZIP_BYTES,
    MAX_FILES,
    MAX_GITHUB_ARCHIVE_BYTES,
    MAX_SINGLE_FILE_BYTES,
)
from sourcetrace.ingestion.validation import (
    validate_archive_member_path,
    validate_github_url,
    validate_ip_address,
    validate_redirect_url,
)

# =========================================================================
# 1. Ingestion limit constants
# =========================================================================


class TestIngestionLimits:
    """Verify exact immutable limit values."""

    def test_max_compressed_zip_bytes(self) -> None:
        assert MAX_COMPRESSED_ZIP_BYTES == 25 * 1024 * 1024
        assert isinstance(MAX_COMPRESSED_ZIP_BYTES, int)

    def test_max_extracted_zip_bytes(self) -> None:
        assert MAX_EXTRACTED_ZIP_BYTES == 100 * 1024 * 1024
        assert isinstance(MAX_EXTRACTED_ZIP_BYTES, int)

    def test_max_files(self) -> None:
        assert MAX_FILES == 5_000
        assert isinstance(MAX_FILES, int)

    def test_max_single_file_bytes(self) -> None:
        assert MAX_SINGLE_FILE_BYTES == 1 * 1024 * 1024
        assert isinstance(MAX_SINGLE_FILE_BYTES, int)

    def test_max_compression_ratio(self) -> None:
        assert MAX_COMPRESSION_RATIO == 20
        assert isinstance(MAX_COMPRESSION_RATIO, int)

    def test_max_github_archive_bytes(self) -> None:
        assert MAX_GITHUB_ARCHIVE_BYTES == 25 * 1024 * 1024
        assert isinstance(MAX_GITHUB_ARCHIVE_BYTES, int)

    def test_download_timeout_seconds(self) -> None:
        assert DOWNLOAD_TIMEOUT_SECONDS == 120
        assert isinstance(DOWNLOAD_TIMEOUT_SECONDS, int)


# =========================================================================
# 2. GitHub URL validation
# =========================================================================


class TestValidateGitHubURL:
    """Validate user-submitted GitHub repository URLs."""

    # ----- Valid URLs -----

    def test_valid_simple(self) -> None:
        owner, repo = validate_github_url("https://github.com/owner/repo")
        assert owner == "owner"
        assert repo == "repo"

    def test_valid_trailing_slash(self) -> None:
        owner, repo = validate_github_url("https://github.com/owner/repo/")
        assert owner == "owner"
        assert repo == "repo"

    def test_valid_hyphens_underscores_dots(self) -> None:
        owner, repo = validate_github_url("https://github.com/my-org/my_repo.lib")
        assert owner == "my-org"
        assert repo == "my_repo.lib"

    def test_valid_case_sensitive(self) -> None:
        owner, repo = validate_github_url("https://github.com/Owner/Repo")
        assert owner == "Owner"
        assert repo == "Repo"

    # ----- Rejected: wrong scheme -----

    def test_reject_http(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="HTTPS"):
            validate_github_url("http://github.com/owner/repo")

    def test_reject_ftp(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="HTTPS"):
            validate_github_url("ftp://github.com/owner/repo")

    def test_reject_missing_scheme(self) -> None:
        with pytest.raises(InvalidRepositoryURLError):
            validate_github_url("github.com/owner/repo")

    # ----- Rejected: wrong host -----

    def test_reject_evil_superdomain(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="github.com"):
            validate_github_url("https://github.com.evil.example/owner/repo")

    def test_reject_subdomain(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="github.com"):
            validate_github_url("https://api.github.com/owner/repo")

    def test_reject_raw_githubusercontent(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="github.com"):
            validate_github_url("https://raw.githubusercontent.com/owner/repo/main/README.md")

    def test_reject_codeload_as_submit(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="github.com"):
            validate_github_url("https://codeload.github.com/owner/repo/zip/refs/heads/main")

    # ----- Rejected: gists -----

    def test_reject_gist_url(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="github.com"):
            validate_github_url("https://gist.github.com/owner/abc123")

    # ----- Rejected: query / fragment -----

    def test_reject_query_string(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="query"):
            validate_github_url("https://github.com/owner/repo?tab=code")

    def test_reject_fragment(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="fragment"):
            validate_github_url("https://github.com/owner/repo#readme")

    # ----- Rejected: credentials -----

    def test_reject_userinfo(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="credentials"):
            validate_github_url("https://user:pass@github.com/owner/repo")

    def test_reject_username_only(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="credentials"):
            validate_github_url("https://user@github.com/owner/repo")

    # ----- Rejected: port -----

    def test_reject_port(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="port"):
            validate_github_url("https://github.com:8080/owner/repo")

    def test_reject_default_port_443(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="port"):
            validate_github_url("https://github.com:443/owner/repo")

    def test_reject_malformed_port(self) -> None:
        """Malformed port must raise typed error, never raw ValueError."""
        with pytest.raises(InvalidRepositoryURLError, match="port"):
            validate_github_url("https://github.com:invalid/owner/repo")

    # ----- Rejected: extra path segments -----

    def test_reject_extra_segments(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="owner.*repo"):
            validate_github_url("https://github.com/owner/repo/tree/main/src")

    def test_reject_three_segments(self) -> None:
        with pytest.raises(InvalidRepositoryURLError, match="owner.*repo"):
            validate_github_url("https://github.com/owner/repo/issues")

    # ----- Rejected: missing owner or repo -----

    def test_reject_missing_repo(self) -> None:
        with pytest.raises(InvalidRepositoryURLError):
            validate_github_url("https://github.com/owner")

    def test_reject_missing_owner(self) -> None:
        with pytest.raises(InvalidRepositoryURLError):
            validate_github_url("https://github.com/")

    def test_reject_root_only(self) -> None:
        with pytest.raises(InvalidRepositoryURLError):
            validate_github_url("https://github.com")

    # ----- Rejected: empty / non-string -----

    def test_reject_empty_string(self) -> None:
        with pytest.raises(InvalidRepositoryURLError):
            validate_github_url("")

    def test_reject_whitespace(self) -> None:
        with pytest.raises(InvalidRepositoryURLError):
            validate_github_url("   ")

    # ----- All errors are SourceTraceError -----

    def test_error_hierarchy(self) -> None:
        with pytest.raises(SourceTraceError):
            validate_github_url("http://github.com/owner/repo")


# =========================================================================
# 3. Redirect policy validation
# =========================================================================


class TestValidateRedirectURL:
    """Redirect policy during GitHub archive download."""

    # ----- Allowed redirect hosts -----

    def test_allow_github_com(self) -> None:
        validate_redirect_url("https://github.com/owner/repo/archive/refs/heads/main.zip")

    def test_allow_codeload(self) -> None:
        validate_redirect_url("https://codeload.github.com/owner/repo/zip/refs/heads/main")

    # ----- Rejected: non-HTTPS -----

    def test_reject_http_redirect(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="HTTPS"):
            validate_redirect_url("http://github.com/owner/repo")

    def test_reject_ftp_redirect(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="HTTPS"):
            validate_redirect_url("ftp://codeload.github.com/owner/repo")

    # ----- Rejected: other hosts -----

    def test_reject_evil_host(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="allowlist"):
            validate_redirect_url("https://evil.example.com/payload.zip")

    def test_reject_raw_githubusercontent_redirect(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="allowlist"):
            validate_redirect_url("https://raw.githubusercontent.com/owner/repo/main/f.py")

    def test_reject_api_github_redirect(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="allowlist"):
            validate_redirect_url("https://api.github.com/repos/owner/repo")

    def test_reject_superdomain_redirect(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="allowlist"):
            validate_redirect_url("https://github.com.evil.example/owner/repo")

    # ----- Rejected: empty -----

    def test_reject_empty_redirect(self) -> None:
        with pytest.raises(DisallowedRedirectError):
            validate_redirect_url("")

    # ----- Rejected: credentials -----

    def test_reject_redirect_with_credentials(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="credentials"):
            validate_redirect_url("https://user:pass@github.com/owner/repo/archive.zip")

    def test_reject_redirect_with_username_only(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="credentials"):
            validate_redirect_url("https://user@codeload.github.com/owner/repo/zip/main")

    # ----- Rejected: ports -----

    def test_reject_redirect_explicit_port(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="port"):
            validate_redirect_url("https://github.com:8080/owner/repo/archive.zip")

    def test_reject_redirect_default_port_443(self) -> None:
        with pytest.raises(DisallowedRedirectError, match="port"):
            validate_redirect_url("https://codeload.github.com:443/owner/repo/zip/main")

    def test_reject_redirect_malformed_port(self) -> None:
        """Malformed port must raise typed error, never raw ValueError."""
        with pytest.raises(DisallowedRedirectError, match="port"):
            validate_redirect_url("https://codeload.github.com:invalid/archive.zip")


# =========================================================================
# 4. IP address safety (SSRF protection)
# =========================================================================


class TestValidateIPAddress:
    """Address-validation helpers for SSRF protection."""

    # ----- Accepted: public addresses -----

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",
            "1.1.1.1",
            "140.82.121.3",
            "2606:4700:4700::1111",
            "104.16.0.1",
        ],
    )
    def test_accept_public_ipv4_and_ipv6(self, ip: str) -> None:
        validate_ip_address(ip)  # should not raise

    # ----- Rejected: loopback -----

    def test_reject_ipv4_loopback(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Ll]oopback"):
            validate_ip_address("127.0.0.1")

    def test_reject_ipv4_loopback_other(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Ll]oopback"):
            validate_ip_address("127.0.0.2")

    def test_reject_ipv6_loopback(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Ll]oopback"):
            validate_ip_address("::1")

    # ----- Rejected: private networks -----

    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.1.100",
        ],
    )
    def test_reject_private(self, ip: str) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Pp]rivate"):
            validate_ip_address(ip)

    # ----- Rejected: link-local -----

    def test_reject_ipv4_link_local(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Ll]ink"):
            validate_ip_address("169.254.1.1")

    def test_reject_ipv6_link_local(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Ll]ink"):
            validate_ip_address("fe80::1")

    # ----- Rejected: multicast -----

    def test_reject_ipv4_multicast(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Mm]ulticast"):
            validate_ip_address("224.0.0.1")

    def test_reject_ipv6_multicast(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Mm]ulticast"):
            validate_ip_address("ff02::1")

    # ----- Rejected: unspecified -----

    def test_reject_ipv4_unspecified(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Uu]nspecified"):
            validate_ip_address("0.0.0.0")

    def test_reject_ipv6_unspecified(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Uu]nspecified"):
            validate_ip_address("::")

    # ----- Rejected: reserved -----

    def test_reject_reserved(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("240.0.0.1")

    # ----- Rejected: carrier-grade NAT (100.64.0.0/10) -----

    def test_reject_carrier_grade_nat(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("100.64.0.1")

    def test_reject_carrier_grade_nat_upper(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("100.127.255.255")

    # ----- Rejected: IPv6 private equivalents -----

    def test_reject_ipv6_private_ula(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="[Pp]rivate"):
            validate_ip_address("fd12:3456:789a::1")

    # ----- Rejected: invalid inputs -----

    def test_reject_empty(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("")

    def test_reject_hostname(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError, match="valid"):
            validate_ip_address("example.com")

    # ----- Rejected: benchmarking range (198.18.0.0/15) -----

    def test_reject_benchmarking_range(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("198.18.0.1")

    def test_reject_benchmarking_range_upper(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("198.19.255.255")

    # ----- Rejected: IPv4-mapped IPv6 wrapping private addresses -----

    def test_reject_ipv4_mapped_ipv6_private(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("::ffff:192.168.1.1")

    def test_reject_ipv4_mapped_ipv6_rfc1918_10(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("::ffff:10.0.0.1")

    # ----- Rejected: documentation ranges -----

    def test_reject_documentation_range_1(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("192.0.2.1")

    def test_reject_documentation_range_2(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("198.51.100.1")

    def test_reject_documentation_range_3(self) -> None:
        with pytest.raises(UnsafeNetworkAddressError):
            validate_ip_address("203.0.113.1")

    # ----- Final guard: non-global catch-all -----

    def test_global_guard_allows_known_public(self) -> None:
        """Known public addresses must still pass the is_global guard."""
        validate_ip_address("8.8.4.4")  # should not raise
        validate_ip_address("2606:4700::6810:85e5")  # Cloudflare IPv6


# =========================================================================
# 5. Archive member path safety
# =========================================================================


class TestValidateArchiveMemberPath:
    """Archive member path traversal and safety validation."""

    # ----- Accepted: safe relative paths -----

    @pytest.mark.parametrize(
        "path",
        [
            "src/main.py",
            "backend/src/sourcetrace/core/config.py",
            "README.md",
            "a/b/c/d.txt",
            "file.py",
            ".hidden_dir/file.txt",
        ],
    )
    def test_accept_safe_paths(self, path: str) -> None:
        result = validate_archive_member_path(path)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_normalised_path(self) -> None:
        result = validate_archive_member_path("src//main.py")
        assert result == "src/main.py"

    def test_normalises_backslash_to_forward(self) -> None:
        result = validate_archive_member_path("src\\main.py")
        assert result == "src/main.py"

    # ----- Rejected: directory traversal -----

    def test_reject_dotdot_forward_slash(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="traversal"):
            validate_archive_member_path("../etc/passwd")

    def test_reject_dotdot_backslash(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="traversal"):
            validate_archive_member_path("..\\windows\\system32")

    def test_reject_mid_path_traversal(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="traversal"):
            validate_archive_member_path("src/../../etc/passwd")

    def test_reject_trailing_dotdot(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="traversal"):
            validate_archive_member_path("src/..")

    # ----- Rejected: absolute POSIX paths -----

    def test_reject_absolute_posix(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="absolute"):
            validate_archive_member_path("/etc/passwd")

    def test_reject_absolute_root(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="absolute"):
            validate_archive_member_path("/")

    # ----- Rejected: Windows drive-letter paths -----

    def test_reject_drive_letter_backslash(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="drive"):
            validate_archive_member_path("C:\\Windows\\system32")

    def test_reject_drive_letter_forward(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="drive"):
            validate_archive_member_path("C:/Users/admin")

    def test_reject_lowercase_drive(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="drive"):
            validate_archive_member_path("c:\\file.txt")

    # ----- Rejected: UNC paths -----

    def test_reject_unc_backslash(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="UNC"):
            validate_archive_member_path("\\\\server\\share\\file.txt")

    def test_reject_unc_forward(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="UNC"):
            validate_archive_member_path("//server/share/file.txt")

    # ----- Rejected: device paths -----

    def test_reject_device_path_dot(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="device"):
            validate_archive_member_path("\\\\.\\COM1")

    def test_reject_device_path_question(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="device"):
            validate_archive_member_path("\\\\?\\Volume{guid}")

    # ----- Rejected: NUL bytes -----

    def test_reject_nul_byte(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="NUL"):
            validate_archive_member_path("src/main\x00.py")

    # ----- Rejected: empty / whitespace -----

    def test_reject_empty(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="empty"):
            validate_archive_member_path("")

    def test_reject_whitespace_only(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="empty"):
            validate_archive_member_path("   ")

    def test_reject_dot_only(self) -> None:
        with pytest.raises(UnsafeArchiveMemberPathError, match="empty|root"):
            validate_archive_member_path(".")


# =========================================================================
# 6. Exception hierarchy
# =========================================================================


class TestExceptionHierarchy:
    """All ingestion security errors inherit from SourceTraceError."""

    @pytest.mark.parametrize(
        "exc_class",
        [
            InvalidRepositoryURLError,
            DisallowedRedirectError,
            UnsafeNetworkAddressError,
            UnsafeArchiveMemberPathError,
            IngestionLimitError,
        ],
    )
    def test_subclass_of_sourcetrace_error(self, exc_class: type[Exception]) -> None:
        assert issubclass(exc_class, SourceTraceError)

    @pytest.mark.parametrize(
        "exc_class",
        [
            InvalidRepositoryURLError,
            DisallowedRedirectError,
            UnsafeNetworkAddressError,
            UnsafeArchiveMemberPathError,
            IngestionLimitError,
        ],
    )
    def test_instantiation_with_message(self, exc_class: type[Exception]) -> None:
        err = exc_class("Test message")
        assert str(err) == "Test message"
        assert isinstance(err, Exception)
