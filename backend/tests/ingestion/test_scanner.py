"""Comprehensive offline tests for deterministic Python file discovery.

Tests use only temporary local fixtures — no internet, no live MongoDB,
no embedding provider, no API credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from sourcetrace.ingestion.scanner import (
    INVALID_PATH_PLACEHOLDER,
    SkipReason,
    scan_python_sources,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakeManifest:
    """Minimal ExtractionManifest stand-in for tests."""

    file_count: int
    total_extracted_bytes: int
    relative_paths: tuple[str, ...]


@dataclass(frozen=True)
class _FakeAcquiredSource:
    """Minimal AcquiredSource stand-in for tests."""

    extraction_root: Path
    manifest: _FakeManifest
    source_type: str


def _write_file(root: Path, rel: str, content: str = "x = 1\n") -> None:
    """Write a file at root / rel."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_source(
    tmp_path: Path,
    relative_paths: tuple[str, ...],
    source_type: str = "zip",
    *,
    content: str = "x = 1\n",
) -> _FakeAcquiredSource:
    """Create fake AcquiredSource with files on disk."""
    for rp in relative_paths:
        _write_file(tmp_path, rp, content)
    manifest = _FakeManifest(
        file_count=len(relative_paths),
        total_extracted_bytes=len(content) * len(relative_paths),
        relative_paths=relative_paths,
    )
    return _FakeAcquiredSource(
        extraction_root=tmp_path,
        manifest=manifest,
        source_type=source_type,
    )


# ---------------------------------------------------------------------------
# Stable lexicographic Python-file discovery
# ---------------------------------------------------------------------------

class TestStableLexicographicOrder:
    """Eligible files must be in stable lexicographic order by citation path."""

    def test_lexicographic_order(self, tmp_path: Path) -> None:
        paths = ("z_module.py", "a_module.py", "m_module.py")
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        citation_paths = [f.relative_path for f in result.eligible_files]
        assert citation_paths == sorted(citation_paths)

    def test_deterministic_repeated_calls(self, tmp_path: Path) -> None:
        paths = ("b.py", "a.py", "c.py")
        source = _make_source(tmp_path, paths)
        r1 = scan_python_sources(source)
        r2 = scan_python_sources(source)
        assert [f.relative_path for f in r1.eligible_files] == [
            f.relative_path for f in r2.eligible_files
        ]


# ---------------------------------------------------------------------------
# Non-Python files excluded
# ---------------------------------------------------------------------------

class TestNonPythonExcluded:
    """Non-Python files must be excluded."""

    def test_txt_excluded(self, tmp_path: Path) -> None:
        paths = ("module.py", "readme.txt", "config.json", "data.csv")
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 1
        assert result.eligible_files[0].relative_path == "module.py"

    def test_no_extension_excluded(self, tmp_path: Path) -> None:
        paths = ("module.py", "Makefile")
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 1

    def test_python_extension_is_intentionally_case_sensitive(self, tmp_path: Path) -> None:
        paths = ("lowercase.py", "uppercase.PY")
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        assert [item.relative_path for item in result.eligible_files] == ["lowercase.py"]
        skipped = {item.relative_path: item.reason for item in result.skipped}
        assert skipped["uppercase.PY"] == SkipReason.UNSUPPORTED_PATH


# ---------------------------------------------------------------------------
# Dependency/cache/VCS paths excluded
# ---------------------------------------------------------------------------

class TestExcludedDirectories:
    """Dependency, cache, VCS, and build directories must be excluded."""

    @pytest.mark.parametrize(
        "excluded_dir",
        [
            ".git", ".hg", ".svn", "__pycache__", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", ".tox", ".nox",
            "venv", ".venv", "env", "site-packages",
            "node_modules", "vendor", "dist", "build",
            "coverage", "htmlcov",
        ],
    )
    def test_excluded_directory(self, tmp_path: Path, excluded_dir: str) -> None:
        good_path = "app.py"
        bad_path = f"{excluded_dir}/module.py"
        source = _make_source(tmp_path, (good_path, bad_path))
        result = scan_python_sources(source)
        citations = [f.relative_path for f in result.eligible_files]
        assert good_path in citations
        assert bad_path not in citations

    def test_nested_excluded_dir(self, tmp_path: Path) -> None:
        paths = ("src/app.py", "src/__pycache__/cached.py")
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        citations = [f.relative_path for f in result.eligible_files]
        assert "src/app.py" in citations
        assert "src/__pycache__/cached.py" not in citations


# ---------------------------------------------------------------------------
# Case-folded exclusions
# ---------------------------------------------------------------------------

class TestCaseFoldedExclusions:
    """Exclusion policy checks must be case-insensitive / case-folded."""

    @pytest.mark.parametrize(
        "bad_path",
        [
            ".GIT/module.py",
            "Node_Modules/module.py",
            "VENV/script.py",
            "Credentials.py",
            "PRIVATE_KEY.py",
            "certificate.PEM",
            "server.KEY",
            ".ENV.local/config.py",
        ],
    )
    def test_case_insensitive_exclusions(self, tmp_path: Path, bad_path: str) -> None:
        good_path = "app.py"
        _write_file(tmp_path, good_path)
        _write_file(tmp_path, bad_path)
        manifest = _FakeManifest(
            file_count=2,
            total_extracted_bytes=100,
            relative_paths=(good_path, bad_path),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        citations = [f.relative_path for f in result.eligible_files]
        assert good_path in citations
        assert bad_path not in citations


class TestSensitiveFilenamePolicy:
    """Sensitive files must be excluded; legitimate security modules must remain eligible."""

    def test_legitimate_auth_py_remains(self, tmp_path: Path) -> None:
        paths = ("auth.py",)
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        assert any(f.relative_path == "auth.py" for f in result.eligible_files)

    def test_legitimate_token_py_remains(self, tmp_path: Path) -> None:
        paths = ("token.py",)
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        assert any(f.relative_path == "token.py" for f in result.eligible_files)

    def test_legitimate_secrets_py_remains(self, tmp_path: Path) -> None:
        paths = ("secrets.py",)
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        assert any(f.relative_path == "secrets.py" for f in result.eligible_files)

    def test_legitimate_security_py_remains(self, tmp_path: Path) -> None:
        paths = ("security.py",)
        source = _make_source(tmp_path, paths)
        result = scan_python_sources(source)
        assert any(f.relative_path == "security.py" for f in result.eligible_files)


# ---------------------------------------------------------------------------
# Reject rather than repair non-canonical paths
# ---------------------------------------------------------------------------

class TestNonCanonicalPathRejection:
    """Non-canonical paths must be rejected rather than repaired/redirected."""

    @pytest.mark.parametrize(
        "non_canonical_path",
        [
            " app.py",
            "app.py ",
            "src//app.py",
            "src/./app.py",
            "src/../app.py",
            "src/app.py/",
            "/src/app.py",
            "C:/src/app.py",
            "\\\\server\\share\\app.py",
            "path\x00evil.py",
        ],
    )
    def test_non_canonical_paths_rejected(
        self, tmp_path: Path, non_canonical_path: str
    ) -> None:
        good = "safe.py"
        _write_file(tmp_path, good)
        manifest = _FakeManifest(
            file_count=2,
            total_extracted_bytes=100,
            relative_paths=(non_canonical_path, good),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        eligible_paths = [f.relative_path for f in result.eligible_files]
        assert good in eligible_paths

        # The non-canonical path must be rejected with INVALID_PATH_PLACEHOLDER
        skipped_paths = [s.relative_path for s in result.skipped]
        assert INVALID_PATH_PLACEHOLDER in skipped_paths
        # Raw untrusted spelling must NOT appear in skipped paths
        assert non_canonical_path not in skipped_paths

    def test_backslash_separator_is_the_only_path_repair(self, tmp_path: Path) -> None:
        _write_file(tmp_path, "src/app.py")
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=10,
            relative_paths=(r"src\app.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )

        result = scan_python_sources(source)

        assert [item.relative_path for item in result.eligible_files] == ["src/app.py"]
        assert result.skipped == ()


# ---------------------------------------------------------------------------
# Redaction of invalid raw manifest paths
# ---------------------------------------------------------------------------

class TestInvalidPathRedaction:
    """Invalid raw manifest paths must never leak into SkippedFile or repr()."""

    @pytest.mark.parametrize(
        "sensitive_raw_path",
        [
            "/etc/passwd/secret.py",
            "C:/Users/Admin/SecretKey/credentials.py",
            "\\\\secret_server\\share\\auth.py",
            "../../secret_dir/key.py",
            "super_secret\x00private.py",
        ],
    )
    def test_sensitive_markers_redacted(
        self, tmp_path: Path, sensitive_raw_path: str
    ) -> None:
        good = "app.py"
        _write_file(tmp_path, good)
        manifest = _FakeManifest(
            file_count=2,
            total_extracted_bytes=100,
            relative_paths=(sensitive_raw_path, good),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)

        for skipped in result.skipped:
            assert skipped.relative_path == INVALID_PATH_PLACEHOLDER
            assert sensitive_raw_path not in skipped.relative_path

        result_repr = repr(result)
        assert sensitive_raw_path not in result_repr
        assert "secret_server" not in result_repr
        assert "passwd" not in result_repr
        assert "Admin" not in result_repr


# ---------------------------------------------------------------------------
# Platform-independent symlink tests
# ---------------------------------------------------------------------------

class TestPlatformIndependentSymlinks:
    """Platform-independent tests for leaf and parent-directory symlinks using mocks."""

    def test_leaf_symlink_rejected_before_read(self, tmp_path: Path) -> None:
        _write_file(tmp_path, "module.py", "x = 1\n")
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=10,
            relative_paths=("module.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )

        with patch("sourcetrace.ingestion.scanner._is_symlink", return_value=True), \
             patch("sourcetrace.ingestion.scanner._decode_python_source") as mock_decode:
            result = scan_python_sources(source)

            assert len(result.eligible_files) == 0
            assert len(result.skipped) == 1
            assert result.skipped[0].reason == SkipReason.SYMLINK_REJECTED
            # Ensure source decode was NEVER called
            mock_decode.assert_not_called()

    def test_parent_directory_symlink_rejected(self, tmp_path: Path) -> None:
        _write_file(tmp_path, "link_dir/module.py", "x = 1\n")
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=10,
            relative_paths=("link_dir/module.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )

        def fake_is_symlink(p: Path) -> bool:
            return "link_dir" in p.parts

        with patch("sourcetrace.ingestion.scanner._is_symlink", side_effect=fake_is_symlink), \
             patch("sourcetrace.ingestion.scanner._decode_python_source") as mock_decode:
            result = scan_python_sources(source)

            assert len(result.eligible_files) == 0
            assert len(result.skipped) == 1
            assert result.skipped[0].reason == SkipReason.SYMLINK_REJECTED
            mock_decode.assert_not_called()


# ---------------------------------------------------------------------------
# Real filesystem symlink test (conditional)
# ---------------------------------------------------------------------------

class TestRealSymlinkRejected:
    """Symlinks must be rejected even if they appear after extraction."""

    def test_symlink_skipped(self, tmp_path: Path) -> None:
        real = tmp_path / "real.py"
        real.write_text("x = 1\n", encoding="utf-8")
        link = tmp_path / "linked.py"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("Cannot create symlinks on this system")
        manifest = _FakeManifest(
            file_count=2,
            total_extracted_bytes=100,
            relative_paths=("real.py", "linked.py"),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        eligible_paths = [f.relative_path for f in result.eligible_files]
        assert "real.py" in eligible_paths
        assert "linked.py" not in eligible_paths
        skipped_reasons = {s.relative_path: s.reason for s in result.skipped}
        assert skipped_reasons.get("linked.py") == SkipReason.SYMLINK_REJECTED


# ---------------------------------------------------------------------------
# Oversized file rejected before read
# ---------------------------------------------------------------------------

class TestOversizedFileRejected:
    """Files exceeding MAX_SINGLE_FILE_BYTES must be skipped before read."""

    def test_oversized_file_skipped(self, tmp_path: Path) -> None:
        from sourcetrace.ingestion.limits import MAX_SINGLE_FILE_BYTES

        good = "small.py"
        bad = "large.py"
        _write_file(tmp_path, good, "x = 1\n")
        large_content = "x" * (MAX_SINGLE_FILE_BYTES + 1)
        _write_file(tmp_path, bad, large_content)
        manifest = _FakeManifest(
            file_count=2,
            total_extracted_bytes=MAX_SINGLE_FILE_BYTES + 10,
            relative_paths=(good, bad),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        eligible_paths = [f.relative_path for f in result.eligible_files]
        assert good in eligible_paths
        assert bad not in eligible_paths
        skipped_reasons = {s.relative_path: s.reason for s in result.skipped}
        assert skipped_reasons.get(bad) == SkipReason.FILE_TOO_LARGE


# ---------------------------------------------------------------------------
# GitHub wrapper stripped only for GitHub source
# ---------------------------------------------------------------------------

class TestGitHubWrapperStripping:
    """GitHub archive wrapper directory must be stripped from citation paths
    only for source_type=='github'."""

    def test_github_wrapper_stripped(self, tmp_path: Path) -> None:
        wrapper = "myrepo-main"
        paths = (
            f"{wrapper}/src/app.py",
            f"{wrapper}/src/utils.py",
        )
        source = _make_source(tmp_path, paths, source_type="github")
        result = scan_python_sources(source)
        citations = [f.relative_path for f in result.eligible_files]
        assert "src/app.py" in citations
        assert "src/utils.py" in citations
        assert all(not c.startswith(wrapper) for c in citations)

    def test_github_physical_path_preserved(self, tmp_path: Path) -> None:
        wrapper = "myrepo-main"
        paths = (f"{wrapper}/app.py",)
        source = _make_source(tmp_path, paths, source_type="github")
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 1
        physical = result.eligible_files[0].physical_path
        assert wrapper in str(physical)


# ---------------------------------------------------------------------------
# ZIP top-level directory preserved
# ---------------------------------------------------------------------------

class TestZipTopLevelPreserved:
    """ZIP uploads must preserve submitted repository-relative paths."""

    def test_zip_preserves_top_level_dir(self, tmp_path: Path) -> None:
        paths = (
            "myproject/src/app.py",
            "myproject/src/utils.py",
        )
        source = _make_source(tmp_path, paths, source_type="zip")
        result = scan_python_sources(source)
        citations = [f.relative_path for f in result.eligible_files]
        assert "myproject/src/app.py" in citations
        assert "myproject/src/utils.py" in citations


# ---------------------------------------------------------------------------
# Encoding classification tests
# ---------------------------------------------------------------------------

class TestEncodingClassifications:
    """Test detailed encoding failure classification."""

    def test_unknown_pep263_codec_invalid_encoding(self, tmp_path: Path) -> None:
        content_bytes = b"# -*- coding: unknown_codec_xyz_123 -*-\n\ndef f():\n    pass\n"
        p = tmp_path / "bad_codec.py"
        p.write_bytes(content_bytes)
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=len(content_bytes),
            relative_paths=("bad_codec.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == SkipReason.INVALID_ENCODING

    def test_malformed_encoding_cookie_invalid_encoding(self, tmp_path: Path) -> None:
        content_bytes = b"\xef\xbb\xbf# -*- coding: latin-1 -*-\ndef f(): pass\n"
        p = tmp_path / "bad_cookie.py"
        p.write_bytes(content_bytes)
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=len(content_bytes),
            relative_paths=("bad_cookie.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == SkipReason.INVALID_ENCODING

    def test_unicode_decode_failure_invalid_encoding(self, tmp_path: Path) -> None:
        content_bytes = b"# -*- coding: utf-8 -*-\n\ndef func():\n    return '\xff\xfe'\n"
        p = tmp_path / "bad_utf8.py"
        p.write_bytes(content_bytes)
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=len(content_bytes),
            relative_paths=("bad_utf8.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == SkipReason.INVALID_ENCODING

    def test_embedded_nul_invalid_encoding(self, tmp_path: Path) -> None:
        content_bytes = b"def func():\n    return 'a\x00b'\n"
        p = tmp_path / "nul_byte.py"
        p.write_bytes(content_bytes)
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=len(content_bytes),
            relative_paths=("nul_byte.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == SkipReason.INVALID_ENCODING

    def test_oserror_unreadable_source(self, tmp_path: Path) -> None:
        p = tmp_path / "unreadable.py"
        p.write_text("x = 1\n", encoding="utf-8")
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=10,
            relative_paths=("unreadable.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )

        with patch("builtins.open", side_effect=OSError("Read error")):
            result = scan_python_sources(source)
            assert len(result.eligible_files) == 0
            assert len(result.skipped) == 1
            assert result.skipped[0].reason == SkipReason.UNREADABLE_SOURCE


# ---------------------------------------------------------------------------
# File not found
# ---------------------------------------------------------------------------

class TestFileNotFound:
    """Missing files must be reported with FILE_NOT_FOUND."""

    def test_missing_file_skipped(self, tmp_path: Path) -> None:
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=100,
            relative_paths=("nonexistent.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 0
        assert any(s.reason == SkipReason.FILE_NOT_FOUND for s in result.skipped)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    """Duplicate paths in manifest must be deduplicated."""

    def test_duplicate_paths_deduplicated(self, tmp_path: Path) -> None:
        paths = ("app.py", "app.py")
        _write_file(tmp_path, "app.py")
        manifest = _FakeManifest(
            file_count=2,
            total_extracted_bytes=100,
            relative_paths=paths,
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        eligible_paths = [f.relative_path for f in result.eligible_files]
        assert eligible_paths.count("app.py") == 1


# ---------------------------------------------------------------------------
# Empty source files skipped with EMPTY_SOURCE
# ---------------------------------------------------------------------------

class TestEmptySourceFile:
    """Empty or whitespace-only Python source files must be skipped with EMPTY_SOURCE."""

    def test_empty_file_skipped(self, tmp_path: Path) -> None:
        _write_file(tmp_path, "empty.py", "")
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=0,
            relative_paths=("empty.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == SkipReason.EMPTY_SOURCE

    def test_whitespace_only_file_skipped(self, tmp_path: Path) -> None:
        _write_file(tmp_path, "space.py", "  \n\n  \t \n")
        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=10,
            relative_paths=("space.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=tmp_path,
            manifest=manifest,
            source_type="zip",
        )
        result = scan_python_sources(source)
        assert len(result.eligible_files) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == SkipReason.EMPTY_SOURCE


# ---------------------------------------------------------------------------
# Resolved root escape handling
# ---------------------------------------------------------------------------

class TestResolvedRootEscape:
    """Paths that resolve outside extraction root must be rejected as UNSUPPORTED_PATH."""

    def test_path_escaping_root_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "extract_root"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n", encoding="utf-8")

        manifest = _FakeManifest(
            file_count=1,
            total_extracted_bytes=10,
            relative_paths=("sub/escape.py",),
        )
        source = _FakeAcquiredSource(
            extraction_root=root,
            manifest=manifest,
            source_type="zip",
        )

        phys_path = root / "sub" / "escape.py"
        phys_path.parent.mkdir()
        phys_path.write_text("x = 1\n", encoding="utf-8")

        def fake_resolve(self_path, strict=False):
            if self_path == phys_path:
                return outside
            return self_path

        with patch("pathlib.Path.resolve", side_effect=fake_resolve):
            result = scan_python_sources(source)
            assert len(result.eligible_files) == 0
            assert any(s.reason == SkipReason.UNSUPPORTED_PATH for s in result.skipped)


